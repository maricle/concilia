"""Reglas de negocio de la carga de comprobantes y de las salidas, independientes
del canal.

Las usan el chat (ConversationService, para WhatsApp y Telegram) y la interfaz
web de operadores. Reciben objetos del modelo y devuelven resultados tipados --
nunca texto ni botones: como se le cuenta cada resultado al operador lo decide
cada canal. Asi un comprobante cargado por cualquier canal sigue exactamente las
mismas reglas (borrador unico, duplicados, cuenta receptora, salida y movil).

El borrador vive en la fila de `conversaciones_whatsapp` del operador (clave:
`Operator.whatsapp_numero`), compartida por todos los canales: un comprobante
empezado por uno se puede terminar por otro.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    BankAccount,
    ConversationState,
    Movement,
    Movil,
    Operator,
    RecordState,
    Reparto,
    RepartoOperador,
    TipoIdentificador,
    WhatsAppConversation,
)
from .zona_horaria import ahora_argentina, hoy_argentina

logger = logging.getLogger(__name__)


@dataclass
class ExtractedTransfer:
    monto: Decimal | None
    fecha_transaccion: datetime
    numero_operacion: str | None
    banco_emisor: str | None = None
    cuenta_receptora: str | None = None
    titular: str | None = None


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    fila_previa = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        fila_actual = [i] + [0] * len(b)
        for j, char_b in enumerate(b, 1):
            fila_actual[j] = min(
                fila_previa[j] + 1,  # borrar
                fila_actual[j - 1] + 1,  # insertar
                fila_previa[j - 1] + (char_a != char_b),  # sustituir
            )
        fila_previa = fila_actual
    return fila_previa[-1]


_LARGO_CBU_CVU = 22
_TOLERANCIA_DIGITOS_CBU_CVU = 2


def buscar_cuenta_bancaria(session: Session, cuenta_receptora: str | None) -> BankAccount | None:
    """Matchea el CBU/CVU/alias leido del comprobante contra las cuentas bancarias
    registradas en /config/cuentas. Compara el alias tal cual (sin distinguir
    mayusculas) y el numero de cuenta primero por sus digitos exactos (para
    tolerar espacios, guiones u otro formato).

    Si no hay match exacto pero el dato extraido tiene forma de CBU/CVU (22
    digitos), se intenta un match aproximado por distancia de edicion --
    confirmado con un caso real de produccion donde Claude leyo el CBU correcto
    salvo por un digito insertado de mas en una tira de ceros y uno de menos al
    final (mismo largo total, pero corrido): "...000001419439" en vez de
    "...0000141943 91". Un umbral de 2 separa bien ese tipo de error de
    transcripcion de una cuenta genuinamente distinta (que en la practica
    difiere en muchos mas digitos), y solo se acepta si es la unica cuenta
    registrada dentro de esa tolerancia -- si hay ambiguedad, no se adivina."""
    if not cuenta_receptora:
        return None
    normalizado = cuenta_receptora.strip().lower()
    digitos = re.sub(r"\D", "", cuenta_receptora)
    cuentas = session.scalars(select(BankAccount)).all()
    for cuenta in cuentas:
        if cuenta.alias.strip().lower() == normalizado:
            return cuenta
        if digitos and digitos == re.sub(r"\D", "", cuenta.numero_cuenta):
            return cuenta

    if digitos and len(digitos) == _LARGO_CBU_CVU:
        candidatas = []
        for cuenta in cuentas:
            digitos_cuenta = re.sub(r"\D", "", cuenta.numero_cuenta)
            if len(digitos_cuenta) != _LARGO_CBU_CVU:
                continue
            if _levenshtein(digitos, digitos_cuenta) <= _TOLERANCIA_DIGITOS_CBU_CVU:
                candidatas.append(cuenta)
        if len(candidatas) == 1:
            return candidatas[0]

    logger.warning(
        "Cuenta receptora sin match: extraida=%r cuentas_registradas=%r",
        cuenta_receptora,
        [(c.alias, c.numero_cuenta) for c in cuentas],
    )
    return None


_CAPTION_FACTURA_CUENTA = re.compile(
    r"^\s*(factura|fact|fac|fc|f|cuenta|cta|c)(?![a-z])[\s.:#nº°-]*(\S+)\s*$", re.IGNORECASE
)


def interpretar_factura_cuenta(caption: str | None) -> tuple[TipoIdentificador, str] | None:
    """Lee el texto que el operador manda junto a la foto/PDF del comprobante
    (ej. "F 12345", "factura 12345", "C 987", "cta: 987"). Si viene, se ahorran
    los dos pasos de preguntar tipo y numero por separado -- desde octubre de
    2026 Meta cobra cada respuesta del bot por WhatsApp, asi que cada paso menos
    es un mensaje menos. Sin prefijo reconocible devuelve None y se sigue el
    flujo de preguntas normal (un numero suelto no alcanza: no se sabe si es
    factura o cuenta)."""
    if not caption:
        return None
    match = _CAPTION_FACTURA_CUENTA.match(caption)
    if match is None:
        return None
    prefijo = match.group(1).lower()
    tipo = TipoIdentificador.FACTURA if prefijo.startswith("f") else TipoIdentificador.CUENTA
    return tipo, match.group(2)


# ---------------------------------------------------------------- conversacion


def conversacion_de(session: Session, operador: Operator) -> WhatsAppConversation:
    """La fila de conversacion del operador (la crea si todavia no existe)."""
    conversacion = session.get(WhatsAppConversation, operador.whatsapp_numero)
    if conversacion is None:
        conversacion = WhatsAppConversation(numero=operador.whatsapp_numero)
        session.add(conversacion)
        session.flush()
    return conversacion


# ------------------------------------------------------------- moviles/salidas


def reparto_abierto_del_movil(session: Session, movil_id: int) -> Reparto | None:
    return session.scalar(select(Reparto).where(Reparto.movil_id == movil_id, Reparto.hora_fin.is_(None)))


def reparto_abierto_del_operador(session: Session, operador_id: int) -> Reparto | None:
    """El reparto abierto (de cualquier movil) al que este operador esta
    asociado ahora mismo. Un operador solo puede estar asociado a un reparto
    abierto a la vez (se valida al asociar, no lo impide el modelo)."""
    return session.scalar(
        select(Reparto)
        .join(RepartoOperador, RepartoOperador.reparto_id == Reparto.id)
        .where(RepartoOperador.operador_id == operador_id, Reparto.hora_fin.is_(None))
    )


def asociar_operador(session: Session, reparto: Reparto, operador: Operator) -> None:
    """Suma al operador como participante del reparto (chofer, ayudante, o el
    que entra en un cambio de turno) si todavia no estaba. El movil no se guarda
    en el operador: queda en la salida y en cada comprobante."""
    ya_asociado = session.scalar(
        select(RepartoOperador).where(RepartoOperador.reparto_id == reparto.id, RepartoOperador.operador_id == operador.id)
    )
    if ya_asociado is None:
        session.add(RepartoOperador(reparto_id=reparto.id, operador_id=operador.id))


def buscar_movil_activo(session: Session, texto: str | None) -> Movil | None:
    """Busca un movil activo por numero, tolerando que el operador escriba con
    o sin el prefijo 'M-' -- 'M-01', 'm01', '01' y '1' matchean el mismo movil
    si sus digitos coinciden. Primero intenta match exacto (por si el numero
    del movil no es puramente numerico, ej. "Camion Rojo")."""
    if texto is None:
        return None
    texto = texto.strip()
    movil = session.scalar(select(Movil).where(Movil.numero == texto, Movil.activo.is_(True)))
    if movil is not None:
        return movil
    digitos = re.sub(r"\D", "", texto).lstrip("0")
    if not digitos:
        return None
    for candidato in session.scalars(select(Movil).where(Movil.activo.is_(True))):
        if re.sub(r"\D", "", candidato.numero).lstrip("0") == digitos:
            return candidato
    return None


def crear_salida(session: Session, operador: Operator, movil: Movil, numero_reparto: int | None) -> Reparto:
    """Abre una salida nueva en el movil y asocia al operador (sin commit)."""
    reparto = Reparto(movil_id=movil.id, fecha=hoy_argentina(), hora_inicio=ahora_argentina(), numero_reparto=numero_reparto)
    session.add(reparto)
    session.flush()
    asociar_operador(session, reparto, operador)
    return reparto


@dataclass
class PedirMovil:
    """No alcanza con el numero de salida para saber el movil: hay que pedirlo
    (la salida no esta abierta, o hay mas de una abierta con ese numero)."""

    varias: bool = False


@dataclass
class SalidaNueva:
    """No hay salida abierta con ese numero y el movil esta libre: se puede crear."""

    movil: Movil


@dataclass
class SalidaEnOtroMovil:
    reparto: Reparto


@dataclass
class MovilConOtraSalida:
    movil: Movil
    reparto: Reparto


def ubicar_salida(
    session: Session, numero_reparto: int, movil: Movil | None = None
) -> Reparto | PedirMovil | SalidaNueva | SalidaEnOtroMovil | MovilConOtraSalida:
    """Regla unica (web y chat) para empezar a trabajar en una salida a partir de
    su numero. Si hay una sola salida abierta con ese numero, es esa (el operador
    se suma sin indicar el movil). Si no esta abierta, recien ahi hace falta el
    movil para crearla, y el movil no puede estar en otra salida abierta."""
    abiertas = session.scalars(
        select(Reparto).where(Reparto.numero_reparto == numero_reparto, Reparto.hora_fin.is_(None))
    ).all()
    if movil is None:
        return abiertas[0] if len(abiertas) == 1 else PedirMovil(varias=len(abiertas) > 1)
    en_el_movil = reparto_abierto_del_movil(session, movil.id)
    if en_el_movil is not None:
        return en_el_movil if en_el_movil.numero_reparto == numero_reparto else MovilConOtraSalida(movil, en_el_movil)
    if abiertas:
        return SalidaEnOtroMovil(abiertas[0])
    return SalidaNueva(movil)


def cerrar_salida(session: Session, reparto: Reparto, quien_cierra: Operator) -> list[Operator]:
    """Cierra la salida para todos sus operadores (sin commit) y devuelve los
    OTROS operadores asociados, por si el canal quiere avisarles."""
    reparto.hora_fin = ahora_argentina()
    otros = []
    for asociado in session.scalars(select(RepartoOperador).where(RepartoOperador.reparto_id == reparto.id)):
        if asociado.operador_id == quien_cierra.id:
            continue
        operador = session.get(Operator, asociado.operador_id)
        if operador is not None:
            otros.append(operador)
    return otros


SIN_BANCO_IDENTIFICADO = "Sin banco identificado"


@dataclass
class TotalesSalida:
    por_banco: dict[str, Decimal]  # incluye SIN_BANCO_IDENTIFICADO si corresponde
    cantidad_comprobantes: int


def totales_de_salida(session: Session, reparto: Reparto) -> TotalesSalida:
    movimientos = session.scalars(select(Movement).where(Movement.reparto_id == reparto.id)).all()
    por_banco: dict[str, Decimal] = {}
    for movimiento in movimientos:
        banco = movimiento.cuenta_bancaria.banco if movimiento.cuenta_bancaria else SIN_BANCO_IDENTIFICADO
        por_banco[banco] = por_banco.get(banco, Decimal("0")) + (movimiento.monto or Decimal("0"))
    return TotalesSalida(por_banco, len(movimientos))


# ------------------------------------------------------------------- borrador


class Rechazo(StrEnum):
    COMPROBANTE_DUPLICADO = "comprobante_duplicado"
    SIN_CUENTAS_BANCARIAS = "sin_cuentas_bancarias"


def crear_borrador(
    session: Session,
    operador: Operator,
    transfer: ExtractedTransfer,
    archivo_id: int | None = None,
    *,
    origen: str = "whatsapp",
) -> Movement | Rechazo:
    """Crea el borrador del comprobante a partir de los datos extraidos y deja la
    conversacion esperando tipo y numero de factura/cuenta. No verifica si ya
    habia otro borrador: eso lo controla cada canal antes de gastar la extraccion."""
    if transfer.numero_operacion:
        duplicado = session.scalar(select(Movement).where(Movement.numero_operacion == transfer.numero_operacion))
        if duplicado is not None:
            return Rechazo.COMPROBANTE_DUPLICADO
    cuenta_bancaria = buscar_cuenta_bancaria(session, transfer.cuenta_receptora)
    if cuenta_bancaria is None and session.scalar(select(BankAccount.id).limit(1)) is None:
        # Sin ninguna cuenta cargada en /config/cuentas no hay contra que
        # conciliar -- ahi si hace falta un administrador.
        return Rechazo.SIN_CUENTAS_BANCARIAS

    conversacion = conversacion_de(session, operador)
    movimiento = Movement(
        operador_id=operador.id,
        monto=transfer.monto,
        fecha_transaccion=transfer.fecha_transaccion,
        numero_operacion=transfer.numero_operacion,
        banco_emisor=transfer.banco_emisor,
        cuenta_receptora_extraida=transfer.cuenta_receptora,
        titular=transfer.titular,
        archivo_id=archivo_id,
        # Si no se pudo identificar la cuenta, el comprobante sigue sin cuenta y
        # un administrador la completa despues desde el panel.
        cuenta_bancaria_id=cuenta_bancaria.id if cuenta_bancaria is not None else None,
        origen=origen,
    )
    session.add(movimiento)
    session.flush()
    conversacion.movimiento_borrador_id = movimiento.id
    conversacion.estado = ConversationState.ESPERANDO_TIPO_FACTURA_CUENTA
    session.commit()
    return movimiento


def descartar_borrador(session: Session, conversacion: WhatsAppConversation) -> None:
    """Borra el borrador (no existe un estado "cancelado") y deja la conversacion
    en reposo (sin commit)."""
    if conversacion.movimiento_borrador is not None:
        session.delete(conversacion.movimiento_borrador)
    conversacion.movimiento_borrador_id = None
    conversacion.numero_reparto_pendiente = None
    conversacion.movil_pendiente_numero = None
    conversacion.estado = ConversationState.ESPERANDO_COMPROBANTE


# ------------------------------------------------------------------- registro


class SalidaUsada(StrEnum):
    PROPIA = "propia"  # la salida que el operador ya tenia abierta
    EXISTENTE = "existente"  # se sumo a una salida abierta (ej. la de un companero)
    NUEVA = "nueva"  # se abrio una salida nueva para registrarlo


@dataclass
class Registrado:
    movimiento: Movement
    reparto: Reparto
    movil: Movil
    salida: SalidaUsada


@dataclass
class SinSalidaAbierta:
    """El operador no tiene salida abierta: hay que pedirle el numero de salida."""


@dataclass
class MovilSinSalida:
    """El movil indicado no tiene salida abierta y no hay numero de salida para crearla."""

    movil: Movil


@dataclass
class MovilNoDisponible:
    """El movil que se habia indicado ya no esta activo."""


def _confirmar(
    session: Session, conversacion: WhatsAppConversation, movimiento: Movement, reparto: Reparto, movil: Movil, salida: SalidaUsada
) -> Registrado:
    movimiento.movil_id = movil.id
    movimiento.reparto_id = reparto.id
    movimiento.estado_registro = RecordState.CONFIRMADO
    conversacion.movimiento_borrador_id = None
    conversacion.numero_reparto_pendiente = None
    conversacion.movil_pendiente_numero = None
    conversacion.estado = ConversationState.ESPERANDO_COMPROBANTE
    session.commit()
    return Registrado(movimiento, reparto, movil, salida)


def registrar(
    session: Session, operador: Operator, conversacion: WhatsAppConversation, movimiento: Movement
) -> Registrado | SinSalidaAbierta:
    """Confirma el borrador (con tipo y numero de factura/cuenta ya cargados) en
    la salida abierta del operador. Sin salida abierta, deja la conversacion
    esperando el numero de salida."""
    reparto = reparto_abierto_del_operador(session, operador.id)
    if reparto is None:
        conversacion.estado = ConversationState.ESPERANDO_NUMERO_REPARTO_NUEVO
        conversacion.movil_pendiente_numero = None
        session.commit()
        return SinSalidaAbierta()
    return _confirmar(session, conversacion, movimiento, reparto, session.get(Movil, reparto.movil_id), SalidaUsada.PROPIA)


def resolver_salida_por_numero(
    session: Session, operador: Operator, conversacion: WhatsAppConversation, movimiento: Movement, numero_reparto: int
) -> Registrado | PedirMovil:
    """El operador sin salida abierta indico el numero de salida: si esta
    abierta, se suma a ella y queda registrado. Si no, queda esperando el movil
    (con el numero guardado) para crearla."""
    salida = ubicar_salida(session, numero_reparto)
    if isinstance(salida, Reparto):
        asociar_operador(session, salida, operador)
        return _confirmar(session, conversacion, movimiento, salida, salida.movil, SalidaUsada.EXISTENTE)
    conversacion.numero_reparto_pendiente = numero_reparto
    conversacion.estado = ConversationState.ESPERANDO_MOVIL
    session.commit()
    return salida


def registrar_en_movil(
    session: Session, operador: Operator, conversacion: WhatsAppConversation, movimiento: Movement, movil: Movil
) -> Registrado | MovilSinSalida | SalidaEnOtroMovil | MovilConOtraSalida:
    """El operador indico en que movil esta, despues del numero de salida: se
    suma a esa salida o se crea en el movil. Si el movil esta en otra salida (o
    la salida en otro movil), vuelve a pedir el numero de salida.

    Sin numero de salida previo (conversaciones de antes de pedir siempre el
    numero primero): se suma a la salida abierta del movil, o queda pendiente
    confirmar la creacion."""
    numero = conversacion.numero_reparto_pendiente
    if numero is None:
        reparto = reparto_abierto_del_movil(session, movil.id)
        if reparto is not None:
            asociar_operador(session, reparto, operador)
            return _confirmar(session, conversacion, movimiento, reparto, movil, SalidaUsada.EXISTENTE)
        conversacion.movil_pendiente_numero = movil.numero
        conversacion.estado = ConversationState.ESPERANDO_CONFIRMACION_CREAR_REPARTO
        session.commit()
        return MovilSinSalida(movil)

    salida = ubicar_salida(session, numero, movil)
    if isinstance(salida, Reparto):
        asociar_operador(session, salida, operador)
        return _confirmar(session, conversacion, movimiento, salida, movil, SalidaUsada.EXISTENTE)
    if isinstance(salida, SalidaNueva):
        reparto = crear_salida(session, operador, movil, numero)
        return _confirmar(session, conversacion, movimiento, reparto, movil, SalidaUsada.NUEVA)
    conversacion.numero_reparto_pendiente = None
    conversacion.movil_pendiente_numero = None
    conversacion.estado = ConversationState.ESPERANDO_NUMERO_REPARTO_NUEVO
    session.commit()
    return salida


def crear_salida_y_registrar(
    session: Session,
    operador: Operator,
    conversacion: WhatsAppConversation,
    movimiento: Movement,
    numero_movil: str | None,
    numero_reparto: int,
) -> Registrado | MovilNoDisponible:
    movil = buscar_movil_activo(session, numero_movil)
    if movil is None:
        conversacion.estado = ConversationState.ESPERANDO_MOVIL
        conversacion.movil_pendiente_numero = None
        conversacion.numero_reparto_pendiente = numero_reparto
        session.commit()
        return MovilNoDisponible()
    reparto = crear_salida(session, operador, movil, numero_reparto)
    return _confirmar(session, conversacion, movimiento, reparto, movil, SalidaUsada.NUEVA)
