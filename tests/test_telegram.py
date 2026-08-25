from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from app import telegram
from app.config import Settings
from app.conversation import ExtractedTransfer
from app.db import Base, engine
from app.main import app


def setup_function():
    Base.metadata.create_all(engine)


def test_text_message_from_unregistered_number_is_rejected(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str) -> None:
        sent.append((chat_id, text))

    monkeypatch.setattr(telegram, "send_telegram_message", fake_send)

    client = TestClient(app)
    response = client.post("/telegram/webhook", json={"message": {"chat": {"id": 111}, "text": "hola"}})

    assert response.status_code == 200
    assert sent == [("111", "Este numero no esta habilitado para registrar comprobantes.")]


def test_webhook_secret_mismatch_is_rejected(monkeypatch):
    monkeypatch.setattr(telegram, "get_settings", lambda: Settings(telegram_webhook_secret="expected"))

    client = TestClient(app)
    response = client.post(
        "/telegram/webhook",
        json={"message": {"chat": {"id": 111}, "text": "hola"}},
        headers={"x-telegram-bot-api-secret-token": "wrong"},
    )

    assert response.status_code == 403


def test_photo_message_is_extracted_and_saved_to_test_store(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str) -> None:
        sent.append((chat_id, text))

    async def fake_download(file_id: str) -> tuple[bytes, str]:
        return b"fake-photo-bytes", "photos/file.jpg"

    def fake_extract(content_type: str, data: bytes) -> ExtractedTransfer:
        return ExtractedTransfer(Decimal("100.00"), datetime(2026, 8, 24), "OP-999")

    saved: list[bytes] = []

    def fake_save(nombre_archivo: str, content_type: str, contenido: bytes, numero_operacion=None) -> int:
        saved.append(contenido)
        return 1

    monkeypatch.setattr(telegram, "send_telegram_message", fake_send)
    monkeypatch.setattr(telegram, "_download_telegram_file", fake_download)
    monkeypatch.setattr(telegram, "extract_transfer", fake_extract)
    monkeypatch.setattr(telegram, "save_comprobante_prueba", fake_save)

    client = TestClient(app)
    response = client.post(
        "/telegram/webhook",
        json={"message": {"chat": {"id": 222}, "photo": [{"file_id": "abc", "file_size": 100}]}},
    )

    assert response.status_code == 200
    assert saved == [b"fake-photo-bytes"]
    assert sent == [("222", "Este numero no esta habilitado para registrar comprobantes.")]


def test_photo_message_with_unreadable_receipt_asks_to_resend(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str) -> None:
        sent.append((chat_id, text))

    async def fake_download(file_id: str) -> tuple[bytes, str]:
        return b"fake-photo-bytes", "photos/file.jpg"

    monkeypatch.setattr(telegram, "send_telegram_message", fake_send)
    monkeypatch.setattr(telegram, "_download_telegram_file", fake_download)
    monkeypatch.setattr(telegram, "extract_transfer", lambda content_type, data: None)

    client = TestClient(app)
    response = client.post(
        "/telegram/webhook",
        json={"message": {"chat": {"id": 333}, "photo": [{"file_id": "abc", "file_size": 100}]}},
    )

    assert response.status_code == 200
    assert sent == [("333", "No pude leer el comprobante. Reenvialo con mejor calidad o ingresa los datos manualmente.")]
