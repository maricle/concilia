"""Salidas desde el panel: un administrador las abre, cierra, reabre y elimina;
la tabla muestra quien la abrio y el ultimo comprobante."""

from datetime import date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import panel
from app.auth import hash_password
from app.conversation import ConversationService
from app.db import Base
from app.main import app
from app.models import Movement, Movil, Operator, PanelUser, RecordState, Reparto, RepartoOperador


def _entorno(rol: str = "Administrador"):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    fabrica = sessionmaker(bind=engine)

    def override_get_db():
        with fabrica() as session:
            yield session

    app.dependency_overrides[panel.get_db] = override_get_db
    with fabrica() as db:
        db.add(PanelUser(nombre="Maria", email="maria@concilia.test", password_hash=hash_password("secreta123"), rol=rol))
        db.add_all(
            [
                Operator(nombre="Ana", whatsapp_numero="5491112345678"),
                Operator(nombre="Beto", whatsapp_numero="5491112345679"),
            ]
        )
        db.commit()
        db.add_all(
            [
                Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1),
                Movil(numero="M-02", nombre="Camion 2", responsable_operador_id=1),
            ]
        )
        db.commit()
    client = TestClient(app)
    client.post("/login", data={"email": "maria@concilia.test", "password": "secreta123"})
    return client, fabrica


def teardown_function():
    app.dependency_overrides.clear()


def _abrir(client, movil_id: int = 1, numero: str = "5"):
    return client.post("/repartos/nueva", data={"movil_id": movil_id, "numero_reparto": numero}, follow_redirects=False)


def test_administrador_abre_una_salida_y_queda_registrado_quien():
    client, fabrica = _entorno()

    respuesta = _abrir(client)

    assert respuesta.status_code == 303
    assert "mensaje=" in respuesta.headers["location"]
    with fabrica() as db:
        salida = db.query(Reparto).one()
        assert (salida.numero_reparto, salida.movil_id, salida.hora_fin) == (5, 1, None)
        assert salida.abierta_por_usuario.nombre == "Maria"
        assert db.query(RepartoOperador).count() == 0
    assert "Maria (panel)" in client.get("/repartos").text


def test_un_operador_se_suma_por_numero_a_la_salida_abierta_desde_el_panel():
    client, fabrica = _entorno()
    _abrir(client)

    with fabrica() as db:
        service = ConversationService(db)
        respuesta = service.handle_text("5491112345678", "inicio salida nro 5")

        assert respuesta == "Te sumaste a la Salida Nº 5 en el movil M-01."
        assert db.query(Reparto).count() == 1
        assert db.query(RepartoOperador).one().operador_id == 1


def test_no_abre_si_el_movil_o_el_numero_ya_estan_en_una_salida_abierta():
    client, fabrica = _entorno()
    _abrir(client, movil_id=1, numero="5")

    mismo = _abrir(client, movil_id=1, numero="5")
    movil_ocupado = _abrir(client, movil_id=1, numero="6")
    numero_ocupado = _abrir(client, movil_id=2, numero="5")
    numero_invalido = _abrir(client, movil_id=2, numero="abc")

    for respuesta in (mismo, movil_ocupado, numero_ocupado, numero_invalido):
        assert "error=" in respuesta.headers["location"]
    with fabrica() as db:
        assert db.query(Reparto).count() == 1


def test_operador_que_abre_por_chat_queda_como_quien_la_abrio():
    client, fabrica = _entorno()
    with fabrica() as db:
        service = ConversationService(db)
        service.handle_text("5491112345678", "inicio movil M-01 reparto nro 5")
        service.handle_text("5491112345678", "SI")
        assert db.query(Reparto).one().abierta_por_operador_id == 1

    listado = client.get("/repartos").text
    assert "<td>Ana</td>" in listado


def test_cerrar_y_reabrir_registra_quien_reabrio():
    client, fabrica = _entorno()
    _abrir(client)

    client.post("/repartos/1/cerrar")
    with fabrica() as db:
        assert db.get(Reparto, 1).hora_fin is not None

    client.post("/repartos/1/reabrir")
    with fabrica() as db:
        salida = db.get(Reparto, 1)
        assert salida.hora_fin is None
        assert salida.reabierta_por_usuario.nombre == "Maria"
        assert salida.reabierta_en is not None
    assert "Reabierta por Maria" in client.get("/repartos").text


def test_no_reabre_si_el_movil_tiene_otra_salida_o_un_operador_esta_en_otra():
    client, fabrica = _entorno()
    _abrir(client, movil_id=1, numero="5")
    with fabrica() as db:
        db.add(RepartoOperador(reparto_id=1, operador_id=1))
        db.commit()
    client.post("/repartos/1/cerrar")
    _abrir(client, movil_id=1, numero="6")

    movil_ocupado = client.post("/repartos/1/reabrir", follow_redirects=False)
    assert "error=" in movil_ocupado.headers["location"]

    client.post("/repartos/2/cerrar")
    _abrir(client, movil_id=2, numero="7")
    with fabrica() as db:
        db.add(RepartoOperador(reparto_id=3, operador_id=1))
        db.commit()
    operador_ocupado = client.post("/repartos/1/reabrir")
    assert "Ana esta en otra salida abierta" in operador_ocupado.text
    with fabrica() as db:
        assert db.get(Reparto, 1).hora_fin is not None


def test_eliminar_solo_salidas_sin_comprobantes():
    client, fabrica = _entorno()
    _abrir(client, movil_id=1, numero="5")
    _abrir(client, movil_id=2, numero="6")
    with fabrica() as db:
        db.add(RepartoOperador(reparto_id=1, operador_id=1))
        db.add(
            Movement(operador_id=2, reparto_id=2, monto=Decimal("100"), estado_registro=RecordState.CONFIRMADO)
        )
        db.commit()

    sin_comprobantes = client.post("/repartos/1/eliminar")
    con_comprobantes = client.post("/repartos/2/eliminar")

    assert "Salida Nº 5 eliminada" in sin_comprobantes.text
    assert "tiene 1 comprobante" in con_comprobantes.text
    with fabrica() as db:
        assert [r.numero_reparto for r in db.query(Reparto)] == [6]
        assert db.query(RepartoOperador).count() == 0


def test_la_tabla_muestra_el_ultimo_comprobante_de_cada_salida():
    client, fabrica = _entorno()
    _abrir(client)
    with fabrica() as db:
        db.add_all(
            [
                Movement(
                    operador_id=1,
                    reparto_id=1,
                    monto=Decimal("100"),
                    fecha_alta=datetime(2026, 10, 28, 9, 15),
                    estado_registro=RecordState.CONFIRMADO,
                ),
                Movement(
                    operador_id=2,
                    reparto_id=1,
                    monto=Decimal("36396.31"),
                    fecha_alta=datetime(2026, 10, 28, 11, 40),
                    estado_registro=RecordState.CONFIRMADO,
                ),
            ]
        )
        db.commit()

    listado = client.get("/repartos").text

    assert "28/10 11:40 · $36.396,31" in listado
    assert "Beto" in listado
    assert "09:15" not in listado


def test_solo_administradores_abren_cierran_reabren_y_eliminan():
    client, fabrica = _entorno(rol="Recaudador")
    with fabrica() as db:
        db.add(Reparto(movil_id=1, fecha=date(2026, 10, 28), hora_inicio=datetime(2026, 10, 28, 8, 0), numero_reparto=5))
        db.commit()

    listado = client.get("/repartos").text
    assert "/repartos/nueva" not in listado and "/repartos/1/eliminar" not in listado
    for ruta in ("/repartos/1/cerrar", "/repartos/1/reabrir", "/repartos/1/eliminar"):
        assert client.post(ruta).status_code == 403, ruta
    assert _abrir(client).status_code == 403
    with fabrica() as db:
        assert db.get(Reparto, 1).hora_fin is None
