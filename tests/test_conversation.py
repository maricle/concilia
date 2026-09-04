from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.conversation import NO_CUENTA_RECEPTORA_TEXTO, ConversationService, ExtractedTransfer
from app.db import Base, _engine_url
from app.models import BankAccount, Movement, Movil, Operator, RecordState, Reparto


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
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    db.get(Operator, 1).movil_id = 1
    db.commit()
    service = ConversationService(db)

    transfer = ExtractedTransfer(Decimal("1250.50"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    response = service.start_transfer("5491112345678", transfer)
    assert "Responde SI" in response
    service.handle_text("5491112345678", "SI")
    assert "Indica el numero" in service.handle_text("5491112345678", "factura")
    final = service.handle_text("5491112345678", "FAC-9")
    assert "Factura: FAC-9" in final
    assert "Confirma la operacion" in final
    assert service.handle_text("5491112345678", "OK") == "Comprobante registrado correctamente."

    movement = db.query(Movement).one()
    assert movement.estado_registro == RecordState.CONFIRMADO
    assert movement.factura_o_cuenta_numero == "FAC-9"
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
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    db.get(Operator, 1).movil_id = 1
    db.commit()
    service = ConversationService(db)

    transfer = ExtractedTransfer(None, datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    response = service.start_transfer("5491112345678", transfer)
    assert "Monto: no detectado" in response
    assert service.handle_text("5491112345678", "SI")
    service.handle_text("5491112345678", "factura")
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


def test_needs_confirmation_keyboard_tracks_si_no_steps():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    service = ConversationService(db)
    numero = "5491112345678"

    assert service.needs_confirmation_keyboard(numero) is False

    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer(numero, transfer)
    assert service.needs_confirmation_keyboard(numero) is True

    service.handle_text(numero, "SI")
    assert service.needs_confirmation_keyboard(numero) is False  # esperando eleccion factura/cuenta
    assert service.needs_tipo_keyboard(numero) is True

    service.handle_text(numero, "factura")
    assert service.needs_confirmation_keyboard(numero) is False  # esperando texto del numero
    assert service.needs_tipo_keyboard(numero) is False

    service.handle_text(numero, "FAC-9")
    assert service.needs_confirmation_keyboard(numero) is True

    service.handle_text(numero, "OK")
    assert service.needs_confirmation_keyboard(numero) is False


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
    assert "factura o de cuenta" in prompt


def test_pending_prompt_reshows_confirmacion_final():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491112345678", transfer)
    service.handle_text("5491112345678", "SI")
    service.handle_text("5491112345678", "factura")
    service.handle_text("5491112345678", "FAC-9")

    prompt = service.pending_prompt("5491112345678")

    assert prompt is not None
    assert "Confirma la operacion" in prompt
    assert db.query(Movement).count() == 1


def test_iniciar_reparto_asigna_movil_y_crea_reparto():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)

    response = service.handle_text("5491112345678", "inicio movil M-01 reparto nro 5")

    assert "Reparto Nº 5 iniciado" in response
    assert db.get(Operator, 1).movil_id == 1
    reparto = db.query(Reparto).one()
    assert reparto.movil_id == 1
    assert reparto.numero_reparto == 5
    assert reparto.hora_fin is None


def test_iniciar_reparto_con_movil_inexistente():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    service = ConversationService(db)

    response = service.handle_text("5491112345678", "inicio movil M-99 reparto nro 1")

    assert "No encontramos un movil activo" in response
    assert db.query(Reparto).count() == 0


def test_iniciar_reparto_con_reparto_abierto_pregunta_y_cierra():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add_all(
        [
            Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1),
            Movil(numero="M-02", nombre="Camion 2", responsable_operador_id=1),
        ]
    )
    db.commit()
    service = ConversationService(db)

    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 1")
    respuesta = service.handle_text("5491112345678", "inicio movil M-02 reparto nro 2")

    assert "Ya tenes un reparto abierto" in respuesta
    assert "Nº 1" in respuesta
    assert db.get(Operator, 1).movil_id == 1  # todavia no cambio

    respuesta_cerrar = service.handle_text("5491112345678", "cerrar")

    assert "Reparto anterior cerrado" in respuesta_cerrar
    assert "Nº 2" in respuesta_cerrar
    assert db.get(Operator, 1).movil_id == 2
    repartos = db.query(Reparto).order_by(Reparto.id).all()
    assert len(repartos) == 2
    assert repartos[0].hora_fin is not None
    assert repartos[1].movil_id == 2
    assert repartos[1].hora_fin is None


def test_iniciar_reparto_con_reparto_abierto_continuar_no_cambia_nada():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add_all(
        [
            Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1),
            Movil(numero="M-02", nombre="Camion 2", responsable_operador_id=1),
        ]
    )
    db.commit()
    service = ConversationService(db)

    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 1")
    service.handle_text("5491112345678", "inicio movil M-02 reparto nro 2")

    respuesta = service.handle_text("5491112345678", "continuar")

    assert "Seguis con el reparto" in respuesta
    assert db.get(Operator, 1).movil_id == 1
    assert db.query(Reparto).count() == 1


def test_cerrar_reparto_exitoso():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 7")

    respuesta = service.handle_text("5491112345678", "cerrar reparto nro 7")

    assert "Reparto Nº 7 cerrado" in respuesta
    assert db.query(Reparto).one().hora_fin is not None


def test_cerrar_reparto_con_numero_incorrecto_no_cierra():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 7")

    respuesta = service.handle_text("5491112345678", "cerrar reparto nro 9")

    assert "no el 9" in respuesta
    assert "Nº 7" in respuesta
    assert db.query(Reparto).one().hora_fin is None


def test_cerrar_reparto_sin_reparto_abierto():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    service = ConversationService(db)

    respuesta = service.handle_text("5491112345678", "cerrar reparto nro 1")

    assert "No tenes ningun movil asignado" in respuesta


def test_comando_reparto_rechazado_durante_flujo_de_comprobante():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491112345678", transfer)

    respuesta = service.handle_text("5491112345678", "inicio movil M-01 reparto nro 1")

    assert "Todavia tenes un comprobante pendiente" in respuesta
    assert db.query(Reparto).count() == 0


def test_confirmacion_pide_movil_si_operador_no_tiene_asignado():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491112345678", transfer)
    service.handle_text("5491112345678", "SI")
    service.handle_text("5491112345678", "factura")
    service.handle_text("5491112345678", "FAC-9")

    respuesta = service.handle_text("5491112345678", "OK")

    assert "En que movil" in respuesta
    assert db.query(Movement).one().estado_registro == RecordState.PENDIENTE_CONFIRMACION

    respuesta_final = service.handle_text("5491112345678", "M-01")

    assert respuesta_final == "Comprobante registrado correctamente."
    movement = db.query(Movement).one()
    assert movement.estado_registro == RecordState.CONFIRMADO
    assert movement.movil_id == 1
    assert db.get(Operator, 1).movil_id == 1


def test_confirmacion_completa_movil_id_si_operador_ya_tiene_asignado():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    db.get(Operator, 1).movil_id = 1
    db.commit()
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491112345678", transfer)
    service.handle_text("5491112345678", "SI")
    service.handle_text("5491112345678", "factura")
    service.handle_text("5491112345678", "FAC-9")

    respuesta = service.handle_text("5491112345678", "OK")

    assert respuesta == "Comprobante registrado correctamente."
    assert db.query(Movement).one().movil_id == 1


def test_pending_prompt_reshows_decision_reparto_abierto():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add_all(
        [
            Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1),
            Movil(numero="M-02", nombre="Camion 2", responsable_operador_id=1),
        ]
    )
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 1")
    service.handle_text("5491112345678", "inicio movil M-02 reparto nro 2")

    prompt = service.pending_prompt("5491112345678")

    assert prompt is not None
    assert "cerrar" in prompt and "continuar" in prompt


def test_pending_prompt_reshows_esperando_movil():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    db.commit()
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491112345678", transfer)
    service.handle_text("5491112345678", "SI")
    service.handle_text("5491112345678", "factura")
    service.handle_text("5491112345678", "FAC-9")
    service.handle_text("5491112345678", "OK")

    prompt = service.pending_prompt("5491112345678")

    assert prompt is not None
    assert "movil" in prompt.lower()
