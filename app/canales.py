"""Logica comun a los dos canales de chat (WhatsApp en webhook.py, Telegram en
telegram.py).

Los dos hacen exactamente lo mismo con un mensaje una vez que identificaron al
operador: pasarle el texto a ConversationService, o bajar el comprobante,
interpretarlo con Claude, guardarlo y arrancar la carga. Lo unico propio de cada
canal es como llega el pedido, como se manda la respuesta y como se dibujan los
botones -- por eso aca se devuelve una `Respuesta` con un `Teclado` abstracto, y
cada canal lo traduce a su formato (botones interactivos de WhatsApp o
inline_keyboard de Telegram)."""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from .conversation import ConversationService
from .db import SessionLocal
from .extraction import extract_transfer
from .models import Operator
from .storage import save_comprobante_archivo

logger = logging.getLogger(__name__)

PEDIR_COMPROBANTE_TEXTO = "Envia una imagen o PDF del comprobante de transferencia."
NO_PUDE_LEER_TEXTO = (
    "No pude leer el comprobante. Reenvialo con mejor calidad, o pedile al administrador que lo cargue desde el panel."
)
ERROR_PROCESANDO_TEXTO = (
    "Tuvimos un problema para procesar el comprobante. Reenvialo en unos minutos; "
    "si sigue fallando, avisale al administrador."
)


class Teclado(StrEnum):
    SI_NO = "si_no"
    TIPO_FACTURA_CUENTA = "tipo_factura_cuenta"
    DESCARTAR = "descartar"


@dataclass
class Respuesta:
    texto: str
    teclado: Teclado | None = None
    # (whatsapp_numero, mensaje) para OTROS operadores -- ej. los companeros de
    # una salida que se cierra. Cada canal resuelve como llegarles.
    notificaciones: list[tuple[str, str]] = field(default_factory=list)


# Un turno (lock) por operador, compartido por WhatsApp, Telegram y la web, con
# la misma clave que la conversacion: Operator.whatsapp_numero. Los pedidos de un
# mismo operador se procesan de a uno (una rafaga "foto + F 123" no se cruza, y
# dos canales no pueden crear dos borradores a la vez); los de operadores
# distintos siguen en paralelo. En memoria del proceso: alcanza porque la app
# corre en un solo proceso de uvicorn (ver Procfile); con varios workers haria
# falta un lock en la base.
_turnos: dict[str, asyncio.Lock] = {}


def turno_de(numero: str) -> asyncio.Lock:
    return _turnos.setdefault(numero, asyncio.Lock())


def ultimos_digitos(numero: str, cantidad: int = 10) -> str | None:
    """En Argentina el "9" de movil, el codigo de pais y el formato varian segun
    quien cargo el numero, pero los ultimos 10 digitos (el numero local) son
    siempre los mismos de un lado y del otro."""
    digitos = re.sub(r"\D", "", numero)
    if len(digitos) < cantidad:
        return None
    return digitos[-cantidad:]


def buscar_operador_por_telefono(session: Session, telefono: str, *, solo_sin_telegram: bool = False) -> Operator | None:
    """Operador activo cuyo whatsapp_numero coincide en los ultimos 10 digitos.
    La comparacion se hace en Python porque el numero se guarda con el formato
    que lo cargo el administrador (espacios, guiones, +54...)."""
    clave = ultimos_digitos(telefono)
    if clave is None:
        return None
    consulta = select(Operator).where(Operator.activo.is_(True))
    if solo_sin_telegram:
        consulta = consulta.where(Operator.telegram_chat_id.is_(None))
    for operador in session.scalars(consulta):
        if ultimos_digitos(operador.whatsapp_numero) == clave:
            return operador
    return None


def teclado_para(service: ConversationService, numero: str) -> Teclado | None:
    if service.needs_confirmation_keyboard(numero):
        return Teclado.SI_NO
    if service.needs_tipo_keyboard(numero):
        return Teclado.TIPO_FACTURA_CUENTA
    if service.needs_descartar_keyboard(numero):
        return Teclado.DESCARTAR
    return None


def responder_texto(numero: str, texto: str) -> Respuesta:
    """Texto libre o boton tocado (el id del boton llega como texto)."""
    with SessionLocal() as session:
        service = ConversationService(session)
        respuesta = service.handle_text(numero, texto)
        return Respuesta(respuesta, teclado_para(service, numero), service.pop_notificaciones())


def recordatorio_de_pendiente(numero: str) -> Respuesta | None:
    """Si el operador tiene un comprobante a medio cargar, lo que hay que
    recordarle en vez de procesar uno nuevo (para no dejar el anterior huerfano).
    None si puede mandar uno nuevo."""
    with SessionLocal() as session:
        service = ConversationService(session)
        pendiente = service.pending_prompt(numero)
        if pendiente is None:
            return None
        return Respuesta(pendiente, teclado_para(service, numero))


async def procesar_comprobante(
    numero: str,
    descargar: Callable[[], Awaitable[tuple[bytes, str]]],
    *,
    nombre_archivo: str,
    caption: str | None,
) -> Respuesta:
    """Baja el archivo (`descargar` devuelve contenido y content type), lo
    interpreta con Claude, guarda el original y arranca la carga del comprobante."""
    try:
        contenido, content_type = await descargar()
        # extract_transfer llama a la API de Claude de forma sincrona (varios
        # segundos, mas si hace fallback a Sonnet): en un thread del pool, para no
        # congelar el event loop y con el a los demas operadores.
        transfer = await run_in_threadpool(extract_transfer, content_type, contenido)
    except Exception:
        # API de Meta/Telegram o de Anthropic caida, timeout, etc.: el operador
        # tiene que recibir una respuesta igual.
        logger.exception("No se pudo descargar o interpretar el comprobante de %s", numero)
        return Respuesta(ERROR_PROCESANDO_TEXTO)
    if transfer is None:
        return Respuesta(NO_PUDE_LEER_TEXTO)

    archivo_id = None
    try:
        archivo_id = save_comprobante_archivo(nombre_archivo=nombre_archivo, content_type=content_type, contenido=contenido)
    except Exception:
        logger.exception("No se pudo guardar el archivo del comprobante; se continua sin bloquear el registro.")

    with SessionLocal() as session:
        service = ConversationService(session)
        respuesta = service.start_transfer(numero, transfer, archivo_id, caption=caption)
        return Respuesta(respuesta, teclado_para(service, numero))
