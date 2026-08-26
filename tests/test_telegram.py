from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import telegram
from app.config import Settings
from app.conversation import ExtractedTransfer
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import BankAccount, Movement, Operator

client = TestClient(app)


def setup_function():
    Base.metadata.create_all(engine)


def _clean_movement(numero_operacion: str) -> None:
    with SessionLocal() as session:
        for movimiento in session.scalars(select(Movement).where(Movement.numero_operacion == numero_operacion)).all():
            session.delete(movimiento)
        session.commit()


def _clean_operator(*, whatsapp_numero: str | None = None, telegram_chat_id: str | None = None) -> None:
    with SessionLocal() as session:
        query = select(Operator)
        if whatsapp_numero is not None:
            query = query.where(Operator.whatsapp_numero == whatsapp_numero)
        else:
            query = query.where(Operator.telegram_chat_id == telegram_chat_id)
        for operador in session.scalars(query).all():
            session.delete(operador)
        session.commit()


def _register_operator(whatsapp_numero: str, telegram_chat_id: str | None = None) -> None:
    _clean_operator(whatsapp_numero=whatsapp_numero)
    with SessionLocal() as session:
        session.add(Operator(nombre="Test", whatsapp_numero=whatsapp_numero, telegram_chat_id=telegram_chat_id))
        session.commit()


def test_text_from_unlinked_chat_is_asked_to_share_phone(monkeypatch):
    sent: list[tuple[str, str, dict | None]] = []

    async def fake_send(chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        sent.append((chat_id, text, reply_markup))

    monkeypatch.setattr(telegram, "send_telegram_message", fake_send)

    response = client.post("/telegram/webhook", json={"message": {"chat": {"id": 111}, "text": "hola"}})

    assert response.status_code == 200
    assert len(sent) == 1
    chat_id, text, reply_markup = sent[0]
    assert chat_id == "111"
    assert text == telegram.PEDIR_TELEFONO_TEXTO
    assert reply_markup == telegram.CONTACT_REQUEST_MARKUP


def test_webhook_secret_mismatch_is_rejected(monkeypatch):
    monkeypatch.setattr(telegram, "get_settings", lambda: Settings(telegram_webhook_secret="expected"))

    response = client.post(
        "/telegram/webhook",
        json={"message": {"chat": {"id": 111}, "text": "hola"}},
        headers={"x-telegram-bot-api-secret-token": "wrong"},
    )

    assert response.status_code == 403


def test_photo_message_is_extracted_and_saved_to_test_store(monkeypatch):
    _register_operator("222")
    _clean_movement("OP-999")
    with SessionLocal() as session:
        if session.scalar(select(BankAccount).where(BankAccount.alias == "empresa.mp")) is None:
            session.add(BankAccount(banco="Mercado Pago", numero_cuenta="1", alias="empresa.mp"))
            session.commit()

    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        sent.append((chat_id, text))

    async def fake_download(file_id: str) -> tuple[bytes, str]:
        return b"fake-photo-bytes", "photos/file.jpg"

    def fake_extract(content_type: str, data: bytes, cuentas_validas=None) -> ExtractedTransfer:
        return ExtractedTransfer(Decimal("100.00"), datetime(2026, 8, 24), "OP-999", cuenta_receptora="empresa.mp")

    saved: list[bytes] = []

    def fake_save(nombre_archivo: str, content_type: str, contenido: bytes) -> int:
        saved.append(contenido)
        return 1

    monkeypatch.setattr(telegram, "send_telegram_message", fake_send)
    monkeypatch.setattr(telegram, "_download_telegram_file", fake_download)
    monkeypatch.setattr(telegram, "extract_transfer", fake_extract)
    monkeypatch.setattr(telegram, "save_comprobante_archivo", fake_save)

    response = client.post(
        "/telegram/webhook",
        json={"message": {"chat": {"id": 222}, "photo": [{"file_id": "abc", "file_size": 100}]}},
    )

    assert response.status_code == 200
    assert saved == [b"fake-photo-bytes"]
    assert sent == [
        (
            "222",
            "Monto: $100.00\nFecha: 2026-08-24\nBanco emisor: no detectado\nOperacion: OP-999\n"
            "Factura/cuenta: pendiente\n\nResponde SI para confirmar o NO para descartar.",
        )
    ]


def test_second_photo_while_a_draft_is_pending_does_not_orphan_the_first(monkeypatch):
    _register_operator("888")
    _clean_movement("OP-PRIMERO")
    _clean_movement("OP-SEGUNDO")
    with SessionLocal() as session:
        if session.scalar(select(BankAccount).where(BankAccount.alias == "empresa.mp")) is None:
            session.add(BankAccount(banco="Mercado Pago", numero_cuenta="1", alias="empresa.mp"))
            session.commit()

    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        sent.append((chat_id, text))

    async def fake_download(file_id: str) -> tuple[bytes, str]:
        return b"fake-photo-bytes", "photos/file.jpg"

    extract_calls = []

    def fake_extract(content_type: str, data: bytes, cuentas_validas=None) -> ExtractedTransfer:
        extract_calls.append(1)
        return ExtractedTransfer(Decimal("100.00"), datetime(2026, 8, 24), "OP-PRIMERO", cuenta_receptora="empresa.mp")

    monkeypatch.setattr(telegram, "send_telegram_message", fake_send)
    monkeypatch.setattr(telegram, "_download_telegram_file", fake_download)
    monkeypatch.setattr(telegram, "extract_transfer", fake_extract)
    monkeypatch.setattr(telegram, "save_comprobante_archivo", lambda **kwargs: 1)

    photo_message = {"message": {"chat": {"id": 888}, "photo": [{"file_id": "abc", "file_size": 100}]}}
    client.post("/telegram/webhook", json=photo_message)  # primer comprobante: crea el borrador
    response = client.post("/telegram/webhook", json=photo_message)  # segundo, sin cerrar el primero

    assert response.status_code == 200
    assert len(extract_calls) == 1  # el segundo mensaje no debe llegar a extraer nada
    assert sent[-1] == (
        "888",
        "Monto: $100.00\nFecha: 2026-08-24\nBanco emisor: no detectado\nOperacion: OP-PRIMERO\n"
        "Factura/cuenta: pendiente\n\nResponde SI para confirmar o NO para descartar.",
    )

    with SessionLocal() as session:
        movimientos = session.scalars(select(Movement).where(Movement.numero_operacion == "OP-PRIMERO")).all()
        assert len(movimientos) == 1


def test_photo_message_with_unreadable_receipt_asks_to_resend(monkeypatch):
    _register_operator("333")

    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        sent.append((chat_id, text))

    async def fake_download(file_id: str) -> tuple[bytes, str]:
        return b"fake-photo-bytes", "photos/file.jpg"

    monkeypatch.setattr(telegram, "send_telegram_message", fake_send)
    monkeypatch.setattr(telegram, "_download_telegram_file", fake_download)
    monkeypatch.setattr(telegram, "extract_transfer", lambda content_type, data, cuentas_validas=None: None)

    response = client.post(
        "/telegram/webhook",
        json={"message": {"chat": {"id": 333}, "photo": [{"file_id": "abc", "file_size": 100}]}},
    )

    assert response.status_code == 200
    assert sent == [("333", "No pude leer el comprobante. Reenvialo con mejor calidad o ingresa los datos manualmente.")]


def test_sharing_contact_links_operator_by_matching_local_phone_suffix(monkeypatch):
    _register_operator("3794579133")

    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        sent.append((chat_id, text))

    monkeypatch.setattr(telegram, "send_telegram_message", fake_send)

    response = client.post(
        "/telegram/webhook",
        json={
            "message": {
                "chat": {"id": 444},
                "contact": {"phone_number": "5493794579133", "user_id": 444},
            }
        },
    )

    assert response.status_code == 200
    assert sent == [("444", telegram.NUMERO_VINCULADO_TEXTO)]

    with SessionLocal() as session:
        operador = session.scalar(select(Operator).where(Operator.whatsapp_numero == "3794579133"))
        assert operador.telegram_chat_id == "444"


def test_sharing_contact_matches_regardless_of_mobile_nine_or_country_code(monkeypatch):
    _register_operator("+54 9 3794800628")

    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        sent.append((chat_id, text))

    monkeypatch.setattr(telegram, "send_telegram_message", fake_send)

    response = client.post(
        "/telegram/webhook",
        json={
            "message": {
                "chat": {"id": 777},
                "contact": {"phone_number": "+54 3794 80 0628", "user_id": 777},
            }
        },
    )

    assert response.status_code == 200
    assert sent == [("777", telegram.NUMERO_VINCULADO_TEXTO)]


def test_sharing_contact_with_unmatched_phone_is_rejected(monkeypatch):
    sent: list[tuple[str, str, dict | None]] = []

    async def fake_send(chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        sent.append((chat_id, text, reply_markup))

    monkeypatch.setattr(telegram, "send_telegram_message", fake_send)

    response = client.post(
        "/telegram/webhook",
        json={
            "message": {
                "chat": {"id": 555},
                "contact": {"phone_number": "5491111111111", "user_id": 555},
            }
        },
    )

    assert response.status_code == 200
    assert sent == [("555", telegram.NUMERO_NO_HABILITADO_TEXTO, telegram.CONTACT_REQUEST_MARKUP)]


def test_already_linked_chat_skips_phone_request(monkeypatch):
    _register_operator("3794579150", telegram_chat_id="666")

    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        sent.append((chat_id, text))

    monkeypatch.setattr(telegram, "send_telegram_message", fake_send)

    response = client.post("/telegram/webhook", json={"message": {"chat": {"id": 666}, "text": "hola"}})

    assert response.status_code == 200
    assert sent == [("666", "Envia una imagen o PDF del comprobante de transferencia.")]
