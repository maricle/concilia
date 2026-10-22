"""Salidas desde la interfaz web (/m): mismas reglas que los comandos del chat."""

from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import servicios
from app.auth import hash_password
from app.db import Base
from app.main import app
from app.models import BankAccount, Movement, Movil, Operator, RecordState, Reparto, RepartoOperador
from app.operador_web import sesion

PIN = "482915"


def _cliente():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    fabrica = sessionmaker(bind=engine)

    def override_get_db():
        with fabrica() as session:
            yield session

    app.dependency_overrides[sesion.get_db] = override_get_db
    with fabrica() as db:
        db.add_all(
            [
                Operator(nombre="Ana", whatsapp_numero="3794500000", web_habilitado=True, pin_hash=hash_password(PIN)),
                Operator(nombre="Beto", whatsapp_numero="3794511111"),
            ]
        )
        db.add(BankAccount(banco="Galicia", numero_cuenta="1", alias="empresa.galicia"))
        db.commit()
        db.add_all(
            [
                Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1),
                Movil(numero="M-02", nombre="Camion 2", responsable_operador_id=2),
                Movil(numero="0", nombre="Entrega y retiro", responsable_operador_id=1),
            ]
        )
        db.commit()
    client = TestClient(app)
    client.post("/m/login", data={"celular": "3794500000", "pin": PIN})
    return client, fabrica


def teardown_function():
    app.dependency_overrides.clear()


def test_iniciar_salida_pide_el_movil_solo_si_la_salida_no_esta_abierta():
    client, fabrica = _cliente()

    formulario = client.get("/m/salida/iniciar").text
    assert 'name="numero"' in formulario and 'name="movil_id"' not in formulario

    pide_movil = client.post("/m/salida/iniciar", data={"numero": "5"})
    assert pide_movil.status_code == 200
    assert "La Salida Nº 5 no está abierta. Elegí el móvil para iniciarla." in pide_movil.text
    assert 'name="movil_id"' in pide_movil.text and 'value="5"' in pide_movil.text

    respuesta = client.post("/m/salida/iniciar", data={"numero": "5", "movil_id": "1"})

    assert "Salida Nº 5 iniciada" in respuesta.text
    with fabrica() as db:
        salida = db.query(Reparto).one()
        assert (salida.numero_reparto, salida.movil_id, salida.hora_fin) == (5, 1, None)
        assert db.get(Operator, 1).movil_id is None
    inicio = client.get("/m/").text
    assert "Salida Nº <strong>5</strong> abierta" in inicio and "(móvil M-01)" in inicio


def test_numero_de_salida_invalido():
    client, _ = _cliente()

    assert client.post("/m/salida/iniciar", data={"numero": "cinco", "movil_id": "2"}).status_code == 400


def test_movil_0_de_la_sala_funciona_igual():
    client, fabrica = _cliente()

    respuesta = client.post("/m/salida/iniciar", data={"numero": "1", "movil_id": "3"})

    assert "Salida Nº 1 iniciada" in respuesta.text
    with fabrica() as db:
        assert db.query(Reparto).one().movil_id == 3


def test_se_suma_a_la_salida_abierta_con_solo_el_numero():
    client, fabrica = _cliente()
    with fabrica() as db:
        servicios.crear_salida(db, db.get(Operator, 2), db.get(Movil, 1), 3)
        db.commit()

    respuesta = client.post("/m/salida/iniciar", data={"numero": "3"})

    assert "Te sumaste a la Salida Nº 3" in respuesta.text and "Móvil M-01." in respuesta.text
    with fabrica() as db:
        assert db.query(Reparto).count() == 1
        assert db.query(RepartoOperador).count() == 2


def test_movil_con_otra_salida_abierta_avisa_sin_sumarse():
    client, fabrica = _cliente()
    with fabrica() as db:
        servicios.crear_salida(db, db.get(Operator, 2), db.get(Movil, 1), 3)
        db.commit()

    respuesta = client.post("/m/salida/iniciar", data={"numero": "9", "movil_id": "1"})

    assert respuesta.status_code == 400
    assert "El móvil M-01 ya tiene abierta la Salida Nº 3" in respuesta.text
    with fabrica() as db:
        assert db.query(Reparto).count() == 1
        assert servicios.reparto_abierto_del_operador(db, 1) is None


def test_con_otra_salida_abierta_pregunta_cerrarla_o_continuar():
    client, fabrica = _cliente()
    client.post("/m/salida/iniciar", data={"numero": "5", "movil_id": "1"})

    mismo_movil = client.post("/m/salida/iniciar", data={"numero": "5"})
    assert "Ya estás en la Salida Nº 5" in mismo_movil.text
    assert "Ya tenés la Salida Nº 5 abierta" in client.get("/m/salida/iniciar").text

    pregunta = client.post("/m/salida/iniciar", data={"numero": "6", "movil_id": "2"})
    assert "Cerrarla y empezar la Nº 6" in pregunta.text
    continuar = client.post("/m/salida/iniciar", data={"numero": "6", "movil_id": "2", "decision": "continuar"}, follow_redirects=False)
    assert continuar.headers["location"] == "/m/"
    with fabrica() as db:
        assert db.query(Reparto).count() == 1

    cerrar = client.post("/m/salida/iniciar", data={"numero": "6", "movil_id": "2", "decision": "cerrar"})
    assert "Salida Nº 6 iniciada" in cerrar.text
    with fabrica() as db:
        primera, segunda = db.query(Reparto).order_by(Reparto.id).all()
        assert primera.hora_fin is not None and segunda.hora_fin is None


def test_cerrar_salida_muestra_los_totales_y_cierra_para_todos():
    client, fabrica = _cliente()
    client.post("/m/salida/iniciar", data={"numero": "5", "movil_id": "1"})
    with fabrica() as db:
        salida = db.query(Reparto).one()
        servicios.asociar_operador(db, salida, db.get(Operator, 2))
        db.add(
            Movement(
                operador_id=1, monto=Decimal("1500"), fecha_transaccion=datetime(2026, 10, 2), numero_operacion="OP-1",
                cuenta_bancaria_id=1, reparto_id=salida.id, estado_registro=RecordState.CONFIRMADO,
            )
        )
        db.commit()

    confirmacion = client.get("/m/salida/cerrar").text
    assert "Galicia: $1.500,00" in confirmacion

    cerrada = client.post("/m/salida/cerrar")

    assert "Salida Nº 5 cerrada" in cerrada.text
    assert "Galicia: $1.500,00" in cerrada.text
    with fabrica() as db:
        assert db.query(Reparto).one().hora_fin is not None
        assert servicios.reparto_abierto_del_operador(db, 2) is None
    assert client.get("/m/salida/cerrar", follow_redirects=False).headers["location"] == "/m/"


def test_salidas_requieren_sesion():
    client = TestClient(app)

    assert client.get("/m/salida/iniciar", follow_redirects=False).headers["location"] == "/m/login"
    assert client.post("/m/salida/cerrar", follow_redirects=False).headers["location"] == "/m/login"
