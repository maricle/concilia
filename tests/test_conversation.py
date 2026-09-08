from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.conversation import NO_CUENTA_RECEPTORA_TEXTO, ConversationService, ExtractedTransfer
from app.db import Base, _engine_url
from app.models import (
    BankAccount,
    ConversationState,
    Movement,
    Movil,
    Operator,
    RecordState,
    Reparto,
    RepartoOperador,
    WhatsAppConversation,
)


def session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def _con_cuenta_registrada(db: Session, alias: str = "empresa.mp") -> None:
    db.add(BankAccount(banco="Mercado Pago", numero_cuenta="123-456", alias=alias))
    db.commit()


def _asociar_a_reparto_abierto(db: Session, operador_id: int, movil_id: int, numero_reparto: int = 1) -> Reparto:
    """Atajo de test: crea un reparto abierto en el movil dado y asocia al
    operador, sin pasar por el intercambio de mensajes del comando 'iniciar'."""
    reparto = Reparto(movil_id=movil_id, fecha=datetime.now().date(), hora_inicio=datetime.now(), numero_reparto=numero_reparto)
    db.add(reparto)
    db.flush()
    db.add(RepartoOperador(reparto_id=reparto.id, operador_id=operador_id))
    db.get(Operator, operador_id).movil_id = movil_id
    db.commit()
    return reparto


def test_registered_operator_can_confirm_and_register_transfer():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    _asociar_a_reparto_abierto(db, operador_id=1, movil_id=1)
    service = ConversationService(db)

    transfer = ExtractedTransfer(Decimal("1250.50"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    response = service.start_transfer("5491112345678", transfer)
    assert "factura o un numero de cuenta" in response
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
    assert "factura o un numero de cuenta" in primera
    service.handle_text("5491112345678", "factura")
    service.handle_text("5491112345678", "FAC-1")
    service.handle_text("5491112345678", "OK")

    segunda = service.start_transfer("5491112345678", sin_numero)
    assert "Ya existe" not in segunda
    assert "factura o un numero de cuenta" in segunda

    assert db.query(Movement).count() == 2


def test_transfer_with_unreadable_monto_is_registered_anyway():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    _asociar_a_reparto_abierto(db, operador_id=1, movil_id=1)
    service = ConversationService(db)

    transfer = ExtractedTransfer(None, datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    response = service.start_transfer("5491112345678", transfer)
    assert "Monto: no detectado" in response
    service.handle_text("5491112345678", "factura")
    service.handle_text("5491112345678", "FAC-9")
    assert service.handle_text("5491112345678", "OK") == "Comprobante registrado correctamente."

    movement = db.query(Movement).one()
    assert movement.estado_registro == RecordState.CONFIRMADO
    assert movement.monto is None


def test_transfer_without_matching_cuenta_receptora_is_rejected_if_none_registered():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    service = ConversationService(db)

    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="no.coincide")
    response = service.start_transfer("5491112345678", transfer)

    assert response == NO_CUENTA_RECEPTORA_TEXTO
    assert db.query(Movement).count() == 0


def test_transfer_without_matching_cuenta_receptora_ofrece_elegir():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db, alias="otra.cuenta")
    service = ConversationService(db)

    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="no.coincide")
    response = service.start_transfer("5491112345678", transfer)

    assert "No pudimos identificar a que cuenta corresponde este pago" in response
    assert "otra.cuenta" in response
    movement = db.query(Movement).one()
    assert movement.cuenta_bancaria_id is None

    respuesta_final = service.handle_text("5491112345678", "otra.cuenta")

    assert "factura o un numero de cuenta" in respuesta_final
    assert db.query(Movement).one().cuenta_bancaria_id == 1


def test_prompt_elegir_cuenta_bancaria_muestra_el_banco_no_solo_el_alias():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.add(BankAccount(banco="Galicia", numero_cuenta="0070077120000014194391", alias="el.paquete.llega"))
    db.commit()
    service = ConversationService(db)

    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="no.coincide")
    response = service.start_transfer("5491112345678", transfer)

    # el alias interno puede no decir nada del banco -- tiene que listarse el banco.
    assert "Galicia (el.paquete.llega)" in response


def test_eleccion_de_cuenta_bancaria_se_puede_elegir_por_nombre_del_banco():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.add(BankAccount(banco="Galicia", numero_cuenta="0070077120000014194391", alias="el.paquete.llega"))
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="no.coincide")
    service.start_transfer("5491112345678", transfer)

    respuesta = service.handle_text("5491112345678", "galicia")

    assert "factura o un numero de cuenta" in respuesta
    assert db.query(Movement).one().cuenta_bancaria_id == 1


def test_eleccion_de_cuenta_bancaria_ambigua_por_banco_repetido_no_matchea():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.add_all(
        [
            BankAccount(banco="Galicia", numero_cuenta="111", alias="galicia.pesos"),
            BankAccount(banco="Galicia", numero_cuenta="222", alias="galicia.dolares"),
        ]
    )
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="no.coincide")
    service.start_transfer("5491112345678", transfer)

    respuesta = service.handle_text("5491112345678", "galicia")

    # hay dos cuentas de Galicia -- "galicia" solo no alcanza para elegir una.
    assert "Esa no es una de las opciones" in respuesta
    assert db.query(Movement).one().cuenta_bancaria_id is None

    respuesta_ok = service.handle_text("5491112345678", "galicia.dolares")
    assert "factura o un numero de cuenta" in respuesta_ok


def test_eleccion_de_cuenta_bancaria_invalida_vuelve_a_pedir():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db, alias="otra.cuenta")
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="no.coincide")
    service.start_transfer("5491112345678", transfer)

    respuesta = service.handle_text("5491112345678", "cuenta que no existe")

    assert "Esa no es una de las opciones" in respuesta
    assert db.query(Movement).one().cuenta_bancaria_id is None


def test_eleccion_de_cuenta_bancaria_se_puede_cancelar():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db, alias="otra.cuenta")
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="no.coincide")
    service.start_transfer("5491112345678", transfer)

    respuesta = service.handle_text("5491112345678", "cancelar")

    assert "Registro descartado" in respuesta
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

    assert "factura o un numero de cuenta" in response
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


def test_pending_prompt_reshows_cuenta_factura_step():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491112345678", transfer)

    prompt = service.pending_prompt("5491112345678")

    assert prompt is not None
    assert "factura o de cuenta" in prompt
    assert db.query(Movement).count() == 1


def test_pending_prompt_reshows_confirmacion_final():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491112345678", transfer)
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

    respuesta = service.handle_text("5491112345678", "inicio movil M-01 reparto nro 5")
    assert "Vas a iniciar la Salida Nº 5 en el movil M-01" in respuesta
    assert db.query(Reparto).count() == 0

    response = service.handle_text("5491112345678", "SI")

    assert "Salida Nº 5 iniciada" in response
    assert db.get(Operator, 1).movil_id == 1
    reparto = db.query(Reparto).one()
    assert reparto.movil_id == 1
    assert reparto.numero_reparto == 5
    assert reparto.hora_fin is None


def test_iniciar_reparto_se_asocia_directo_si_el_movil_ya_tiene_uno_abierto():
    db = session()
    db.add_all(
        [
            Operator(nombre="Ana", whatsapp_numero="5491112345678"),
            Operator(nombre="Beto", whatsapp_numero="5491100000000"),
        ]
    )
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 5")
    service.handle_text("5491112345678", "SI")

    respuesta = service.handle_text("5491100000000", "inicio movil M-01 reparto nro 9")

    # se asocia directo al reparto Nº 5 que ya estaba abierto, sin pedir confirmacion
    # y sin importar que Beto haya tipeado el numero 9.
    assert respuesta == "Te asociaste a la Salida Nº 5 en el movil M-01."
    assert db.query(Reparto).count() == 1
    beto = db.scalar(select(Operator).where(Operator.whatsapp_numero == "5491100000000"))
    assert beto.movil_id == 1
    reparto = db.query(Reparto).one()
    asociados = db.scalars(select(RepartoOperador).where(RepartoOperador.reparto_id == reparto.id)).all()
    assert {a.operador_id for a in asociados} == {1, 2}


def test_iniciar_reparto_ya_asociado_al_mismo_movil_solo_avisa():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 5")
    service.handle_text("5491112345678", "SI")

    respuesta = service.handle_text("5491112345678", "inicio movil M-01 reparto nro 5")

    assert respuesta == "Ya estas asociado a la Salida Nº 5 en el movil M-01."
    assert db.query(Reparto).count() == 1


def test_cerrar_reparto_lo_puede_cerrar_cualquier_operador_asociado():
    db = session()
    db.add_all(
        [
            Operator(nombre="Ana", whatsapp_numero="5491112345678"),
            Operator(nombre="Beto", whatsapp_numero="5491100000000"),
        ]
    )
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 5")
    service.handle_text("5491112345678", "SI")
    service.handle_text("5491100000000", "inicio movil M-01 reparto nro 5")  # Beto se asocia

    respuesta = service.handle_text("5491100000000", "cerrar reparto nro 5")

    assert "Salida Nº 5 cerrada" in respuesta
    reparto = db.query(Reparto).one()
    assert reparto.hora_fin is not None
    # queda cerrado para los dos, no solo para quien lo cerro
    ana = db.scalar(select(Operator).where(Operator.whatsapp_numero == "5491112345678"))
    beto = db.scalar(select(Operator).where(Operator.whatsapp_numero == "5491100000000"))
    assert ConversationService(db)._reparto_abierto_de_operador(ana.id) is None
    assert ConversationService(db)._reparto_abierto_de_operador(beto.id) is None


def test_iniciar_reparto_confirmacion_no_cancela():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 5")

    respuesta = service.handle_text("5491112345678", "NO")

    assert "Inicio de salida cancelado" in respuesta
    assert db.query(Reparto).count() == 0
    assert db.get(Operator, 1).movil_id is None


def test_iniciar_reparto_acepta_numero_de_movil_sin_prefijo_m():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)

    respuesta = service.handle_text("5491112345678", "inicio movil 1 reparto nro 5")

    assert "Vas a iniciar la Salida Nº 5 en el movil M-01" in respuesta
    service.handle_text("5491112345678", "SI")
    assert db.query(Reparto).one().movil_id == 1


def test_iniciar_reparto_con_movil_inexistente():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    service = ConversationService(db)

    response = service.handle_text("5491112345678", "inicio movil M-99 reparto nro 1")

    assert "No encontramos un movil activo" in response
    assert db.query(Reparto).count() == 0


def test_iniciar_bare_con_reparto_ya_abierto_avisa_en_vez_de_pedir_datos():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 5")
    service.handle_text("5491112345678", "SI")

    respuesta = service.handle_text("5491112345678", "iniciar")

    assert "Ya tenes la Salida Nº 5 iniciada en el movil M-01" in respuesta
    # no debe haber entrado a pedir datos para un reparto nuevo
    assert db.get(WhatsAppConversation, "5491112345678").estado == ConversationState.ESPERANDO_COMPROBANTE


def test_iniciar_con_solo_movil_y_reparto_ya_abierto_avisa_en_vez_de_pedir_numero():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 5")
    service.handle_text("5491112345678", "SI")

    respuesta = service.handle_text("5491112345678", "iniciar movil M-01")

    assert "Ya tenes la Salida Nº 5 iniciada en el movil M-01" in respuesta


def test_iniciar_reparto_solo_palabra_clave_pide_movil_y_luego_numero():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)

    respuesta = service.handle_text("5491112345678", "inicio")
    assert "En que movil" in respuesta

    respuesta = service.handle_text("5491112345678", "M-01")
    assert "numero de salida" in respuesta

    respuesta = service.handle_text("5491112345678", "5")
    assert "Vas a iniciar la Salida Nº 5 en el movil M-01" in respuesta
    assert db.query(Reparto).count() == 0

    respuesta = service.handle_text("5491112345678", "SI")
    assert "Salida Nº 5 iniciada" in respuesta
    reparto = db.query(Reparto).one()
    assert reparto.movil_id == 1
    assert reparto.numero_reparto == 5


def test_iniciar_reparto_con_iniciar_solo_falta_numero_de_reparto():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)

    respuesta = service.handle_text("5491112345678", "iniciar movil M-01")
    assert "numero de salida" in respuesta

    respuesta = service.handle_text("5491112345678", "7")
    assert "Vas a iniciar la Salida Nº 7" in respuesta

    respuesta = service.handle_text("5491112345678", "SI")
    assert "Salida Nº 7 iniciada" in respuesta
    assert db.query(Reparto).one().numero_reparto == 7


def test_iniciar_reparto_solo_falta_movil():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)

    respuesta = service.handle_text("5491112345678", "inicio reparto nro 3")
    assert "En que movil" in respuesta

    respuesta = service.handle_text("5491112345678", "M-01")
    assert "Vas a iniciar la Salida Nº 3" in respuesta

    respuesta = service.handle_text("5491112345678", "SI")
    assert "Salida Nº 3 iniciada" in respuesta
    assert db.query(Reparto).one().numero_reparto == 3


def test_iniciar_reparto_orden_invertido_movil_y_reparto():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)

    respuesta = service.handle_text("5491112345678", "iniciar reparto nro 4 movil M-01")
    assert "Vas a iniciar la Salida Nº 4" in respuesta

    respuesta = service.handle_text("5491112345678", "SI")
    assert "Salida Nº 4 iniciada" in respuesta
    assert db.query(Reparto).one().numero_reparto == 4


def test_iniciar_reparto_numero_de_reparto_invalido_vuelve_a_pedir():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01")

    respuesta = service.handle_text("5491112345678", "cinco")

    assert "no es un numero de salida valido" in respuesta
    assert db.query(Reparto).count() == 0

    respuesta_ok = service.handle_text("5491112345678", "5")
    assert "Vas a iniciar la Salida Nº 5" in respuesta_ok

    respuesta_final = service.handle_text("5491112345678", "SI")
    assert "Salida Nº 5 iniciada" in respuesta_final


def test_pending_prompt_reshows_dato_faltante_inicio_reparto():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio")

    prompt = service.pending_prompt("5491112345678")

    assert prompt is not None
    assert "En que movil" in prompt


def test_pending_prompt_reshows_confirmacion_inicio_reparto():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 5")

    prompt = service.pending_prompt("5491112345678")

    assert prompt is not None
    assert "Vas a iniciar la Salida Nº 5 en el movil M-01" in prompt


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
    service.handle_text("5491112345678", "SI")
    respuesta = service.handle_text("5491112345678", "inicio movil M-02 reparto nro 2")

    assert "Ya tenes una salida abierta" in respuesta
    assert "Nº 1" in respuesta
    assert db.get(Operator, 1).movil_id == 1  # todavia no cambio

    respuesta_cerrar = service.handle_text("5491112345678", "cerrar")

    assert "Salida anterior cerrada" in respuesta_cerrar
    assert "Nº 2" in respuesta_cerrar
    assert db.get(Operator, 1).movil_id == 2
    repartos = db.query(Reparto).order_by(Reparto.id).all()
    assert len(repartos) == 2
    assert repartos[0].hora_fin is not None
    assert repartos[1].movil_id == 2
    assert repartos[1].hora_fin is None


def test_cerrar_y_arrancar_se_asocia_si_el_movil_nuevo_ya_tiene_reparto_abierto():
    db = session()
    db.add_all(
        [
            Operator(nombre="Ana", whatsapp_numero="5491112345678"),
            Operator(nombre="Beto", whatsapp_numero="5491100000000"),
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
    service = ConversationService(db)
    # Ana en M-01, Beto en M-02 -- los dos repartos abiertos en simultaneo.
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 1")
    service.handle_text("5491112345678", "SI")
    service.handle_text("5491100000000", "inicio movil M-02 reparto nro 2")
    service.handle_text("5491100000000", "SI")

    # Ana quiere pasarse al M-02, que Beto ya tiene abierto.
    service.handle_text("5491112345678", "inicio movil M-02 reparto nro 9")
    respuesta = service.handle_text("5491112345678", "cerrar")

    assert respuesta == "Salida anterior cerrada. Te asociaste a la Salida Nº 2 en el movil M-02."
    assert db.query(Reparto).count() == 2  # no se creo un tercero
    reparto_m02 = db.query(Reparto).where(Reparto.movil_id == 2).one()
    asociados = db.scalars(select(RepartoOperador).where(RepartoOperador.reparto_id == reparto_m02.id)).all()
    assert {a.operador_id for a in asociados} == {1, 2}


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
    service.handle_text("5491112345678", "SI")
    service.handle_text("5491112345678", "inicio movil M-02 reparto nro 2")

    respuesta = service.handle_text("5491112345678", "continuar")

    assert "Seguis con la salida" in respuesta
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
    service.handle_text("5491112345678", "SI")

    respuesta = service.handle_text("5491112345678", "cerrar reparto nro 7")

    assert "Salida Nº 7 cerrada" in respuesta
    assert db.query(Reparto).one().hora_fin is not None


def test_cerrar_reparto_con_numero_incorrecto_no_cierra():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 7")
    service.handle_text("5491112345678", "SI")

    respuesta = service.handle_text("5491112345678", "cerrar reparto nro 9")

    assert "no la 9" in respuesta
    assert "Nº 7" in respuesta


def test_cerrar_reparto_acepta_palabra_sola_sin_numero():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 7")
    service.handle_text("5491112345678", "SI")

    respuesta = service.handle_text("5491112345678", "cerrar")

    assert "Salida Nº 7 cerrada" in respuesta
    assert db.query(Reparto).one().hora_fin is not None


def test_cerrar_reparto_acepta_fin():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 7")
    service.handle_text("5491112345678", "SI")

    respuesta = service.handle_text("5491112345678", "fin de reparto")

    assert "Salida Nº 7 cerrada" in respuesta
    assert db.query(Reparto).one().hora_fin is not None


def test_cerrar_reparto_notifica_a_los_demas_operadores_asociados():
    db = session()
    db.add_all(
        [
            Operator(nombre="Ana", whatsapp_numero="5491112345678", telegram_chat_id="111"),
            Operator(nombre="Beto", whatsapp_numero="5491100000000", telegram_chat_id="222"),
        ]
    )
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 7")
    service.handle_text("5491112345678", "SI")
    service.handle_text("5491100000000", "inicio movil M-01 reparto nro 7")  # Beto se asocia

    service.handle_text("5491100000000", "cerrar")

    notificaciones = service.pop_notificaciones()
    assert notificaciones == [("5491112345678", "La Salida Nº 7 en el movil M-01 fue cerrada por Beto.")]


def test_cerrar_reparto_encola_pdf_de_resumen_para_cada_asociado():
    db = session()
    db.add_all(
        [
            Operator(nombre="Ana", whatsapp_numero="5491112345678", telegram_chat_id="111"),
            Operator(nombre="Beto", whatsapp_numero="5491100000000", telegram_chat_id="222"),
        ]
    )
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 7")
    service.handle_text("5491112345678", "SI")
    service.handle_text("5491100000000", "inicio movil M-01 reparto nro 7")  # Beto se asocia

    service.handle_text("5491100000000", "cerrar")

    documentos = service.pop_documentos()
    numeros = {numero for numero, _, _ in documentos}
    assert numeros == {"5491112345678", "5491100000000"}
    for _, nombre_archivo, contenido in documentos:
        assert nombre_archivo == "salida_7_M-01.pdf"
        assert contenido[:4] == b"%PDF"


def test_cerrar_reparto_sin_otros_asociados_no_genera_notificaciones():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 7")
    service.handle_text("5491112345678", "SI")

    service.handle_text("5491112345678", "cerrar")

    assert service.pop_notificaciones() == []
    assert db.query(Reparto).one().hora_fin is not None


def test_cerrar_reparto_sin_reparto_abierto():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    service = ConversationService(db)

    respuesta = service.handle_text("5491112345678", "cerrar reparto nro 1")

    assert "No tenes ninguna salida abierta" in respuesta


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

    respuesta_movil = service.handle_text("5491112345678", "M-01")
    assert "No hay ninguna salida abierta en el movil M-01" in respuesta_movil
    assert db.query(Movement).one().estado_registro == RecordState.PENDIENTE_CONFIRMACION

    respuesta_numero = service.handle_text("5491112345678", "SI")
    assert "Que numero de salida es" in respuesta_numero

    respuesta_final = service.handle_text("5491112345678", "8")

    assert respuesta_final == "Comprobante registrado correctamente. Se inicio la Salida Nº 8 en el movil M-01."
    movement = db.query(Movement).one()
    assert movement.estado_registro == RecordState.CONFIRMADO
    assert movement.movil_id == 1
    assert db.get(Operator, 1).movil_id == 1
    assert db.query(Reparto).one().numero_reparto == 8


def test_numero_de_reparto_nuevo_invalido_vuelve_a_pedir():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491112345678", transfer)
    service.handle_text("5491112345678", "factura")
    service.handle_text("5491112345678", "FAC-9")
    service.handle_text("5491112345678", "OK")
    service.handle_text("5491112345678", "M-01")
    service.handle_text("5491112345678", "SI")

    respuesta = service.handle_text("5491112345678", "ocho")

    assert "no es un numero de salida valido" in respuesta
    assert db.query(Reparto).count() == 0

    respuesta_ok = service.handle_text("5491112345678", "8")
    assert respuesta_ok == "Comprobante registrado correctamente. Se inicio la Salida Nº 8 en el movil M-01."


def test_numero_de_reparto_nuevo_se_puede_cancelar():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491112345678", transfer)
    service.handle_text("5491112345678", "factura")
    service.handle_text("5491112345678", "FAC-9")
    service.handle_text("5491112345678", "OK")
    service.handle_text("5491112345678", "M-01")
    service.handle_text("5491112345678", "SI")

    respuesta = service.handle_text("5491112345678", "cancelar")

    assert "Registro descartado" in respuesta
    assert db.query(Reparto).count() == 0
    assert db.query(Movement).count() == 0


def test_confirmacion_no_iniciar_reparto_nuevo_vuelve_a_pedir_movil():
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
    service.handle_text("5491112345678", "OK")
    service.handle_text("5491112345678", "M-01")

    respuesta = service.handle_text("5491112345678", "NO")

    assert "En que movil" in respuesta
    assert db.query(Reparto).count() == 0
    assert db.query(Movement).one().estado_registro == RecordState.PENDIENTE_CONFIRMACION


def test_confirmacion_via_esperando_movil_se_asocia_a_reparto_abierto_de_otro_operador():
    db = session()
    db.add_all(
        [
            Operator(nombre="Ana", whatsapp_numero="5491112345678"),
            Operator(nombre="Beto", whatsapp_numero="5491100000000"),
        ]
    )
    _con_cuenta_registrada(db)
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    # Ana arranca el reparto -- Beto todavia no tiene ninguna asociacion.
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 3")
    service.handle_text("5491112345678", "SI")
    reparto = db.query(Reparto).one()

    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491100000000", transfer)
    service.handle_text("5491100000000", "SI")
    service.handle_text("5491100000000", "factura")
    service.handle_text("5491100000000", "FAC-9")
    respuesta = service.handle_text("5491100000000", "OK")
    assert "En que movil" in respuesta

    respuesta_final = service.handle_text("5491100000000", "M-01")

    assert respuesta_final == "Comprobante registrado correctamente. Salida Nº 3 en el movil M-01."
    movement = db.query(Movement).one()
    assert movement.reparto_id == reparto.id
    assert movement.movil_id == 1
    beto = db.scalar(select(Operator).where(Operator.whatsapp_numero == "5491100000000"))
    assert beto.movil_id == 1


def test_confirmacion_completa_movil_id_si_operador_ya_tiene_asignado():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    _asociar_a_reparto_abierto(db, operador_id=1, movil_id=1)
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491112345678", transfer)
    service.handle_text("5491112345678", "SI")
    service.handle_text("5491112345678", "factura")
    service.handle_text("5491112345678", "FAC-9")

    respuesta = service.handle_text("5491112345678", "OK")

    assert respuesta == "Comprobante registrado correctamente."
    assert db.query(Movement).one().movil_id == 1


def test_confirmacion_asigna_el_reparto_abierto_del_movil():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 5")
    service.handle_text("5491112345678", "SI")
    reparto = db.query(Reparto).one()

    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491112345678", transfer)
    service.handle_text("5491112345678", "SI")
    service.handle_text("5491112345678", "factura")
    service.handle_text("5491112345678", "FAC-9")
    service.handle_text("5491112345678", "OK")

    assert db.query(Movement).one().reparto_id == reparto.id


def test_confirmacion_sin_reparto_abierto_deja_reparto_id_nulo():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    db.get(Operator, 1).movil_id = 1  # tiene movil asignado, pero nunca inicio un reparto
    db.commit()
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491112345678", transfer)
    service.handle_text("5491112345678", "SI")
    service.handle_text("5491112345678", "factura")
    service.handle_text("5491112345678", "FAC-9")
    service.handle_text("5491112345678", "OK")

    assert db.query(Movement).one().reparto_id is None


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
    service.handle_text("5491112345678", "SI")
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
