import hashlib
import hmac
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from .config import get_settings
from .conversation import ConversationService
from .db import SessionLocal
from .models import Operator

router = APIRouter()


def verify_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if not secret:
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:], expected)


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
        with SessionLocal() as session:
            number = message.get("from")
            if not number:
                continue
            service = ConversationService(session)
            if message.get("type") == "text":
                service.handle_text(number, message.get("text", {}).get("body", ""))
    return {"status": "accepted"}


def _messages(payload: dict[str, Any]):
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            yield from change.get("value", {}).get("messages", [])
