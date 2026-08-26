from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.conversation import NO_CUENTA_RECEPTORA_TEXTO, ConversationService, ExtractedTransfer
from app.db import Base, _engine_url
from app.models import BankAccount, Movement, Operator, RecordState


def session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def _con_cuenta_registrada(db: Session, alias: str = "empresa.mp") -> None:
    db.add(BankAccount(banco="Mercado Pago", numero_cuenta="123-456", alias=alias))
    db.commit()


def test_registered_operator_can_confirm_and_register_transfer():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    service = ConversationService(db)

    transfer = ExtractedTransfer(Decimal("1250.50"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    response = service.start_transfer("5491112345678", transfer)
    assert "Responde SI" in response
    assert "Indica el numero" in service.handle_text("5491112345678", "SI")
    final = service.handle_text("5491112345678", "FAC-9")
    assert "Factura/cuenta: FAC-9" in final
    assert "Confirma la operacion" in final
    assert service.handle_text("5491112345678", "OK") == "Comprobante registrado correctamente."

    movement = db.query(Movement).one()
    assert movement.estado_registro == RecordState.CONFIRMADO
    assert movement.factura_o_cuenta == "FAC-9"
    assert movement.cuenta_bancaria_id == 1


def test_unknown_operator_is_rejected_before_processing():
    db = session()
    assert ConversationService(db).handle_text("5491100000000", "hola") == "Este numero no esta habilitado para registrar comprobantes."


def test_duplicate_operation_is_rejected():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.add(Movement(operador_id=1, monto=Decimal("10"), fecha_transaccion=datetime.now(), numero_operacion="OP-1"))
    _con_cuenta_registrada(db)
    transfer = ExtractedTransfer(Decimal("10"), datetime.now(), "OP-1", cuenta_receptora="empresa.mp")
    response = ConversationService(db).start_transfer("5491112345678", transfer)
    assert "Ya existe" in response


def test_missing_numero_operacion_does_not_collide_between_movements():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    service = ConversationService(db)

    sin_numero = ExtractedTransfer(Decimal("10"), datetime.now(), None, cuenta_receptora="empresa.mp")
    primera = service.start_transfer("5491112345678", sin_numero)
    assert "Responde SI" in primera
    service.handle_text("5491112345678", "SI")
    service.handle_text("5491112345678", "FAC-1")
    service.handle_text("5491112345678", "OK")

    segunda = service.start_transfer("5491112345678", sin_numero)
    assert "Ya existe" not in segunda
    assert "Responde SI" in segunda

    assert db.query(Movement).count() == 2


def test_transfer_with_unreadable_monto_is_registered_anyway():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    service = ConversationService(db)

    transfer = ExtractedTransfer(None, datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    response = service.start_transfer("5491112345678", transfer)
    assert "Monto: no detectado" in response
    assert service.handle_text("5491112345678", "SI")
    service.handle_text("5491112345678", "FAC-9")
    assert service.handle_text("5491112345678", "OK") == "Comprobante registrado correctamente."

    movement = db.query(Movement).one()
    assert movement.estado_registro == RecordState.CONFIRMADO
    assert movement.monto is None


def test_transfer_without_matching_cuenta_receptora_is_rejected():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db, alias="otra.cuenta")
    service = ConversationService(db)

    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="no.coincide")
    response = service.start_transfer("5491112345678", transfer)

    assert response == NO_CUENTA_RECEPTORA_TEXTO
    assert db.query(Movement).count() == 0


def test_transfer_matches_cuenta_receptora_by_numero_cuenta_digits():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.add(BankAccount(banco="Nacion", numero_cuenta="0110599520000012345678", alias="cuenta-nacion"))
    db.commit()
    service = ConversationService(db)

    transfer = ExtractedTransfer(
        Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="0110 5995 2000 0012 3456 78"
    )
    response = service.start_transfer("5491112345678", transfer)

    assert "Responde SI" in response
    assert db.query(Movement).one().cuenta_bancaria_id == 1


def test_postgres_urls_use_psycopg_driver():
    assert _engine_url("postgres://user:pass@localhost/db") == "postgresql+psycopg://user:pass@localhost/db"
    assert _engine_url("postgresql://user:pass@localhost/db") == "postgresql+psycopg://user:pass@localhost/db"


def test_pending_prompt_is_none_when_no_draft_in_progress():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    assert ConversationService(db).pending_prompt("5491112345678") is None


def test_pending_prompt_reshows_confirmacion_datos():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491112345678", transfer)

    prompt = service.pending_prompt("5491112345678")

    assert prompt is not None
    assert "Responde SI" in prompt
    assert db.query(Movement).count() == 1


def test_pending_prompt_reshows_cuenta_factura_step():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491112345678", transfer)
    service.handle_text("5491112345678", "SI")

    prompt = service.pending_prompt("5491112345678")

    assert prompt is not None
    assert "numero de cuenta o factura" in prompt


def test_pending_prompt_reshows_confirmacion_final():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491112345678", transfer)
    service.handle_text("5491112345678", "SI")
    service.handle_text("5491112345678", "FAC-9")

    prompt = service.pending_prompt("5491112345678")

    assert prompt is not None
    assert "Confirma la operacion" in prompt
    assert db.query(Movement).count() == 1
