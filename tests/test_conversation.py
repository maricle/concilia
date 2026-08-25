from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.conversation import ConversationService, ExtractedTransfer
from app.db import Base, _engine_url
from app.models import Movement, Operator, RecordState


def session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def test_registered_operator_can_confirm_and_register_transfer():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    service = ConversationService(db)

    response = service.start_transfer("5491112345678", ExtractedTransfer(Decimal("1250.50"), datetime(2026, 8, 21), "OP-1"))
    assert "Responde SI" in response
    assert "Indica el numero" in service.handle_text("5491112345678", "SI")
    assert "factura/cuenta: FAC-9" in service.handle_text("5491112345678", "FAC-9")
    assert service.handle_text("5491112345678", "OK") == "Comprobante registrado correctamente."

    movement = db.query(Movement).one()
    assert movement.estado_registro == RecordState.CONFIRMADO
    assert movement.factura_o_cuenta == "FAC-9"


def test_unknown_operator_is_rejected_before_processing():
    db = session()
    assert ConversationService(db).handle_text("5491100000000", "hola") == "Este numero no esta habilitado para registrar comprobantes."


def test_duplicate_operation_is_rejected():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.add(Movement(operador_id=1, monto=Decimal("10"), fecha_transaccion=datetime.now(), numero_operacion="OP-1"))
    db.commit()
    response = ConversationService(db).start_transfer("5491112345678", ExtractedTransfer(Decimal("10"), datetime.now(), "OP-1"))
    assert "Ya existe" in response


def test_transfer_with_unreadable_monto_is_registered_anyway():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    service = ConversationService(db)

    response = service.start_transfer("5491112345678", ExtractedTransfer(None, datetime(2026, 8, 21), "OP-1"))
    assert "Monto: no detectado" in response
    assert service.handle_text("5491112345678", "SI")
    service.handle_text("5491112345678", "FAC-9")
    assert service.handle_text("5491112345678", "OK") == "Comprobante registrado correctamente."

    movement = db.query(Movement).one()
    assert movement.estado_registro == RecordState.CONFIRMADO
    assert movement.monto is None


def test_postgres_urls_use_psycopg_driver():
    assert _engine_url("postgres://user:pass@localhost/db") == "postgresql+psycopg://user:pass@localhost/db"
    assert _engine_url("postgresql://user:pass@localhost/db") == "postgresql+psycopg://user:pass@localhost/db"
