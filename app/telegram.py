import logging
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Request

from .config import get_settings
from .conversation import ConversationService
from .db import SessionLocal
from .extraction import extract_transfer
from .storage import save_comprobante_prueba

router = APIRouter()

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"


def _api_url(method: str) -> str:
    return f"{TELEGRAM_API_BASE.format(token=get_settings().telegram_bot_token)}/{method}"


async def send_telegram_message(chat_id: str, text: str) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(_api_url("sendMessage"), json={"chat_id": chat_id, "text": text})
        response.raise_for_status()


async def _download_telegram_file(file_id: str) -> tuple[bytes, str]:
    async with httpx.AsyncClient() as client:
        info = await client.get(_api_url("getFile"), params={"file_id": file_id})
        info.raise_for_status()
        file_path = info.json()["result"]["file_path"]
        token = get_settings().telegram_bot_token
        download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        file_response = await client.get(download_url)
        file_response.raise_for_status()
        return file_response.content, file_path


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

    if "text" in message:
        with SessionLocal() as session:
            reply = ConversationService(session).handle_text(chat_id, message["text"])
        await send_telegram_message(chat_id, reply)
        return {"status": "accepted"}

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

    try:
        save_comprobante_prueba(
            nombre_archivo=file_name,
            content_type=content_type,
            contenido=contenido,
            numero_operacion=transfer.numero_operacion,
        )
    except Exception:
        logging.exception("No se pudo guardar el archivo de prueba en Turso; se continua sin bloquear el registro.")

    with SessionLocal() as session:
        reply = ConversationService(session).start_transfer(chat_id, transfer)
    await send_telegram_message(chat_id, reply)
    return {"status": "accepted"}
