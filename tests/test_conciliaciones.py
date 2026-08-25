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
from app.models import BankAccount, Movement, Operator, PanelUser, RecordState, ReconciliationState, StatementLine


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
        session.add(BankAccount(banco="Nacion", numero_cuenta="1", alias="Principal"))
        session.add(Operator(nombre="Ana", whatsapp_numero="111"))
        session.commit()

    return TestClient(app), test_session


def teardown_function():
    app.dependency_overrides.clear()


def _login(client):
    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})


def test_conciliaciones_page_requires_login():
    client, _ = _client_with_admin()
    response = client.get("/conciliaciones", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_importar_resumen_reconciles_matching_movement():
    client, test_session = _client_with_admin()
    _login(client)

    with test_session() as session:
        session.add(
            Movement(
                operador_id=1,
                monto=Decimal("500.00"),
                fecha_transaccion=datetime(2026, 8, 24),
                numero_operacion="OP-1",
                estado_registro=RecordState.CONFIRMADO,
            )
        )
        session.commit()

    csv_contenido = "Fecha,Importe,Descripcion,Referencia\n24/08/2026,500.00,Transferencia,OP-1\n"
    response = client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen.csv", csv_contenido, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with test_session() as session:
        movimiento = session.query(Movement).filter_by(numero_operacion="OP-1").one()
        assert movimiento.estado_conciliacion == ReconciliationState.CONCILIADO
        assert movimiento.cuenta_bancaria_id == 1
        linea = session.query(StatementLine).one()
        assert linea.movimiento_id == movimiento.id


def test_importar_resumen_with_bad_file_shows_error():
    client, _ = _client_with_admin()
    _login(client)

    response = client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen.csv", "Descripcion,Referencia\nAlgo,OP-1\n", "text/csv")},
    )
    assert response.status_code == 400
    assert "columnas de fecha y monto" in response.text


def test_emparejar_linea_manualmente():
    client, test_session = _client_with_admin()
    _login(client)

    with test_session() as session:
        session.add(
            Movement(
                operador_id=1,
                monto=Decimal("480.00"),
                fecha_transaccion=datetime(2026, 8, 20),
                numero_operacion="OP-DISTINTO",
                estado_registro=RecordState.CONFIRMADO,
            )
        )
        session.commit()

    csv_contenido = "Fecha,Importe,Descripcion,Referencia\n24/08/2026,500.00,Transferencia,OP-9\n"
    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen.csv", csv_contenido, "text/csv")},
    )

    with test_session() as session:
        linea = session.query(StatementLine).one()
        movimiento = session.query(Movement).filter_by(numero_operacion="OP-DISTINTO").one()
        linea_id, movimiento_id = linea.id, movimiento.id

    response = client.post(
        f"/conciliaciones/lineas/{linea_id}/emparejar",
        data={"movimiento_id": movimiento_id},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with test_session() as session:
        movimiento = session.get(Movement, movimiento_id)
        assert movimiento.estado_conciliacion == ReconciliationState.CONCILIADO_MANUALMENTE
        assert movimiento.cuenta_bancaria_id == 1


def test_marcar_linea_no_corresponde():
    client, test_session = _client_with_admin()
    _login(client)

    csv_contenido = "Fecha,Importe,Descripcion,Referencia\n24/08/2026,999.00,Deposito ajeno,\n"
    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen.csv", csv_contenido, "text/csv")},
    )

    with test_session() as session:
        linea = session.query(StatementLine).one()
        linea_id = linea.id

    response = client.post(f"/conciliaciones/lineas/{linea_id}/no-corresponde", follow_redirects=False)
    assert response.status_code == 303

    with test_session() as session:
        linea = session.get(StatementLine, linea_id)
        assert linea.estado.value == "no_corresponde"
