import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .conversation import ConversationService
from .db import SessionLocal
from .extraction import extract_transfer
from .models import Operator
from .storage import save_comprobante_prueba

router = APIRouter()

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"

CONTACT_REQUEST_MARKUP = {
    "keyboard": [[{"text": "Compartir mi numero", "request_contact": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

PEDIR_TELEFONO_TEXTO = "Para registrar comprobantes primero compartinos tu numero de telefono con el boton de abajo."
NUMERO_NO_HABILITADO_TEXTO = "Tu numero no esta habilitado. Contacta al administrador para que te registre."
NUMERO_VINCULADO_TEXTO = "Numero vinculado correctamente. Ya podes enviar tus comprobantes."


def _api_url(method: str) -> str:
    return f"{TELEGRAM_API_BASE.format(token=get_settings().telegram_bot_token)}/{method}"


_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """Cliente httpx compartido entre requests: evita pagar handshake TCP/TLS nuevo
    en cada llamada a la API de Telegram (una misma actualizacion suele hacer 2-3)."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient()
    return _http_client


async def send_telegram_message(chat_id: str, text: str, reply_markup: dict | None = None) -> None:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    response = await _get_http_client().post(_api_url("sendMessage"), json=payload)
    response.raise_for_status()


async def _download_telegram_file(file_id: str) -> tuple[bytes, str]:
    client = _get_http_client()
    info = await client.get(_api_url("getFile"), params={"file_id": file_id})
    info.raise_for_status()
    file_path = info.json()["result"]["file_path"]
    token = get_settings().telegram_bot_token
    download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    file_response = await client.get(download_url)
    file_response.raise_for_status()
    return file_response.content, file_path


def _find_operator_by_chat_id(session: Session, chat_id: str) -> Operator | None:
    return session.scalar(
        select(Operator).where(
            Operator.activo.is_(True),
            (Operator.telegram_chat_id == chat_id) | (Operator.whatsapp_numero == chat_id),
        )
    )


def _ultimos_digitos(numero: str, cantidad: int = 10) -> str | None:
    digitos = re.sub(r"\D", "", numero)
    if len(digitos) < cantidad:
        return None
    return digitos[-cantidad:]


def _find_operator_by_phone(session: Session, phone_number: str) -> Operator | None:
    """Compara solo los ultimos 10 digitos: en Argentina el "9" de movil, el codigo de
    pais y el formato varian segun quien cargo el numero, pero los ultimos 10 digitos
    (el numero local) son siempre los mismos de un lado y del otro."""
    clave = _ultimos_digitos(phone_number)
    if clave is None:
        return None
    candidatos = session.scalars(
        select(Operator).where(Operator.activo.is_(True), Operator.telegram_chat_id.is_(None))
    ).all()
    for operador in candidatos:
        if _ultimos_digitos(operador.whatsapp_numero) == clave:
            return operador
    logging.warning(
        "Contacto de Telegram sin operador coincidente: telefono=%r ultimos_digitos=%r candidatos=%r",
        phone_number,
        clave,
        [_ultimos_digitos(o.whatsapp_numero) for o in candidatos],
    )
    return None


async def _resolve_operator_numero(chat_id: str, message: dict[str, Any]) -> str | None:
    """Devuelve el whatsapp_numero del operador vinculado a este chat, o None si hay que
    pedirle/validarle el telefono (y ya se le respondio en ese caso)."""
    with SessionLocal() as session:
        operador = _find_operator_by_chat_id(session, chat_id)
        if operador is not None:
            return operador.whatsapp_numero

        contact = message.get("contact")
        if contact is not None and str(contact.get("user_id", "")) == chat_id:
            operador = _find_operator_by_phone(session, contact["phone_number"])
            if operador is None:
                await send_telegram_message(chat_id, NUMERO_NO_HABILITADO_TEXTO, reply_markup=CONTACT_REQUEST_MARKUP)
                return None
            operador.telegram_chat_id = chat_id
            session.commit()
            await send_telegram_message(chat_id, NUMERO_VINCULADO_TEXTO)
            return None

    await send_telegram_message(chat_id, PEDIR_TELEFONO_TEXTO, reply_markup=CONTACT_REQUEST_MARKUP)
    return None


@router.post("/telegram/webhook")
async def receive_telegram_update(
    request: Request,
    secret_token: str | None = Header(default=None, alias="x-telegram-bot-api-secret-token"),
) -> dict[str, str]:
    settings = get_settings()
    if settings.telegram_webhook_secret and secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Token de webhook invalido")

    update: dict[str, Any] = await request.json()
    message = update.get("message")
    if message is None:
        return {"status": "ignored"}

    chat_id = str(message["chat"]["id"])

    numero = await _resolve_operator_numero(chat_id, message)
    if numero is None:
        return {"status": "ignored"}

    if "text" in message:
        with SessionLocal() as session:
            reply = ConversationService(session).handle_text(numero, message["text"])
        await send_telegram_message(chat_id, reply)
        return {"status": "accepted"}

    with SessionLocal() as session:
        pendiente = ConversationService(session).pending_prompt(numero)
    if pendiente is not None:
        await send_telegram_message(chat_id, pendiente)
        return {"status": "ignored"}

    file_id = None
    file_name = "comprobante"
    content_type = "application/octet-stream"
    if "photo" in message:
        file_id = message["photo"][-1]["file_id"]
        file_name = f"{file_id}.jpg"
        content_type = "image/jpeg"
    elif "document" in message:
        document = message["document"]
        file_id = document["file_id"]
        file_name = document.get("file_name", file_name)
        content_type = document.get("mime_type", content_type)

    if file_id is None:
        await send_telegram_message(chat_id, "Envia una imagen o PDF del comprobante de transferencia.")
        return {"status": "ignored"}

    contenido, _ = await _download_telegram_file(file_id)
    transfer = extract_transfer(content_type, contenido)
    if transfer is None:
        await send_telegram_message(
            chat_id,
            "No pude leer el comprobante. Reenvialo con mejor calidad o ingresa los datos manualmente.",
        )
        return {"status": "accepted"}

    archivo_prueba_id = None
    try:
        archivo_prueba_id = save_comprobante_prueba(
            nombre_archivo=file_name,
            content_type=content_type,
            contenido=contenido,
            numero_operacion=transfer.numero_operacion,
        )
    except Exception:
        logging.exception("No se pudo guardar el archivo de prueba en Turso; se continua sin bloquear el registro.")

    with SessionLocal() as session:
        reply = ConversationService(session).start_transfer(numero, transfer, archivo_prueba_id)
    await send_telegram_message(chat_id, reply)
    return {"status": "accepted"}
