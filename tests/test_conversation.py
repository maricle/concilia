from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import servicios
from app.conversation import (
    NO_CUENTA_RECEPTORA_TEXTO,
    SIN_SALIDA_ABIERTA_TEXTO,
    ConversationService,
    ExtractedTransfer,
)
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
    assert final == "Comprobante registrado correctamente.\nFactura: FAC-9 · $1250.50"

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
    assert service.handle_text("5491112345678", "FAC-9") == "Comprobante registrado correctamente.\nFactura: FAC-9 · monto no detectado"

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


def test_transfer_without_matching_cuenta_receptora_continua_sin_preguntar():
    """No se le pide al operador que elija la cuenta a mano -- el movimiento queda
    registrado con cuenta_bancaria_id null y el flujo sigue igual que si hubiera
    matcheado, para que un administrador lo complete despues desde el panel
    (/comprobantes/{id}/editar)."""
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db, alias="otra.cuenta")
    service = ConversationService(db)

    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="no.coincide")
    response = service.start_transfer("5491112345678", transfer)

    assert "factura o un numero de cuenta" in response
    movement = db.query(Movement).one()
    assert movement.cuenta_bancaria_id is None
    assert movement.cuenta_receptora_extraida == "no.coincide"


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


def test_transfer_matches_cuenta_receptora_con_digitos_corridos_por_error_de_extraccion():
    """Caso real de produccion: Claude leyo el CBU con un digito de mas en una
    tira de ceros y uno de menos al final -- mismo largo (22 digitos) pero
    corrido respecto al registrado, asi que el match exacto por digitos fallaba
    y el comprobante quedaba 'Sin identificar' aunque la cuenta correcta ya
    estaba cargada en /config/cuentas."""
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.add(BankAccount(banco="Galicia", numero_cuenta="0070077120000014194391", alias="el.paquete.llega"))
    db.commit()
    service = ConversationService(db)

    transfer = ExtractedTransfer(
        Decimal("13462.76"), datetime(2026, 9, 21), "PDX4OGNY4LZDL14Q20L6EY", cuenta_receptora="0070077120000001419439"
    )
    response = service.start_transfer("5491112345678", transfer)

    assert "factura o un numero de cuenta" in response
    assert db.query(Movement).one().cuenta_bancaria_id == 1


def test_transfer_no_matchea_por_aproximacion_si_hay_ambiguedad():
    """Si dos cuentas registradas quedan igual de cerca (dentro de la tolerancia)
    del numero extraido, no se adivina entre ellas -- se deja sin identificar
    para que un administrador lo resuelva a mano."""
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.add(BankAccount(banco="Galicia", numero_cuenta="0070077120000014194391", alias="cuenta-a"))
    db.add(BankAccount(banco="Galicia", numero_cuenta="0070077120000014194392", alias="cuenta-b"))
    db.commit()
    service = ConversationService(db)

    transfer = ExtractedTransfer(
        Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="0070077120000014194390"
    )
    response = service.start_transfer("5491112345678", transfer)

    assert "factura o un numero de cuenta" in response
    assert db.query(Movement).one().cuenta_bancaria_id is None


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

    service.handle_text(numero, "FAC-9")  # registra directo, sin SI/NO final
    assert service.needs_confirmation_keyboard(numero) is False

    otro = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-2", cuenta_receptora="empresa.mp")
    service.start_transfer(numero, otro, caption="F 123")
    assert service.needs_confirmation_keyboard(numero) is True
    assert service.needs_tipo_keyboard(numero) is False

    service.handle_text(numero, "si")
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

    assert prompt == "Tenes movimientos pendientes. Indica el tipo y numero de comprobante (ej: F 12345) o descartalo."
    assert db.query(Movement).count() == 1


def test_pending_prompt_reshows_confirmacion_final():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491112345678", transfer, caption="factura FAC-9")

    prompt = service.pending_prompt("5491112345678")

    assert prompt is not None
    assert prompt == "Tenes movimientos pendientes. Factura: FAC-9 · $500.00. ¿Lo registro? Respondé SI, o NO para descartarlo."
    assert db.query(Movement).count() == 1


def test_caption_con_factura_saltea_las_preguntas_y_registra_con_un_si():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    _asociar_a_reparto_abierto(db, operador_id=1, movil_id=1)
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")

    resumen = service.start_transfer("5491112345678", transfer, caption="  F 00012-345 ")

    assert "Factura/cuenta: Factura: 00012-345" in resumen
    assert service.handle_text("5491112345678", "si") == "Comprobante registrado correctamente.\nFactura: 00012-345 · $500.00"
    movement = db.query(Movement).one()
    assert movement.estado_registro == RecordState.CONFIRMADO
    assert movement.factura_o_cuenta_numero == "00012-345"


def test_caption_con_cuenta_y_descartar():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")

    resumen = service.start_transfer("5491112345678", transfer, caption="cta: 987")

    assert "Cuenta: 987" in resumen
    assert "Registro descartado" in service.handle_text("5491112345678", "no")
    assert db.query(Movement).count() == 0


def test_caption_sin_prefijo_reconocible_pregunta_factura_o_cuenta():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")

    resumen = service.start_transfer("5491112345678", transfer, caption="12345")

    assert "factura o un numero de cuenta" in resumen
    assert service.needs_tipo_keyboard("5491112345678") is True


def test_boton_descartar_en_paso_factura_cuenta():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer("5491112345678", transfer)

    assert "Registro descartado" in service.handle_text("5491112345678", "descartar")
    assert db.query(Movement).count() == 0


def test_iniciar_reparto_crea_la_salida_sin_guardar_el_movil_en_el_operador():
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
    assert db.get(Operator, 1).movil_id is None
    reparto = db.query(Reparto).one()
    assert reparto.movil_id == 1
    assert reparto.numero_reparto == 5
    assert reparto.hora_fin is None


def test_iniciar_reparto_se_suma_directo_si_esa_salida_ya_esta_abierta():
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

    respuesta = service.handle_text("5491100000000", "inicio movil M-01 reparto nro 5")

    # se suma directo a la Salida Nº 5 que ya estaba abierta, sin pedir confirmacion.
    assert respuesta == "Te sumaste a la Salida Nº 5 en el movil M-01."
    assert db.query(Reparto).count() == 1
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
    assert servicios.reparto_abierto_del_operador(db, ana.id) is None
    assert servicios.reparto_abierto_del_operador(db, beto.id) is None


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


def _con_salida_5_abierta() -> tuple[Session, ConversationService]:
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
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 5")
    service.handle_text("5491112345678", "SI")
    return db, service


def test_inicio_incompleto_con_salida_abierta_pregunta_si_sigue_con_esa():
    db, service = _con_salida_5_abierta()

    respuesta = service.handle_text("5491112345678", "iniciar")

    assert respuesta == (
        "Ya tenes una salida abierta (Nº 5). Responde 'cerrar' para cerrarla y arrancar la nueva, "
        "o 'continuar' para seguir con la que ya esta abierta."
    )
    assert service.handle_text("5491112345678", "continuar") == "Seguis con la salida que ya estaba abierta."
    assert servicios.reparto_abierto_del_operador(db, 1).numero_reparto == 5


def test_inicio_incompleto_con_salida_abierta_al_cerrar_pide_los_datos_de_la_nueva():
    db, service = _con_salida_5_abierta()
    service.handle_text("5491112345678", "iniciar")

    respuesta = service.handle_text("5491112345678", "cerrar")

    assert respuesta == "Salida anterior cerrada. ¿Que numero de salida es? Respondé solo con el numero."
    assert servicios.reparto_abierto_del_operador(db, 1) is None
    assert service.handle_text("5491112345678", "6").startswith("La Salida Nº 6 no esta abierta.")
    assert "Vas a iniciar la Salida Nº 6 en el movil M-02" in service.handle_text("5491112345678", "M-02")
    assert service.handle_text("5491112345678", "SI") == "Salida Nº 6 iniciada en el movil M-02."


def test_inicio_con_solo_el_numero_y_salida_abierta_al_cerrar_pide_el_movil():
    _, service = _con_salida_5_abierta()

    assert "Ya tenes una salida abierta (Nº 5)" in service.handle_text("5491112345678", "inicio salida nro 6")
    respuesta = service.handle_text("5491112345678", "cerrar")

    assert respuesta.startswith("Salida anterior cerrada. La Salida Nº 6 no esta abierta.")


def test_iniciar_reparto_solo_palabra_clave_pide_numero_y_luego_movil():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)

    respuesta = service.handle_text("5491112345678", "inicio")
    assert "numero de salida" in respuesta

    respuesta = service.handle_text("5491112345678", "5")
    assert "En que movil" in respuesta

    respuesta = service.handle_text("5491112345678", "M-01")
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
    assert "numero de salida" in prompt


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


def test_iniciar_reparto_en_un_movil_con_otra_salida_avisa_y_pide_el_numero():
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
    _asociar_a_reparto_abierto(db, operador_id=1, movil_id=1, numero_reparto=5)
    service = ConversationService(db)

    respuesta = service.handle_text("5491100000000", "inicio movil M-01 reparto nro 9")

    assert respuesta == "El movil M-01 ya tiene abierta la Salida Nº 5. Si vas en ese movil, indicá ese numero de salida."
    assert servicios.reparto_abierto_del_operador(db, 2) is None
    assert service.handle_text("5491100000000", "5") == "Te sumaste a la Salida Nº 5 en el movil M-01."
    assert db.query(Reparto).count() == 1


def test_iniciar_reparto_con_una_salida_abierta_en_otro_movil_avisa():
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
    _asociar_a_reparto_abierto(db, operador_id=1, movil_id=1, numero_reparto=5)
    service = ConversationService(db)

    respuesta = service.handle_text("5491100000000", "inicio movil M-02 reparto nro 5")

    assert respuesta == "La Salida Nº 5 esta abierta en el movil M-01. Revisá el numero de salida."
    assert db.query(Reparto).count() == 1


def test_inicio_incompleto_con_la_salida_abierta_se_suma_sin_pedir_el_movil():
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
    _asociar_a_reparto_abierto(db, operador_id=1, movil_id=1, numero_reparto=5)
    service = ConversationService(db)

    assert "numero de salida" in service.handle_text("5491100000000", "inicio")
    respuesta = service.handle_text("5491100000000", "5")

    assert respuesta == "Te sumaste a la Salida Nº 5 en el movil M-01."
    assert servicios.reparto_abierto_del_operador(db, 2).numero_reparto == 5
    assert db.get(WhatsAppConversation, "5491100000000").estado == ConversationState.ESPERANDO_COMPROBANTE


def test_inicio_incompleto_con_la_salida_cerrada_pide_el_movil_y_confirma():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)

    service.handle_text("5491112345678", "inicio")
    respuesta = service.handle_text("5491112345678", "7")

    assert respuesta == (
        "La Salida Nº 7 no esta abierta. ¿En que movil la vas a iniciar? Respondé con el numero del movil."
    )
    assert service.pending_prompt("5491112345678") == respuesta
    assert "Vas a iniciar la Salida Nº 7 en el movil M-01" in service.handle_text("5491112345678", "M-01")
    assert service.handle_text("5491112345678", "SI") == "Salida Nº 7 iniciada en el movil M-01."
    assert db.query(Reparto).one().numero_reparto == 7


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

    respuesta_cerrar = service.handle_text("5491112345678", "cerrar")

    assert "Salida anterior cerrada" in respuesta_cerrar
    assert "Nº 2" in respuesta_cerrar
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
    service.handle_text("5491112345678", "inicio movil M-02 reparto nro 2")
    respuesta = service.handle_text("5491112345678", "cerrar")

    assert respuesta == "Salida anterior cerrada. Te sumaste a la Salida Nº 2 en el movil M-02."
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
    assert notificaciones == [
        (
            "5491112345678",
            "La Salida Nº 7 en el movil M-01 fue cerrada por Beto.\n\n"
            "Cierre Salida Nº 7\nSin comprobantes registrados.",
        )
    ]


def test_cerrar_reparto_envia_resumen_bancario_en_vez_de_pdf():
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
    db.add_all(
        [
            BankAccount(banco="Mercado Pago", numero_cuenta="1", alias="mp"),
            BankAccount(banco="Galicia", numero_cuenta="2", alias="gal"),
        ]
    )
    db.commit()
    service = ConversationService(db)
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 7")
    service.handle_text("5491112345678", "SI")
    service.handle_text("5491100000000", "inicio movil M-01 reparto nro 7")  # Beto se asocia

    reparto = db.query(Reparto).filter_by(numero_reparto=7).one()
    db.add_all(
        [
            Movement(
                operador_id=1,
                monto=Decimal("1000"),
                fecha_transaccion=datetime.now(),
                numero_operacion="OP-1",
                movil_id=1,
                reparto_id=reparto.id,
                cuenta_bancaria_id=1,
            ),
            Movement(
                operador_id=1,
                monto=Decimal("500"),
                fecha_transaccion=datetime.now(),
                numero_operacion="OP-2",
                movil_id=1,
                reparto_id=reparto.id,
                cuenta_bancaria_id=2,
            ),
            Movement(
                operador_id=1,
                monto=Decimal("250"),
                fecha_transaccion=datetime.now(),
                numero_operacion="OP-3",
                movil_id=1,
                reparto_id=reparto.id,
            ),
        ]
    )
    db.commit()

    respuesta = service.handle_text("5491100000000", "cerrar")

    assert respuesta == (
        "Salida Nº 7 cerrada.\n\n"
        "Cierre Salida Nº 7\n"
        "Galicia: $500,00\n"
        "Mercado Pago: $1.000,00\n"
        "Sin banco identificado: $250,00"
    )

    notificaciones = service.pop_notificaciones()
    assert len(notificaciones) == 1
    numero, mensaje = notificaciones[0]
    assert numero == "5491112345678"
    assert mensaje == (
        "La Salida Nº 7 en el movil M-01 fue cerrada por Beto.\n\n"
        "Cierre Salida Nº 7\n"
        "Galicia: $500,00\n"
        "Mercado Pago: $1.000,00\n"
        "Sin banco identificado: $250,00"
    )


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


def _borrador_listo_para_registrar(service: ConversationService, numero: str = "5491112345678") -> str:
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    service.start_transfer(numero, transfer)
    return service.handle_text(numero, "F FAC-9")


def test_sin_salida_abierta_pide_el_numero_y_el_movil_solo_si_no_esta_abierta():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)

    respuesta = _borrador_listo_para_registrar(service)

    assert respuesta == SIN_SALIDA_ABIERTA_TEXTO
    assert db.query(Movement).one().estado_registro == RecordState.PENDIENTE_CONFIRMACION

    pedir_movil = service.handle_text("5491112345678", "8")
    assert pedir_movil.startswith("La Salida Nº 8 no esta abierta.")
    assert service.pending_prompt("5491112345678").endswith(pedir_movil)
    respuesta_final = service.handle_text("5491112345678", "M-01")

    assert respuesta_final == (
        "Comprobante registrado correctamente. Se inicio la Salida Nº 8 en el movil M-01.\nFactura: FAC-9 · $500.00"
    )
    movement = db.query(Movement).one()
    assert movement.estado_registro == RecordState.CONFIRMADO
    assert movement.movil_id == 1
    assert db.query(Reparto).one().numero_reparto == 8


def test_sin_salida_abierta_se_suma_a_la_salida_abierta_con_ese_numero():
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
    # Ana arranca la salida -- Beto todavia no esta asociado.
    service.handle_text("5491112345678", "inicio movil M-01 reparto nro 3")
    service.handle_text("5491112345678", "SI")
    reparto = db.query(Reparto).one()

    assert _borrador_listo_para_registrar(service, "5491100000000") == SIN_SALIDA_ABIERTA_TEXTO
    respuesta_final = service.handle_text("5491100000000", "3")

    assert respuesta_final == (
        "Comprobante registrado correctamente. Salida Nº 3 en el movil M-01.\nFactura: FAC-9 · $500.00"
    )
    movement = db.query(Movement).one()
    assert movement.reparto_id == reparto.id
    assert movement.movil_id == 1
    assert servicios.reparto_abierto_del_operador(db, 2).id == reparto.id


def test_sin_salida_pide_el_movil_y_crea_la_salida_sin_mas_preguntas():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    _borrador_listo_para_registrar(service)

    assert "En que movil" in service.handle_text("5491112345678", "8")
    respuesta_final = service.handle_text("5491112345678", "M-01")

    assert respuesta_final.startswith("Comprobante registrado correctamente. Se inicio la Salida Nº 8 en el movil M-01.")
    assert db.query(Reparto).one().numero_reparto == 8
    assert db.query(Movement).one().estado_registro == RecordState.CONFIRMADO


def test_movil_elegido_con_otra_salida_abierta_indica_ese_numero():
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
    _asociar_a_reparto_abierto(db, operador_id=1, movil_id=1, numero_reparto=4)
    service = ConversationService(db)
    _borrador_listo_para_registrar(service, "5491100000000")

    assert service.handle_text("5491100000000", "9").startswith("La Salida Nº 9 no esta abierta.")
    respuesta = service.handle_text("5491100000000", "M-01")

    assert respuesta == "El movil M-01 ya tiene abierta la Salida Nº 4. Si vas en ese movil, indicá ese numero de salida."
    assert db.query(Reparto).count() == 1
    assert service.handle_text("5491100000000", "4").startswith("Comprobante registrado correctamente. Salida Nº 4")


def test_numero_de_salida_invalido_vuelve_a_pedir():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    _borrador_listo_para_registrar(service)

    respuesta = service.handle_text("5491112345678", "ocho")

    assert "no es un numero de salida valido" in respuesta
    assert db.query(Reparto).count() == 0


def test_numero_de_salida_se_puede_descartar():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    service = ConversationService(db)
    _borrador_listo_para_registrar(service)
    assert service.needs_descartar_keyboard("5491112345678") is True

    respuesta = service.handle_text("5491112345678", "descartar")

    assert "Registro descartado" in respuesta
    assert db.query(Reparto).count() == 0
    assert db.query(Movement).count() == 0
    assert db.get(WhatsAppConversation, "5491112345678").numero_reparto_pendiente is None


def test_tipo_y_numero_escritos_juntos_registran_de_una():
    db = session()
    db.add(Operator(nombre="Ana", whatsapp_numero="5491112345678"))
    _con_cuenta_registrada(db)
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()
    _asociar_a_reparto_abierto(db, operador_id=1, movil_id=1)
    service = ConversationService(db)
    transfer = ExtractedTransfer(Decimal("500"), datetime(2026, 8, 21), "OP-1", cuenta_receptora="empresa.mp")
    resumen = service.start_transfer("5491112345678", transfer)
    assert "Indica el tipo y numero de comprobante" in resumen

    respuesta = service.handle_text("5491112345678", "c987")

    assert respuesta == "Comprobante registrado correctamente.\nCuenta: 987 · $500.00"


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
    respuesta = service.handle_text("5491112345678", "FAC-9")

    assert respuesta == "Comprobante registrado correctamente.\nFactura: FAC-9 · $500.00"
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

    assert db.query(Movement).one().reparto_id == reparto.id


def test_confirmacion_sin_reparto_abierto_deja_reparto_id_nulo():
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

    prompt = service.pending_prompt("5491112345678")

    assert prompt == "Tenes movimientos pendientes. Indica el numero de salida o descartalo."
