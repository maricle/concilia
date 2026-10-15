import logging
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .canales import (
    PEDIR_COMPROBANTE_TEXTO,
    Respuesta,
    Teclado,
    buscar_operador_por_telefono,
    procesar_comprobante,
    recordatorio_de_pendiente,
    responder_texto,
    turno_de,
    ultimos_digitos,
)
from .config import get_settings
from .db import SessionLocal
from .models import Operator

router = APIRouter()
logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"

CONTACT_REQUEST_MARKUP = {
    "keyboard": [[{"text": "Compartir mi numero", "request_contact": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

SI_NO_MARKUP = {
    "inline_keyboard": [[{"text": "Si", "callback_data": "si"}, {"text": "No", "callback_data": "no"}]]
}

TIPO_FACTURA_CUENTA_MARKUP = {
    "inline_keyboard": [
        [
            {"text": "Factura", "callback_data": "factura"},
            {"text": "Cuenta", "callback_data": "cuenta"},
            {"text": "Descartar", "callback_data": "descartar"},
        ]
    ]
}

DESCARTAR_MARKUP = {"inline_keyboard": [[{"text": "Descartar", "callback_data": "descartar"}]]}
MARKUP_POR_TECLADO = {
    Teclado.SI_NO: SI_NO_MARKUP,
    Teclado.TIPO_FACTURA_CUENTA: TIPO_FACTURA_CUENTA_MARKUP,
    Teclado.DESCARTAR: DESCARTAR_MARKUP,
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


def _resolver_chat_ids(session: Session, notificaciones: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Traduce (whatsapp_numero, mensaje) a (chat_id, mensaje), descartando a los
    operadores que todavia no vincularon su Telegram (no hay adonde mandarles)."""
    destinos: list[tuple[str, str]] = []
    for numero, mensaje in notificaciones:
        operador = session.scalar(select(Operator).where(Operator.whatsapp_numero == numero))
        if operador is not None and operador.telegram_chat_id:
            destinos.append((operador.telegram_chat_id, mensaje))
    return destinos


async def _enviar_notificaciones(destinos: list[tuple[str, str]]) -> None:
    for chat_id_destino, mensaje in destinos:
        try:
            await send_telegram_message(chat_id_destino, mensaje)
        except httpx.HTTPError:
            logger.exception("No se pudo enviar notificacion a chat_id=%s", chat_id_destino)




async def _answer_callback_query(callback_query_id: str) -> None:
    """Le avisa a Telegram que se recibio el click (apaga el spinner del boton).
    Es solo cosmetico -- si Telegram la rechaza (ej. "query is too old"), no debe
    impedir que se procese la respuesta real del operador."""
    try:
        response = await _get_http_client().post(
            _api_url("answerCallbackQuery"), json={"callback_query_id": callback_query_id}
        )
        response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("No se pudo confirmar el callback_query a Telegram; se continua igual.")


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


def _find_operator_by_phone(session: Session, phone_number: str) -> Operator | None:
    operador = buscar_operador_por_telefono(session, phone_number, solo_sin_telegram=True)
    if operador is None:
        logger.warning(
            "Contacto de Telegram sin operador coincidente: telefono=%r ultimos_digitos=%r",
            phone_number,
            ultimos_digitos(phone_number),
        )
    return operador


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


async def _responder(chat_id: str, respuesta: Respuesta) -> None:
    await send_telegram_message(chat_id, respuesta.texto, reply_markup=MARKUP_POR_TECLADO.get(respuesta.teclado))
    if respuesta.notificaciones:
        with SessionLocal() as session:
            destinos = _resolver_chat_ids(session, respuesta.notificaciones)
        await _enviar_notificaciones(destinos)


@router.post("/telegram/webhook")
async def receive_telegram_update(
    request: Request,
    secret_token: str | None = Header(default=None, alias="x-telegram-bot-api-secret-token"),
) -> dict[str, str]:
    settings = get_settings()
    if settings.telegram_webhook_secret and secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Token de webhook invalido")

    update: dict[str, Any] = await request.json()

    callback_query = update.get("callback_query")
    if callback_query is not None:
        chat_id = str(callback_query["message"]["chat"]["id"])
        await _answer_callback_query(callback_query["id"])
        numero = await _resolve_operator_numero(chat_id, callback_query)
        if numero is None:
            return {"status": "ignored"}
        async with turno_de(numero):
            await _responder(chat_id, responder_texto(numero, callback_query.get("data", "")))
        return {"status": "accepted"}

    message = update.get("message")
    if message is None:
        return {"status": "ignored"}

    chat_id = str(message["chat"]["id"])
    numero = await _resolve_operator_numero(chat_id, message)
    if numero is None:
        return {"status": "ignored"}
    async with turno_de(numero):
        return await _procesar_mensaje(chat_id, numero, message)


async def _procesar_mensaje(chat_id: str, numero: str, message: dict[str, Any]) -> dict[str, str]:
    if "text" in message:
        await _responder(chat_id, responder_texto(numero, message["text"]))
        return {"status": "accepted"}

    pendiente = recordatorio_de_pendiente(numero)
    if pendiente is not None:
        await _responder(chat_id, pendiente)
        return {"status": "ignored"}

    if "photo" in message:
        file_id = message["photo"][-1]["file_id"]
        nombre_archivo = f"{file_id}.jpg"
        content_type = "image/jpeg"
    elif "document" in message:
        file_id = message["document"]["file_id"]
        nombre_archivo = message["document"].get("file_name", "comprobante")
        content_type = message["document"].get("mime_type", "application/octet-stream")
    else:
        await send_telegram_message(chat_id, PEDIR_COMPROBANTE_TEXTO)
        return {"status": "ignored"}

    async def descargar() -> tuple[bytes, str]:
        # Telegram devuelve la ruta del archivo, no su tipo: se usa el del mensaje.
        contenido, _ = await _download_telegram_file(file_id)
        return contenido, content_type

    # Siempre 200, aun si falla la extraccion: con un 500 Telegram reintenta la
    # misma entrega una y otra vez.
    respuesta = await procesar_comprobante(
        numero, descargar, nombre_archivo=nombre_archivo, caption=message.get("caption")
    )
    await _responder(chat_id, respuesta)
    return {"status": "accepted"}
