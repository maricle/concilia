"""Contrato de app/servicios.py: las reglas de carga de comprobantes y salidas
que comparten el chat y la interfaz web de operadores."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import servicios
from app.db import Base
from app.models import (
    BankAccount,
    ConversationState,
    Movement,
    Movil,
    Operator,
    RecordState,
    Reparto,
)


def _base() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add_all([Operator(nombre="Ana", whatsapp_numero="111"), Operator(nombre="Beto", whatsapp_numero="222")])
    db.add(BankAccount(banco="Mercado Pago", numero_cuenta="1", alias="empresa.mp"))
    db.commit()
    db.add_all([Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1), Movil(numero="M-02", nombre="Camion 2", responsable_operador_id=2)])
    db.commit()
    return db


def _transfer(numero_operacion: str | None = "OP-1", cuenta: str = "empresa.mp") -> servicios.ExtractedTransfer:
    return servicios.ExtractedTransfer(Decimal("100"), datetime(2026, 10, 2, 10), numero_operacion, cuenta_receptora=cuenta)


def _borrador(db: Session, operador_id: int = 1, **kwargs) -> Movement:
    borrador = servicios.crear_borrador(db, db.get(Operator, operador_id), _transfer(**kwargs))
    assert isinstance(borrador, Movement)
    borrador.factura_o_cuenta_tipo, borrador.factura_o_cuenta_numero = "factura", "F-1"
    return borrador


def test_crear_borrador_deja_la_conversacion_esperando_factura_cuenta_y_guarda_el_origen():
    db = _base()
    ana = db.get(Operator, 1)

    borrador = servicios.crear_borrador(db, ana, _transfer(), origen="web")

    assert borrador.estado_registro == RecordState.PENDIENTE_CONFIRMACION
    assert borrador.origen == "web"
    assert borrador.cuenta_bancaria_id == 1
    conversacion = servicios.conversacion_de(db, ana)
    assert conversacion.movimiento_borrador_id == borrador.id
    assert conversacion.estado == ConversationState.ESPERANDO_TIPO_FACTURA_CUENTA


def test_crear_borrador_rechaza_duplicados_y_falta_de_cuentas():
    db = _base()
    db.add(Movement(operador_id=2, monto=Decimal("1"), fecha_transaccion=datetime(2026, 10, 1), numero_operacion="OP-1"))
    db.commit()

    assert servicios.crear_borrador(db, db.get(Operator, 1), _transfer()) is servicios.Rechazo.COMPROBANTE_DUPLICADO

    sin_cuentas = Session(create_engine("sqlite://"))
    Base.metadata.create_all(sin_cuentas.get_bind())
    sin_cuentas.add(Operator(nombre="Ana", whatsapp_numero="111"))
    sin_cuentas.commit()
    resultado = servicios.crear_borrador(sin_cuentas, sin_cuentas.get(Operator, 1), _transfer())
    assert resultado is servicios.Rechazo.SIN_CUENTAS_BANCARIAS


def test_cuenta_receptora_no_identificada_se_registra_igual_sin_cuenta():
    db = _base()

    borrador = servicios.crear_borrador(db, db.get(Operator, 1), _transfer(cuenta="otra.cuenta"))

    assert isinstance(borrador, Movement)
    assert borrador.cuenta_bancaria_id is None


def test_registrar_en_la_salida_propia():
    db = _base()
    ana = db.get(Operator, 1)
    salida = servicios.crear_salida(db, ana, db.get(Movil, 1), 5)
    db.commit()
    borrador = _borrador(db)

    resultado = servicios.registrar(db, ana, servicios.conversacion_de(db, ana), borrador)

    assert isinstance(resultado, servicios.Registrado)
    assert resultado.salida == servicios.SalidaUsada.PROPIA
    assert (borrador.estado_registro, borrador.reparto_id, borrador.movil_id) == (RecordState.CONFIRMADO, salida.id, 1)
    assert servicios.conversacion_de(db, ana).estado == ConversationState.ESPERANDO_COMPROBANTE


def test_registrar_sin_salida_abierta_y_resolverla_por_numero():
    db = _base()
    ana = db.get(Operator, 1)
    borrador = _borrador(db)
    conversacion = servicios.conversacion_de(db, ana)

    assert isinstance(servicios.registrar(db, ana, conversacion, borrador), servicios.SinSalidaAbierta)
    assert conversacion.estado == ConversationState.ESPERANDO_NUMERO_REPARTO_NUEVO

    # Sin movil asignado y sin salida con ese numero: hay que pedir el movil.
    assert isinstance(servicios.resolver_salida_por_numero(db, ana, conversacion, borrador, 7), servicios.PedirMovil)
    assert (conversacion.estado, conversacion.numero_reparto_pendiente) == (ConversationState.ESPERANDO_MOVIL, 7)

    resultado = servicios.registrar_en_movil(db, ana, conversacion, borrador, db.get(Movil, 1))
    assert isinstance(resultado, servicios.Registrado)
    assert resultado.salida == servicios.SalidaUsada.NUEVA
    assert resultado.reparto.numero_reparto == 7


def test_ubicar_salida_pide_el_movil_solo_si_la_salida_no_esta_abierta():
    db = _base()
    beto = db.get(Operator, 2)
    m1, m2 = db.get(Movil, 1), db.get(Movil, 2)
    abierta = servicios.crear_salida(db, beto, m2, 3)
    db.commit()

    assert servicios.ubicar_salida(db, 3) is abierta
    assert servicios.ubicar_salida(db, 9) == servicios.PedirMovil(varias=False)
    assert servicios.ubicar_salida(db, 9, m1) == servicios.SalidaNueva(m1)
    assert servicios.ubicar_salida(db, 3, m2) is abierta
    assert servicios.ubicar_salida(db, 9, m2) == servicios.MovilConOtraSalida(m2, abierta)
    assert servicios.ubicar_salida(db, 3, m1) == servicios.SalidaEnOtroMovil(abierta)

    servicios.crear_salida(db, db.get(Operator, 1), m1, 3)
    db.commit()
    assert servicios.ubicar_salida(db, 3) == servicios.PedirMovil(varias=True)
    assert servicios.ubicar_salida(db, 3, m2) is abierta


def test_resolver_salida_por_numero_se_suma_sin_guardar_el_movil_en_el_operador():
    db = _base()
    ana, beto = db.get(Operator, 1), db.get(Operator, 2)
    servicios.crear_salida(db, beto, db.get(Movil, 2), 3)
    db.commit()

    sumada = servicios.resolver_salida_por_numero(db, ana, servicios.conversacion_de(db, ana), _borrador(db, numero_operacion="OP-A"), 3)

    assert isinstance(sumada, servicios.Registrado)
    assert (sumada.salida, sumada.movil.id, sumada.movimiento.movil_id) == (servicios.SalidaUsada.EXISTENTE, 2, 2)
    assert ana.movil_id is None
    assert servicios.reparto_abierto_del_operador(db, ana.id) is sumada.reparto


def test_salida_no_abierta_pide_el_movil_y_la_crea_ahi():
    db = _base()
    ana = db.get(Operator, 1)
    conversacion = servicios.conversacion_de(db, ana)
    borrador = _borrador(db)

    pedido = servicios.resolver_salida_por_numero(db, ana, conversacion, borrador, 9)
    assert isinstance(pedido, servicios.PedirMovil)
    assert (conversacion.estado, conversacion.numero_reparto_pendiente) == (ConversationState.ESPERANDO_MOVIL, 9)

    nueva = servicios.registrar_en_movil(db, ana, conversacion, borrador, db.get(Movil, 1))
    assert isinstance(nueva, servicios.Registrado)
    assert (nueva.salida, nueva.movil.id, nueva.reparto.numero_reparto) == (servicios.SalidaUsada.NUEVA, 1, 9)


def test_movil_con_otra_salida_vuelve_a_pedir_el_numero():
    db = _base()
    ana, beto = db.get(Operator, 1), db.get(Operator, 2)
    servicios.crear_salida(db, beto, db.get(Movil, 1), 4)
    db.commit()
    conversacion = servicios.conversacion_de(db, ana)
    borrador = _borrador(db)
    servicios.resolver_salida_por_numero(db, ana, conversacion, borrador, 9)

    resultado = servicios.registrar_en_movil(db, ana, conversacion, borrador, db.get(Movil, 1))

    assert isinstance(resultado, servicios.MovilConOtraSalida)
    assert resultado.reparto.numero_reparto == 4
    assert conversacion.estado == ConversationState.ESPERANDO_NUMERO_REPARTO_NUEVO
    assert conversacion.numero_reparto_pendiente is None
    assert borrador.estado_registro == RecordState.PENDIENTE_CONFIRMACION


def test_descartar_borrador_lo_borra_y_limpia_la_conversacion():
    db = _base()
    ana = db.get(Operator, 1)
    _borrador(db)
    conversacion = servicios.conversacion_de(db, ana)
    conversacion.numero_reparto_pendiente = 3

    servicios.descartar_borrador(db, conversacion)
    db.commit()

    assert db.query(Movement).count() == 0
    assert conversacion.movimiento_borrador_id is None
    assert conversacion.numero_reparto_pendiente is None
    assert conversacion.estado == ConversationState.ESPERANDO_COMPROBANTE


def test_cerrar_salida_cierra_para_todos_y_devuelve_a_los_demas():
    db = _base()
    ana, beto = db.get(Operator, 1), db.get(Operator, 2)
    salida = servicios.crear_salida(db, ana, db.get(Movil, 1), 5)
    servicios.asociar_operador(db, salida, beto)
    db.commit()

    otros = servicios.cerrar_salida(db, salida, ana)

    assert salida.hora_fin is not None
    assert [o.nombre for o in otros] == ["Beto"]
    assert servicios.reparto_abierto_del_operador(db, beto.id) is None
    assert db.query(Reparto).count() == 1
