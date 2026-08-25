import io
from datetime import datetime
from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import BankAccount, ImportedStatement, Movement, Operator, RecordState, ReconciliationState, StatementLine, StatementLineState
from app.reconciliation import StatementParseError, match_statement, parse_csv, parse_xlsx


def test_parse_csv_with_spanish_decimal_comma():
    contenido = (
        "Fecha,Importe,Descripcion,Referencia\n"
        "24/08/2026,\"1.234,56\",Transferencia recibida,OP-1\n"
    ).encode("utf-8")

    filas = parse_csv(contenido)

    assert len(filas) == 1
    assert filas[0].fecha == datetime(2026, 8, 24)
    assert filas[0].monto == Decimal("1234.56")
    assert filas[0].referencia == "OP-1"


def test_parse_csv_missing_required_columns_raises():
    contenido = "Descripcion,Referencia\nAlgo,OP-1\n".encode("utf-8")

    with pytest.raises(StatementParseError):
        parse_csv(contenido)


def test_parse_xlsx_reads_header_and_rows():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Fecha", "Monto", "Concepto", "Nro Operacion"])
    sheet.append([datetime(2026, 8, 24), 500.0, "Transferencia", "OP-9"])
    buffer = io.BytesIO()
    workbook.save(buffer)

    filas = parse_xlsx(buffer.getvalue())

    assert len(filas) == 1
    assert filas[0].monto == Decimal("500.0")
    assert filas[0].referencia == "OP-9"


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine)
    with test_session() as db:
        yield db


def _crear_movimiento(db, **overrides) -> Movement:
    operador = db.query(Operator).first()
    if operador is None:
        operador = Operator(nombre="Ana", whatsapp_numero="111")
        db.add(operador)
        db.flush()
    defaults = dict(
        operador_id=operador.id,
        fecha_transaccion=datetime(2026, 8, 24),
        monto=Decimal("500.00"),
        numero_operacion="OP-1",
        estado_registro=RecordState.CONFIRMADO,
        estado_conciliacion=ReconciliationState.PENDIENTE,
    )
    defaults.update(overrides)
    movimiento = Movement(**defaults)
    db.add(movimiento)
    db.flush()
    return movimiento


def _crear_resumen_y_linea(db, cuenta_id, **overrides) -> tuple[ImportedStatement, StatementLine]:
    resumen = ImportedStatement(
        cuenta_bancaria_id=cuenta_id,
        fecha=datetime(2026, 8, 24),
        archivo_nombre="resumen.csv",
        formato="csv",
        usuario_id=1,
    )
    db.add(resumen)
    db.flush()
    defaults = dict(
        resumen_id=resumen.id,
        resumen=resumen,
        fecha=datetime(2026, 8, 24),
        monto=Decimal("500.00"),
        referencia="OP-1",
    )
    defaults.update(overrides)
    linea = StatementLine(**defaults)
    db.add(linea)
    db.flush()
    return resumen, linea


def test_single_exact_candidate_is_reconciled_automatically(session):
    cuenta = BankAccount(banco="Nacion", numero_cuenta="1", alias="Principal")
    session.add(cuenta)
    session.flush()
    movimiento = _crear_movimiento(session)
    resumen, linea = _crear_resumen_y_linea(session, cuenta.id)

    match_statement(session, resumen, [linea])

    assert linea.estado == StatementLineState.CONCILIADA
    assert linea.movimiento_id == movimiento.id
    assert movimiento.estado_conciliacion == ReconciliationState.CONCILIADO
    assert movimiento.cuenta_bancaria_id == cuenta.id


def test_ambiguous_candidates_without_reference_stay_pending(session):
    cuenta = BankAccount(banco="Nacion", numero_cuenta="1", alias="Principal")
    session.add(cuenta)
    session.flush()
    _crear_movimiento(session, numero_operacion="OP-1")
    _crear_movimiento(session, numero_operacion="OP-2")
    resumen, linea = _crear_resumen_y_linea(session, cuenta.id, referencia=None)

    match_statement(session, resumen, [linea])

    assert linea.estado == StatementLineState.PENDIENTE
    assert linea.movimiento_id is None


def test_reference_disambiguates_same_amount_and_date(session):
    cuenta = BankAccount(banco="Nacion", numero_cuenta="1", alias="Principal")
    session.add(cuenta)
    session.flush()
    _crear_movimiento(session, numero_operacion="OP-1")
    objetivo = _crear_movimiento(session, numero_operacion="OP-2")
    resumen, linea = _crear_resumen_y_linea(session, cuenta.id, referencia="OP-2")

    match_statement(session, resumen, [linea])

    assert linea.movimiento_id == objetivo.id
    assert objetivo.estado_conciliacion == ReconciliationState.CONCILIADO


def test_no_candidate_leaves_line_pending(session):
    cuenta = BankAccount(banco="Nacion", numero_cuenta="1", alias="Principal")
    session.add(cuenta)
    session.flush()
    resumen, linea = _crear_resumen_y_linea(session, cuenta.id)

    match_statement(session, resumen, [linea])

    assert linea.estado == StatementLineState.PENDIENTE
    assert linea.movimiento_id is None


def test_matching_reference_with_different_amount_is_flagged_con_diferencia(session):
    cuenta = BankAccount(banco="Nacion", numero_cuenta="1", alias="Principal")
    session.add(cuenta)
    session.flush()
    movimiento = _crear_movimiento(session, numero_operacion="OP-1", monto=Decimal("480.00"))
    resumen, linea = _crear_resumen_y_linea(session, cuenta.id, monto=Decimal("500.00"), referencia="OP-1")

    match_statement(session, resumen, [linea])

    assert linea.movimiento_id == movimiento.id
    assert linea.estado == StatementLineState.CONCILIADA
    assert movimiento.estado_conciliacion == ReconciliationState.CON_DIFERENCIA
