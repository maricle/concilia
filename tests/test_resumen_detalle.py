"""Detalle de un resumen importado: todos sus registros (conciliados o no) y la
opcion de quitar una conciliacion para volver a conciliarla."""

from datetime import date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import panel
from app.auth import hash_password
from app.db import Base
from app.main import app
from app.models import (
    BankAccount,
    CierreDiario,
    ImportedStatement,
    Movement,
    Operator,
    PanelUser,
    ReconciliationState,
    RecordState,
    StatementLine,
    StatementLineState,
)

DIA = datetime(2026, 11, 4)


def _cliente():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    fabrica = sessionmaker(bind=engine)

    def override_get_db():
        with fabrica() as session:
            yield session

    app.dependency_overrides[panel.get_db] = override_get_db
    with fabrica() as db:
        db.add(PanelUser(nombre="Admin", email="admin@concilia.test", password_hash=hash_password("secreta123")))
        db.add(BankAccount(banco="Galicia", numero_cuenta="1", alias="galicia.empresa"))
        db.add(Operator(nombre="Ana", whatsapp_numero="111"))
        db.commit()
        db.add(ImportedStatement(cuenta_bancaria_id=1, fecha=DIA, archivo_nombre="galicia.xlsx", formato="xlsx", usuario_id=1))
        conciliado = Movement(
            operador_id=1, cuenta_bancaria_id=1, monto=Decimal("1500"), fecha_transaccion=DIA,
            numero_operacion="OP-1", estado_registro=RecordState.CONFIRMADO,
            estado_conciliacion=ReconciliationState.CONCILIADO,
        )
        db.add(conciliado)
        db.commit()
        db.add_all(
            [
                StatementLine(resumen_id=1, fecha=DIA, monto=Decimal("1500"), referencia="OP-1", titular="PEREZ JUAN",
                              movimiento_id=conciliado.id, estado=StatementLineState.CONCILIADA),
                StatementLine(resumen_id=1, fecha=DIA, monto=Decimal("200"), titular="GOMEZ ANA"),
                StatementLine(resumen_id=1, fecha=DIA, monto=Decimal("50"), descripcion="INTERESES",
                              estado=StatementLineState.NO_CORRESPONDE),
            ]
        )
        db.commit()
    client = TestClient(app)
    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    return client, fabrica


def teardown_function():
    app.dependency_overrides.clear()


def test_el_listado_lleva_al_detalle_con_todos_los_registros():
    client, _ = _cliente()

    listado = client.get("/conciliaciones/resumenes").text
    assert 'href="/conciliaciones/resumenes/1?volver=' in listado

    detalle = client.get("/conciliaciones/resumenes/1").text
    assert "PEREZ JUAN" in detalle and "GOMEZ ANA" in detalle and "INTERESES" in detalle
    assert "Todos (3)" in detalle and "Conciliadas (1)" in detalle and "Sin conciliar (1)" in detalle
    assert "Quitar conciliacion" in detalle and "Volver a sin conciliar" in detalle

    solo_pendientes = client.get("/conciliaciones/resumenes/1", params={"estado": "pendientes"}).text
    assert "GOMEZ ANA" in solo_pendientes and "PEREZ JUAN" not in solo_pendientes


def test_quitar_conciliacion_deja_linea_y_comprobante_sin_conciliar():
    client, fabrica = _cliente()

    respuesta = client.post("/conciliaciones/lineas/1/quitar-conciliacion?volver=/conciliaciones/resumenes/1")

    assert "Se quito la conciliacion" in respuesta.text
    with fabrica() as db:
        linea = db.get(StatementLine, 1)
        assert (linea.estado, linea.movimiento_id) == (StatementLineState.PENDIENTE, None)
        movimiento = db.get(Movement, 1)
        assert movimiento.estado_conciliacion == ReconciliationState.PENDIENTE
        assert movimiento.cuenta_bancaria_id == 1  # la cuenta asignada no se toca


def test_no_corresponde_vuelve_a_sin_conciliar():
    client, fabrica = _cliente()
    client.post("/conciliaciones/lineas/3/volver-a-pendiente")
    with fabrica() as db:
        assert db.get(StatementLine, 3).estado == StatementLineState.PENDIENTE


def test_con_el_dia_cerrado_no_se_puede_quitar():
    client, fabrica = _cliente()
    with fabrica() as db:
        db.add(CierreDiario(fecha=date(2026, 11, 4), cerrado_por_id=1))
        db.commit()

    detalle = client.get("/conciliaciones/resumenes/1").text
    assert "esta cerrado" in detalle and "Quitar conciliacion</button>" not in detalle

    respuesta = client.post("/conciliaciones/lineas/1/quitar-conciliacion?volver=/conciliaciones/resumenes/1")
    assert "ya esta cerrado" in respuesta.text
    with fabrica() as db:
        assert db.get(StatementLine, 1).estado == StatementLineState.CONCILIADA
        assert db.get(Movement, 1).estado_conciliacion == ReconciliationState.CONCILIADO
