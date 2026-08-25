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
from app.models import BankAccount, Movement, Operator, PanelUser, RecordState


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
    assert login.headers["location"] == "/config/operadores"

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
            "fecha_transaccion": "2026-08-20",
            "monto": "650.50",
            "numero_operacion": "OP-1-CORREGIDO",
            "banco_emisor": "Banco Nuevo",
            "cuenta_receptora_extraida": "CBU12345",
            "titular": "Juan Perez",
            "factura_o_cuenta": "9999",
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
        assert movimiento.factura_o_cuenta == "9999"
        assert movimiento.fecha_transaccion == datetime(2026, 8, 20)


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
            "fecha_transaccion": "2026-08-24",
            "monto": "300",
            "numero_operacion": "OP-1",
        },
    )
    assert response.status_code == 400
    assert "Ya existe otro comprobante" in response.text
