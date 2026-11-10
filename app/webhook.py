import hashlib
import hmac
import logging
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request

from .canales import (
    PEDIR_COMPROBANTE_TEXTO,
    Respuesta,
    Teclado,
    buscar_operador_por_telefono,
    procesar_comprobante,
    recordatorio_de_pendiente,
    responder_texto,
    turno_de,
)
from .config import get_settings
from .db import SessionLocal

router = APIRouter()
logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"

SI_NO_BOTONES = [("si", "Si"), ("no", "No")]
TIPO_FACTURA_CUENTA_BOTONES = [("factura", "Factura"), ("cuenta", "Cuenta"), ("descartar", "Descartar")]
DESCARTAR_BOTONES = [("descartar", "Descartar")]
BOTONES_POR_TECLADO = {
    Teclado.SI_NO: SI_NO_BOTONES,
    Teclado.TIPO_FACTURA_CUENTA: TIPO_FACTURA_CUENTA_BOTONES,
    Teclado.DESCARTAR: DESCARTAR_BOTONES,
}

NUMERO_NO_HABILITADO_TEXTO = (
    "Este numero no esta habilitado para registrar comprobantes. Contacta al administrador para que te registre."
)


def verify_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if not secret:
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:], expected)


def _api_url() -> str:
    return f"{GRAPH_API_BASE}/{get_settings().wa_phone_id}/messages"


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().wa_access_token}"}


_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """Cliente httpx compartido entre requests, mismo criterio que telegram.py:
    evita pagar handshake TCP/TLS nuevo en cada llamada a la API de Meta."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient()
    return _http_client


async def send_whatsapp_message(to: str, text: str, botones: list[tuple[str, str]] | None = None) -> None:
    if botones:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": text},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": id_, "title": titulo}} for id_, titulo in botones
                    ]
                },
            },
        }
    else:
        payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    response = await _get_http_client().post(_api_url(), json=payload, headers=_auth_headers())
    response.raise_for_status()


async def _enviar_notificaciones(destinos: list[tuple[str, str]]) -> None:
    # A diferencia de Telegram, no hace falta resolver nada: pop_notificaciones()
    # ya devuelve (whatsapp_numero, mensaje), y whatsapp_numero es directamente
    # la direccion a la que se manda por WhatsApp.
    for numero_destino, mensaje in destinos:
        try:
            await send_whatsapp_message(numero_destino, mensaje)
        except httpx.HTTPError:
            logger.exception("No se pudo enviar notificacion de WhatsApp a %s", numero_destino)


async def _download_whatsapp_media(media_id: str) -> tuple[bytes, str]:
    client = _get_http_client()
    headers = _auth_headers()
    info = await client.get(f"{GRAPH_API_BASE}/{media_id}", headers=headers)
    info.raise_for_status()
    datos = info.json()
    archivo = await client.get(datos["url"], headers=headers)
    archivo.raise_for_status()
    return archivo.content, datos.get("mime_type", "application/octet-stream")


# Desde octubre de 2026 Meta cobra cada respuesta del bot, asi que a un numero
# no registrado (o a alguien que escribe por error) se le avisa una sola vez por
# ventana de 24h en vez de contestarle cada mensaje. En memoria del proceso: si
# se reinicia, a lo sumo se repite el aviso una vez, no hace falta persistirlo.
_AVISO_NO_HABILITADO_CADA = timedelta(hours=24)
_avisos_no_habilitado: dict[str, datetime] = {}


def _debe_avisar_no_habilitado(numero: str) -> bool:
    ahora = datetime.now()
    ultimo = _avisos_no_habilitado.get(numero)
    if ultimo is not None and ahora - ultimo < _AVISO_NO_HABILITADO_CADA:
        return False
    _avisos_no_habilitado[numero] = ahora
    return True


@router.get("/webhook")
def verify_webhook(
    mode: str | None = Query(default=None, alias="hub.mode"),
    token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> int | str:
    settings = get_settings()
    if mode == "subscribe" and token == settings.meta_verify_token:
        return challenge or ""
    raise HTTPException(status_code=403, detail="Token de verificacion invalido")


# Meta reintenta la entrega si no recibe un 200 a tiempo. Procesar un comprobante
# (bajar el archivo + extraccion con Claude, mas si hace fallback a Sonnet) puede
# tardar varios segundos, asi que el 200 se devuelve apenas validada la firma y el
# mensaje se procesa despues (BackgroundTasks). Igual se deduplica por el "id" del
# mensaje: un reintento de Meta no tiene que crear un segundo borrador ni gastar
# otra extraccion ni otra respuesta cobrada. En memoria del proceso: alcanza para
# la ventana de reintentos de Meta (minutos), no hace falta persistirlo.
_MAX_MENSAJES_RECORDADOS = 2000
_mensajes_procesados: OrderedDict[str, None] = OrderedDict()


def _es_mensaje_repetido(message_id: str | None) -> bool:
    if not message_id:
        return False
    if message_id in _mensajes_procesados:
        return True
    _mensajes_procesados[message_id] = None
    if len(_mensajes_procesados) > _MAX_MENSAJES_RECORDADOS:
        _mensajes_procesados.popitem(last=False)
    return False


async def _handle_message_seguro(message: dict[str, Any]) -> None:
    try:
        await _handle_message(message)
    except Exception:
        logger.exception("Error procesando mensaje de WhatsApp %s", message.get("id"))


@router.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    body = await request.body()
    if not verify_signature(body, request.headers.get("x-hub-signature-256"), get_settings().meta_app_secret):
        raise HTTPException(status_code=403, detail="Firma invalida")
    payload: dict[str, Any] = await request.json()

    for message in _messages(payload):
        if _es_mensaje_repetido(message.get("id")):
            continue
        background_tasks.add_task(_handle_message_seguro, message)
    return {"status": "accepted"}


async def _responder(numero: str, respuesta: Respuesta) -> None:
    await send_whatsapp_message(numero, respuesta.texto, BOTONES_POR_TECLADO.get(respuesta.teclado))
    await _enviar_notificaciones(respuesta.notificaciones)


async def _handle_message(message: dict[str, Any]) -> None:
    from_numero = message.get("from")
    if not from_numero:
        return

    # El "from" que manda WhatsApp ya es el telefono real del operador: no hace
    # falta el paso de "compartir contacto" que necesita Telegram.
    with SessionLocal() as session:
        operador = buscar_operador_por_telefono(session, from_numero)
    if operador is None:
        if _debe_avisar_no_habilitado(from_numero):
            await send_whatsapp_message(from_numero, NUMERO_NO_HABILITADO_TEXTO)
        return
    # Clave canonica compartida con Telegram y la web: un comprobante empezado
    # por un canal se puede terminar por otro sin perder el estado.
    numero = operador.whatsapp_numero
    async with turno_de(numero):
        await _procesar_mensaje(numero, message)


async def _procesar_mensaje(numero: str, message: dict[str, Any]) -> None:
    tipo = message.get("type")
    texto: str | None = None
    if tipo == "text":
        texto = message.get("text", {}).get("body", "")
    elif tipo == "interactive":
        texto = message.get("interactive", {}).get("button_reply", {}).get("id", "")
    if texto is not None:
        await _responder(numero, responder_texto(numero, texto))
        return

    pendiente = recordatorio_de_pendiente(numero)
    if pendiente is not None:
        await _responder(numero, pendiente)
        return

    adjunto = message.get(tipo) if tipo in ("image", "document") else None
    if adjunto is None:
        await send_whatsapp_message(numero, PEDIR_COMPROBANTE_TEXTO)
        return
    media_id = adjunto["id"]
    content_type_declarado = adjunto.get("mime_type", "image/jpeg" if tipo == "image" else "application/octet-stream")
    nombre_archivo = f"{media_id}.jpg" if tipo == "image" else adjunto.get("filename", "comprobante")

    async def descargar() -> tuple[bytes, str]:
        contenido, content_type = await _download_whatsapp_media(media_id)
        return contenido, content_type or content_type_declarado

    respuesta = await procesar_comprobante(
        numero, descargar, nombre_archivo=nombre_archivo, caption=adjunto.get("caption")
    )
    await _responder(numero, respuesta)


def _messages(payload: dict[str, Any]):
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            yield from change.get("value", {}).get("messages", [])
