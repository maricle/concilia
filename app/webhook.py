import hashlib
import hmac
import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .conversation import ConversationService
from .db import SessionLocal
from .extraction import extract_transfer
from .models import Operator
from .storage import save_comprobante_archivo

router = APIRouter()

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"

SI_NO_BOTONES = [("si", "Si"), ("no", "No")]
TIPO_FACTURA_CUENTA_BOTONES = [("factura", "Factura"), ("cuenta", "Cuenta")]

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


def _reply_buttons(service: ConversationService, numero: str) -> list[tuple[str, str]] | None:
    if service.needs_confirmation_keyboard(numero):
        return SI_NO_BOTONES
    if service.needs_tipo_keyboard(numero):
        return TIPO_FACTURA_CUENTA_BOTONES
    return None


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
            logging.exception("No se pudo enviar notificacion de WhatsApp a %s", numero_destino)


async def _download_whatsapp_media(media_id: str) -> tuple[bytes, str]:
    client = _get_http_client()
    headers = _auth_headers()
    info = await client.get(f"{GRAPH_API_BASE}/{media_id}", headers=headers)
    info.raise_for_status()
    datos = info.json()
    archivo = await client.get(datos["url"], headers=headers)
    archivo.raise_for_status()
    return archivo.content, datos.get("mime_type", "application/octet-stream")


def _ultimos_digitos(numero: str, cantidad: int = 10) -> str | None:
    digitos = re.sub(r"\D", "", numero)
    if len(digitos) < cantidad:
        return None
    return digitos[-cantidad:]


def _find_operator_by_numero(session: Session, from_numero: str) -> Operator | None:
    """El "from" que manda WhatsApp Business ya es el numero de telefono real del
    operador -- a diferencia de Telegram (donde el chat_id es opaco y hace falta
    un paso de "compartir contacto" para vincularlo), aca alcanza con comparar
    directo contra Operator.whatsapp_numero. Se compara solo por los ultimos 10
    digitos para tolerar el "9" de movil o el codigo de pais, que pueden venir
    formateados distinto de cada lado (mismo criterio que _find_operator_by_phone
    en telegram.py)."""
    clave = _ultimos_digitos(from_numero)
    if clave is None:
        return None
    operadores = session.scalars(select(Operator).where(Operator.activo.is_(True))).all()
    for operador in operadores:
        if _ultimos_digitos(operador.whatsapp_numero) == clave:
            return operador
    return None


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


@router.post("/webhook")
async def receive_webhook(request: Request) -> dict[str, str]:
    body = await request.body()
    if not verify_signature(body, request.headers.get("x-hub-signature-256"), get_settings().meta_app_secret):
        raise HTTPException(status_code=403, detail="Firma invalida")
    payload: dict[str, Any] = await request.json()

    for message in _messages(payload):
        await _handle_message(message)
    return {"status": "accepted"}


async def _handle_message(message: dict[str, Any]) -> None:
    from_numero = message.get("from")
    if not from_numero:
        return

    with SessionLocal() as session:
        operador = _find_operator_by_numero(session, from_numero)
    if operador is None:
        await send_whatsapp_message(from_numero, NUMERO_NO_HABILITADO_TEXTO)
        return
    # Clave canonica compartida con Telegram (ConversationService.handle_text ya
    # identifica al operador por este mismo numero alla) -- un comprobante
    # empezado por un canal se puede terminar por el otro sin perder el estado.
    numero = operador.whatsapp_numero

    tipo = message.get("type")
    texto: str | None = None
    if tipo == "text":
        texto = message.get("text", {}).get("body", "")
    elif tipo == "interactive":
        texto = message.get("interactive", {}).get("button_reply", {}).get("id", "")

    if texto is not None:
        with SessionLocal() as session:
            service = ConversationService(session)
            reply = service.handle_text(numero, texto)
            botones = _reply_buttons(service, numero)
            destinos = service.pop_notificaciones()
        await send_whatsapp_message(numero, reply, botones)
        await _enviar_notificaciones(destinos)
        return

    with SessionLocal() as session:
        service = ConversationService(session)
        pendiente = service.pending_prompt(numero)
        botones = _reply_buttons(service, numero)
    if pendiente is not None:
        await send_whatsapp_message(numero, pendiente, botones)
        return

    media_id = None
    file_name = "comprobante"
    content_type = "application/octet-stream"
    if tipo == "image":
        media_id = message["image"]["id"]
        content_type = message["image"].get("mime_type", "image/jpeg")
        file_name = f"{media_id}.jpg"
    elif tipo == "document":
        document = message["document"]
        media_id = document["id"]
        content_type = document.get("mime_type", content_type)
        file_name = document.get("filename", file_name)

    if media_id is None:
        await send_whatsapp_message(numero, "Envia una imagen o PDF del comprobante de transferencia.")
        return

    contenido, content_type_real = await _download_whatsapp_media(media_id)
    content_type = content_type_real or content_type

    # extract_transfer llama a la API de Claude de forma sincrona (varios
    # segundos, mas si hace fallback a Sonnet); correrla directo en el handler
    # async bloquearia el unico event loop del proceso, mismo motivo que en
    # telegram.py.
    transfer = await run_in_threadpool(extract_transfer, content_type, contenido)
    if transfer is None:
        await send_whatsapp_message(
            numero, "No pude leer el comprobante. Reenvialo con mejor calidad o ingresa los datos manualmente."
        )
        return

    archivo_id = None
    try:
        archivo_id = save_comprobante_archivo(nombre_archivo=file_name, content_type=content_type, contenido=contenido)
    except Exception:
        logging.exception("No se pudo guardar el archivo del comprobante; se continua sin bloquear el registro.")

    with SessionLocal() as session:
        service = ConversationService(session)
        reply = service.start_transfer(numero, transfer, archivo_id)
        botones = _reply_buttons(service, numero)
    await send_whatsapp_message(numero, reply, botones)


def _messages(payload: dict[str, Any]):
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            yield from change.get("value", {}).get("messages", [])
