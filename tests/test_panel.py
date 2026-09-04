from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import panel
from app.auth import hash_password
from app.db import Base
from app.main import app
from app.models import BankAccount, Movement, Movil, Operator, PanelUser, RecordState


def _client_with_admin():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine)

    def override_get_db():
        with test_session() as session:
            yield session

    app.dependency_overrides[panel.get_db] = override_get_db

    with test_session() as session:
        session.add(PanelUser(nombre="Admin", email="admin@concilia.test", password_hash=hash_password("secreta123")))
        session.commit()

    return TestClient(app), test_session


def teardown_function():
    app.dependency_overrides.clear()


def test_operadores_page_requires_login():
    client, _ = _client_with_admin()
    response = client.get("/config/operadores", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_with_wrong_password_is_rejected():
    client, _ = _client_with_admin()
    response = client.post("/login", data={"email": "admin@concilia.test", "password": "incorrecta"})
    assert response.status_code == 401


def test_login_and_create_operador():
    client, test_session = _client_with_admin()
    login = client.post(
        "/login", data={"email": "admin@concilia.test", "password": "secreta123"}, follow_redirects=False
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/resumen"

    create = client.post(
        "/config/operadores",
        data={"nombre": "Ana", "whatsapp_numero": "111222333", "tipo": "Reparto"},
        follow_redirects=False,
    )
    assert create.status_code == 303

    with test_session() as session:
        operador = session.query(Operator).filter_by(whatsapp_numero="111222333").one()
        assert operador.nombre == "Ana"


def test_duplicate_operador_number_is_rejected():
    client, _ = _client_with_admin()
    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    client.post("/config/operadores", data={"nombre": "Ana", "whatsapp_numero": "555", "tipo": "Reparto"})

    response = client.post("/config/operadores", data={"nombre": "Otra", "whatsapp_numero": "555", "tipo": "Reparto"})
    assert response.status_code == 400
    assert "Ya existe un operador" in response.text


def test_create_cuenta_bancaria():
    client, test_session = _client_with_admin()
    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})

    response = client.post(
        "/config/cuentas",
        data={"banco": "Banco Nacion", "numero_cuenta": "123-456", "alias": "Cuenta principal", "moneda": "ARS"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with test_session() as session:
        cuenta = session.query(BankAccount).filter_by(numero_cuenta="123-456").one()
        assert cuenta.alias == "Cuenta principal"


def test_editar_operador_updates_fields():
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add(Operator(nombre="Ana", whatsapp_numero="111", tipo="Reparto"))
        session.commit()

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    response = client.post(
        "/config/operadores/1/editar",
        data={"nombre": "Ana Corregida", "whatsapp_numero": "222", "tipo": "Administrativo"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with test_session() as session:
        operador = session.get(Operator, 1)
        assert operador.nombre == "Ana Corregida"
        assert operador.whatsapp_numero == "222"
        assert operador.tipo == "Administrativo"


def test_editar_operador_rejects_duplicate_numero():
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add_all(
            [
                Operator(nombre="Ana", whatsapp_numero="111"),
                Operator(nombre="Beto", whatsapp_numero="222"),
            ]
        )
        session.commit()

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    response = client.post(
        "/config/operadores/2/editar",
        data={"nombre": "Beto", "whatsapp_numero": "111", "tipo": "Reparto"},
    )
    assert response.status_code == 400
    assert "Ya existe otro operador" in response.text


def test_editar_cuenta_bancaria_updates_fields():
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add(BankAccount(banco="Banco Nacion", numero_cuenta="123", alias="Vieja", moneda="ARS"))
        session.commit()

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    response = client.post(
        "/config/cuentas/1/editar",
        data={"banco": "Banco Galicia", "numero_cuenta": "999", "alias": "Nueva", "moneda": "USD"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with test_session() as session:
        cuenta = session.get(BankAccount, 1)
        assert cuenta.banco == "Banco Galicia"
        assert cuenta.numero_cuenta == "999"
        assert cuenta.alias == "Nueva"
        assert cuenta.moneda == "USD"


def test_movimientos_list_shows_only_confirmed():
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add(Operator(nombre="Ana", whatsapp_numero="111"))
        session.commit()
        session.add(
            Movement(
                operador_id=1,
                monto=Decimal("500"),
                fecha_transaccion=datetime(2026, 8, 24),
                numero_operacion="OP-CONFIRMADO",
                estado_registro=RecordState.CONFIRMADO,
            )
        )
        session.add(
            Movement(
                operador_id=1,
                monto=Decimal("300"),
                fecha_transaccion=datetime(2026, 8, 24),
                numero_operacion="OP-BORRADOR",
                estado_registro=RecordState.PENDIENTE_CONFIRMACION,
            )
        )
        session.commit()

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    response = client.get("/comprobantes")

    assert response.status_code == 200
    assert "OP-CONFIRMADO" in response.text
    assert "OP-BORRADOR" not in response.text


def test_editar_movimiento_requires_login():
    client, _ = _client_with_admin()
    response = client.get("/comprobantes/1/editar", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_ver_archivo_requires_login():
    client, _ = _client_with_admin()
    response = client.get("/comprobantes/1/archivo", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_ver_archivo_redirects_when_movimiento_has_no_archivo():
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add(Operator(nombre="Ana", whatsapp_numero="111"))
        session.commit()
        session.add(
            Movement(
                operador_id=1,
                monto=Decimal("500"),
                fecha_transaccion=datetime(2026, 8, 24),
                numero_operacion="OP-1",
                estado_registro=RecordState.CONFIRMADO,
            )
        )
        session.commit()

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    response = client.get("/comprobantes/1/archivo", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/comprobantes"


def test_ver_archivo_returns_file_content(monkeypatch):
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add(Operator(nombre="Ana", whatsapp_numero="111"))
        session.commit()
        session.add(
            Movement(
                operador_id=1,
                monto=Decimal("500"),
                fecha_transaccion=datetime(2026, 8, 24),
                numero_operacion="OP-1",
                estado_registro=RecordState.CONFIRMADO,
                archivo_id=42,
            )
        )
        session.commit()

    class _FakeArchivo:
        contenido = b"fake-image-bytes"
        content_type = "image/jpeg"
        nombre_archivo = "comprobante.jpg"

    monkeypatch.setattr(panel, "get_comprobante_archivo", lambda archivo_id: _FakeArchivo() if archivo_id == 42 else None)

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    response = client.get("/comprobantes/1/archivo")

    assert response.status_code == 200
    assert response.content == b"fake-image-bytes"
    assert response.headers["content-type"] == "image/jpeg"


def test_editar_movimiento_updates_fields():
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add(Operator(nombre="Ana", whatsapp_numero="111"))
        session.commit()
        session.add(
            Movement(
                operador_id=1,
                monto=Decimal("500"),
                fecha_transaccion=datetime(2026, 8, 24),
                numero_operacion="OP-1",
                banco_emisor="Banco Viejo",
                estado_registro=RecordState.CONFIRMADO,
            )
        )
        session.commit()

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    response = client.post(
        "/comprobantes/1/editar",
        data={
            "fecha_transaccion": "2026-08-20T00:00",
            "monto": "650.50",
            "numero_operacion": "OP-1-CORREGIDO",
            "banco_emisor": "Banco Nuevo",
            "cuenta_receptora_extraida": "CBU12345",
            "titular": "Juan Perez",
            "factura_o_cuenta_tipo": "factura",
            "factura_o_cuenta_numero": "9999",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with test_session() as session:
        movimiento = session.get(Movement, 1)
        assert movimiento.numero_operacion == "OP-1-CORREGIDO"
        assert movimiento.monto == Decimal("650.50")
        assert movimiento.banco_emisor == "Banco Nuevo"
        assert movimiento.cuenta_receptora_extraida == "CBU12345"
        assert movimiento.titular == "Juan Perez"
        assert movimiento.factura_o_cuenta_numero == "9999"
        assert movimiento.fecha_transaccion == datetime(2026, 8, 20)


def test_editar_movimiento_allows_blank_monto():
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add(Operator(nombre="Ana", whatsapp_numero="111"))
        session.commit()
        session.add(
            Movement(
                operador_id=1,
                monto=Decimal("500"),
                fecha_transaccion=datetime(2026, 8, 24),
                numero_operacion="OP-1",
                estado_registro=RecordState.CONFIRMADO,
            )
        )
        session.commit()

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    response = client.post(
        "/comprobantes/1/editar",
        data={"fecha_transaccion": "2026-08-24T00:00", "monto": "", "numero_operacion": "OP-1"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with test_session() as session:
        movimiento = session.get(Movement, 1)
        assert movimiento.monto is None


def test_editar_movimiento_allows_blank_numero_operacion_without_colliding():
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add(Operator(nombre="Ana", whatsapp_numero="111"))
        session.commit()
        session.add_all(
            [
                Movement(
                    operador_id=1,
                    monto=Decimal("500"),
                    fecha_transaccion=datetime(2026, 8, 24),
                    numero_operacion="OP-1",
                    estado_registro=RecordState.CONFIRMADO,
                ),
                Movement(
                    operador_id=1,
                    monto=Decimal("300"),
                    fecha_transaccion=datetime(2026, 8, 24),
                    numero_operacion="OP-2",
                    estado_registro=RecordState.CONFIRMADO,
                ),
            ]
        )
        session.commit()

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    for movimiento_id in (1, 2):
        response = client.post(
            f"/comprobantes/{movimiento_id}/editar",
            data={"fecha_transaccion": "2026-08-24T00:00", "monto": "500", "numero_operacion": ""},
            follow_redirects=False,
        )
        assert response.status_code == 303

    with test_session() as session:
        assert session.get(Movement, 1).numero_operacion is None
        assert session.get(Movement, 2).numero_operacion is None


def test_editar_movimiento_rejects_duplicate_numero_operacion():
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add(Operator(nombre="Ana", whatsapp_numero="111"))
        session.commit()
        session.add_all(
            [
                Movement(
                    operador_id=1,
                    monto=Decimal("500"),
                    fecha_transaccion=datetime(2026, 8, 24),
                    numero_operacion="OP-1",
                    estado_registro=RecordState.CONFIRMADO,
                ),
                Movement(
                    operador_id=1,
                    monto=Decimal("300"),
                    fecha_transaccion=datetime(2026, 8, 24),
                    numero_operacion="OP-2",
                    estado_registro=RecordState.CONFIRMADO,
                ),
            ]
        )
        session.commit()

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    response = client.post(
        "/comprobantes/2/editar",
        data={
            "fecha_transaccion": "2026-08-24T00:00",
            "monto": "300",
            "numero_operacion": "OP-1",
        },
    )
    assert response.status_code == 400
    assert "Ya existe otro comprobante" in response.text


def test_crear_movil_ok():
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add(Operator(nombre="Resp", whatsapp_numero="7770001"))
        session.commit()

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    response = client.post(
        "/config/moviles",
        data={"numero": "M-01", "nombre": "Camion 1", "descripcion": "", "responsable_operador_id": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with test_session() as session:
        movil = session.query(Movil).filter_by(numero="M-01").one()
        assert movil.nombre == "Camion 1"
        assert movil.responsable_operador_id == 1


def test_crear_movil_numero_duplicado_rechazado():
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add(Operator(nombre="Resp", whatsapp_numero="7770001"))
        session.commit()

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    client.post("/config/moviles", data={"numero": "M-01", "nombre": "Camion 1", "responsable_operador_id": "1"})

    response = client.post("/config/moviles", data={"numero": "M-01", "nombre": "Otro", "responsable_operador_id": "1"})
    assert response.status_code == 400
    assert "Ya existe un movil" in response.text


def test_crear_movil_sin_responsable_rechazado():
    client, _ = _client_with_admin()
    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})

    response = client.post("/config/moviles", data={"numero": "M-01", "nombre": "Camion 1", "responsable_operador_id": ""})
    assert response.status_code == 400
    assert "necesita un operador responsable" in response.text


def test_crear_movil_responsable_sin_celular_rechazado():
    client, test_session = _client_with_admin()
    with test_session() as session:
        operador = Operator(nombre="SinCelular", whatsapp_numero="")
        session.add(operador)
        session.commit()
        responsable_id = operador.id

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    response = client.post(
        "/config/moviles",
        data={"numero": "M-01", "nombre": "Camion 1", "responsable_operador_id": str(responsable_id)},
    )
    assert response.status_code == 400
    assert "no tiene celular cargado" in response.text


def test_editar_movil_updates_fields():
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add_all(
            [
                Operator(nombre="Resp1", whatsapp_numero="7770001"),
                Operator(nombre="Resp2", whatsapp_numero="7770002"),
            ]
        )
        session.commit()
        session.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
        session.commit()

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    response = client.post(
        "/config/moviles/1/editar",
        data={"numero": "M-01B", "nombre": "Camion Nuevo", "descripcion": "desc", "responsable_operador_id": "2"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with test_session() as session:
        movil = session.get(Movil, 1)
        assert movil.numero == "M-01B"
        assert movil.nombre == "Camion Nuevo"
        assert movil.responsable_operador_id == 2


def test_editar_operador_asigna_y_limpia_movil():
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add(Operator(nombre="Ana", whatsapp_numero="111"))
        session.commit()
        session.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
        session.commit()

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    response = client.post(
        "/config/operadores/1/editar",
        data={"nombre": "Ana", "whatsapp_numero": "111", "tipo": "Reparto", "movil_id": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with test_session() as session:
        assert session.get(Operator, 1).movil_id == 1

    response = client.post(
        "/config/operadores/1/editar",
        data={"nombre": "Ana", "whatsapp_numero": "111", "tipo": "Reparto", "movil_id": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with test_session() as session:
        assert session.get(Operator, 1).movil_id is None


def test_editar_operador_rechaza_celular_vacio_si_es_responsable():
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add(Operator(nombre="Ana", whatsapp_numero="111"))
        session.commit()
        session.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
        session.commit()

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    response = client.post(
        "/config/operadores/1/editar",
        data={"nombre": "Ana", "whatsapp_numero": "", "tipo": "Reparto"},
    )
    assert response.status_code == 400
    assert "responsable del movil" in response.text


def test_toggle_operador_bloqueado_si_es_responsable():
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add(Operator(nombre="Ana", whatsapp_numero="111"))
        session.commit()
        session.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
        session.commit()

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    response = client.post("/config/operadores/1/toggle", follow_redirects=False)

    assert response.status_code == 303
    assert "/config/operadores?error=" in response.headers["location"]
    with test_session() as session:
        assert session.get(Operator, 1).activo is True


def test_comprobantes_filtra_por_movil():
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add(Operator(nombre="Ana", whatsapp_numero="111"))
        session.commit()
        session.add_all(
            [
                Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1),
                Movil(numero="M-02", nombre="Camion 2", responsable_operador_id=1),
            ]
        )
        session.commit()
        session.add_all(
            [
                Movement(
                    operador_id=1,
                    monto=Decimal("500"),
                    fecha_transaccion=datetime(2026, 8, 24),
                    numero_operacion="OP-M1",
                    estado_registro=RecordState.CONFIRMADO,
                    movil_id=1,
                ),
                Movement(
                    operador_id=1,
                    monto=Decimal("300"),
                    fecha_transaccion=datetime(2026, 8, 24),
                    numero_operacion="OP-M2",
                    estado_registro=RecordState.CONFIRMADO,
                    movil_id=2,
                ),
            ]
        )
        session.commit()

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    response = client.get("/comprobantes", params={"movil_id": "1"})

    assert response.status_code == 200
    assert "OP-M1" in response.text
    assert "OP-M2" not in response.text


def test_comprobantes_exportar_incluye_columna_movil():
    client, test_session = _client_with_admin()
    with test_session() as session:
        session.add(Operator(nombre="Ana", whatsapp_numero="111"))
        session.commit()
        session.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
        session.commit()
        session.add(
            Movement(
                operador_id=1,
                monto=Decimal("500"),
                fecha_transaccion=datetime(2026, 8, 24),
                numero_operacion="OP-1",
                estado_registro=RecordState.CONFIRMADO,
                movil_id=1,
            )
        )
        session.commit()

    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    response = client.get("/comprobantes/exportar")

    assert response.status_code == 200
    contenido = response.content.decode("utf-8-sig")
    assert "Movil" in contenido
    assert "M-01" in contenido
