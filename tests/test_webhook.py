from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import webhook
from app.config import Settings
from app.conversation import ConversationService, ExtractedTransfer
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import BankAccount, Movement, Movil, Operator, Reparto, RepartoOperador, WhatsAppConversation

client = TestClient(app)


def setup_function():
    Base.metadata.create_all(engine)


def _clean_movement(numero_operacion: str) -> None:
    with SessionLocal() as session:
        for movimiento in session.scalars(select(Movement).where(Movement.numero_operacion == numero_operacion)).all():
            session.delete(movimiento)
        session.commit()


def _clean_conversation(numero: str) -> None:
    with SessionLocal() as session:
        conversation = session.get(WhatsAppConversation, numero)
        if conversation is not None:
            session.delete(conversation)
            session.commit()


def _clean_operator(whatsapp_numero: str) -> None:
    with SessionLocal() as session:
        for operador in session.scalars(select(Operator).where(Operator.whatsapp_numero == whatsapp_numero)).all():
            session.delete(operador)
        session.commit()


def _clean_movil(numero: str) -> None:
    with SessionLocal() as session:
        movil = session.scalar(select(Movil).where(Movil.numero == numero))
        if movil is not None:
            session.query(RepartoOperador).where(
                RepartoOperador.reparto_id.in_(select(Reparto.id).where(Reparto.movil_id == movil.id))
            ).delete(synchronize_session=False)
            session.query(Reparto).where(Reparto.movil_id == movil.id).delete(synchronize_session=False)
            session.delete(movil)
            session.commit()


def _register_operator(whatsapp_numero: str) -> None:
    _clean_operator(whatsapp_numero)
    _clean_conversation(whatsapp_numero)
    with SessionLocal() as session:
        session.add(Operator(nombre="Test", whatsapp_numero=whatsapp_numero))
        session.commit()


def _mensaje_texto(numero_from: str, texto: str) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"from": numero_from, "type": "text", "text": {"body": texto}}
                            ]
                        }
                    }
                ]
            }
        ]
    }


def _mensaje_imagen(numero_from: str, media_id: str = "media-abc", caption: str | None = None) -> dict:
    image = {"id": media_id, "mime_type": "image/jpeg"}
    if caption is not None:
        image["caption"] = caption
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"from": numero_from, "type": "image", "image": image}
                            ]
                        }
                    }
                ]
            }
        ]
    }


def test_texto_de_numero_no_registrado_es_rechazado(monkeypatch):
    monkeypatch.setattr(webhook, "_avisos_no_habilitado", {})
    sent: list[tuple[str, str, list | None]] = []

    async def fake_send(to: str, text: str, botones=None) -> None:
        sent.append((to, text, botones))

    monkeypatch.setattr(webhook, "send_whatsapp_message", fake_send)

    response = client.post("/webhook", json=_mensaje_texto("5493794500000", "hola"))

    assert response.status_code == 200
    assert sent == [("5493794500000", webhook.NUMERO_NO_HABILITADO_TEXTO, None)]


def test_webhook_signature_mismatch_is_rejected(monkeypatch):
    monkeypatch.setattr(webhook, "get_settings", lambda: Settings(database_url="sqlite:///:memory:", meta_app_secret="expected"))

    response = client.post(
        "/webhook",
        json=_mensaje_texto("111", "hola"),
        headers={"x-hub-signature-256": "sha256=deadbeef"},
    )

    assert response.status_code == 403


def test_numero_se_matchea_por_ultimos_digitos_con_codigo_de_pais(monkeypatch):
    """El operador se carga sin codigo de pais (ej. "3794579133", ver README),
    pero WhatsApp manda el "from" con codigo de pais + 9 de movil -- tiene que
    matchear igual, sin ningun paso previo de vincular numero (a diferencia de
    Telegram, donde el chat_id no es el telefono real)."""
    _register_operator("3794579133")

    sent: list[tuple[str, str, list | None]] = []

    async def fake_send(to: str, text: str, botones=None) -> None:
        sent.append((to, text, botones))

    monkeypatch.setattr(webhook, "send_whatsapp_message", fake_send)

    response = client.post("/webhook", json=_mensaje_texto("5493794579133", "hola"))

    assert response.status_code == 200
    # Se identifico al operador (no el mensaje de "numero no habilitado"), y la
    # respuesta se manda a la clave canonica (whatsapp_numero tal cual esta
    # cargado), no al numero crudo que vino en el "from".
    assert sent[0][0] == "3794579133"
    assert sent[0][1] != webhook.NUMERO_NO_HABILITADO_TEXTO


def test_imagen_se_extrae_y_guarda(monkeypatch):
    _register_operator("3794500222")
    _clean_movement("OP-999")
    with SessionLocal() as session:
        if session.scalar(select(BankAccount).where(BankAccount.alias == "empresa.mp")) is None:
            session.add(BankAccount(banco="Mercado Pago", numero_cuenta="1", alias="empresa.mp"))
            session.commit()

    sent: list[tuple[str, str, list | None]] = []

    async def fake_send(to: str, text: str, botones=None) -> None:
        sent.append((to, text, botones))

    async def fake_download(media_id: str) -> tuple[bytes, str]:
        return b"fake-photo-bytes", "image/jpeg"

    def fake_extract(content_type: str, data: bytes) -> ExtractedTransfer:
        return ExtractedTransfer(Decimal("100.00"), datetime(2026, 8, 24), "OP-999", cuenta_receptora="empresa.mp")

    saved: list[bytes] = []

    def fake_save(nombre_archivo: str, content_type: str, contenido: bytes) -> int:
        saved.append(contenido)
        return 1

    monkeypatch.setattr(webhook, "send_whatsapp_message", fake_send)
    monkeypatch.setattr(webhook, "_download_whatsapp_media", fake_download)
    monkeypatch.setattr(webhook, "extract_transfer", fake_extract)
    monkeypatch.setattr(webhook, "save_comprobante_archivo", fake_save)

    response = client.post("/webhook", json=_mensaje_imagen("3794500222"))

    assert response.status_code == 200
    assert saved == [b"fake-photo-bytes"]
    assert sent == [
        (
            "3794500222",
            "Monto: $100.00\nFecha: 2026-08-24\nCuenta receptora: empresa.mp\nOperacion: OP-999\n"
            "Factura/cuenta: pendiente\n\nSi los datos estan bien, elegi si vas a cargar un numero de factura o un numero de cuenta del cliente. Si no, descartalo."
            "\nTip: la proxima vez manda el comprobante con el texto \"F 12345\" o \"C 12345\" y te ahorras este paso."
            "\n\nIndica el tipo y numero de comprobante (ej: F 12345 o C 987).",
            webhook.TIPO_FACTURA_CUENTA_BOTONES,
        )
    ]


def test_documento_pdf_se_extrae_igual_que_una_imagen(monkeypatch):
    _register_operator("3794500223")
    _clean_movement("OP-PDF")
    with SessionLocal() as session:
        if session.scalar(select(BankAccount).where(BankAccount.alias == "empresa.mp")) is None:
            session.add(BankAccount(banco="Mercado Pago", numero_cuenta="1", alias="empresa.mp"))
            session.commit()

    sent: list[tuple[str, str, list | None]] = []

    async def fake_send(to: str, text: str, botones=None) -> None:
        sent.append((to, text, botones))

    async def fake_download(media_id: str) -> tuple[bytes, str]:
        return b"fake-pdf-bytes", "application/pdf"

    def fake_extract(content_type: str, data: bytes) -> ExtractedTransfer:
        assert content_type == "application/pdf"
        return ExtractedTransfer(Decimal("50.00"), datetime(2026, 8, 24), "OP-PDF", cuenta_receptora="empresa.mp")

    monkeypatch.setattr(webhook, "send_whatsapp_message", fake_send)
    monkeypatch.setattr(webhook, "_download_whatsapp_media", fake_download)
    monkeypatch.setattr(webhook, "extract_transfer", fake_extract)
    monkeypatch.setattr(webhook, "save_comprobante_archivo", lambda **kwargs: 1)

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "3794500223",
                                    "type": "document",
                                    "document": {"id": "doc-1", "mime_type": "application/pdf", "filename": "comp.pdf"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    response = client.post("/webhook", json=payload)

    assert response.status_code == 200
    assert "OP-PDF" in sent[0][1]


def test_imagen_no_legible_pide_reenviar(monkeypatch):
    _register_operator("3794500333")

    sent: list[tuple[str, str, list | None]] = []

    async def fake_send(to: str, text: str, botones=None) -> None:
        sent.append((to, text, botones))

    async def fake_download(media_id: str) -> tuple[bytes, str]:
        return b"fake-photo-bytes", "image/jpeg"

    monkeypatch.setattr(webhook, "send_whatsapp_message", fake_send)
    monkeypatch.setattr(webhook, "_download_whatsapp_media", fake_download)
    monkeypatch.setattr(webhook, "extract_transfer", lambda content_type, data: None)

    response = client.post("/webhook", json=_mensaje_imagen("3794500333"))

    assert response.status_code == 200
    assert sent == [("3794500333", "No pude leer el comprobante. Reenvialo con mejor calidad o ingresa los datos manualmente.", None)]


def test_segunda_imagen_mientras_hay_borrador_pendiente_no_huerfana_la_primera(monkeypatch):
    _register_operator("3794500888")
    _clean_movement("OP-PRIMERO")
    _clean_movement("OP-SEGUNDO")
    with SessionLocal() as session:
        if session.scalar(select(BankAccount).where(BankAccount.alias == "empresa.mp")) is None:
            session.add(BankAccount(banco="Mercado Pago", numero_cuenta="1", alias="empresa.mp"))
            session.commit()

    sent: list[tuple[str, str, list | None]] = []

    async def fake_send(to: str, text: str, botones=None) -> None:
        sent.append((to, text, botones))

    async def fake_download(media_id: str) -> tuple[bytes, str]:
        return b"fake-photo-bytes", "image/jpeg"

    extract_calls = []

    def fake_extract(content_type: str, data: bytes) -> ExtractedTransfer:
        extract_calls.append(1)
        return ExtractedTransfer(Decimal("100.00"), datetime(2026, 8, 24), "OP-PRIMERO", cuenta_receptora="empresa.mp")

    monkeypatch.setattr(webhook, "send_whatsapp_message", fake_send)
    monkeypatch.setattr(webhook, "_download_whatsapp_media", fake_download)
    monkeypatch.setattr(webhook, "extract_transfer", fake_extract)
    monkeypatch.setattr(webhook, "save_comprobante_archivo", lambda **kwargs: 1)

    payload = _mensaje_imagen("3794500888")
    client.post("/webhook", json=payload)  # primer comprobante: crea el borrador
    response = client.post("/webhook", json=payload)  # segundo, sin cerrar el primero

    assert response.status_code == 200
    assert len(extract_calls) == 1  # el segundo mensaje no debe llegar a extraer nada
    assert sent[-1] == (
        "3794500888",
        "Tenes movimientos pendientes. Indica el tipo y numero de comprobante (ej: F 12345) o descartalo.",
        webhook.TIPO_FACTURA_CUENTA_BOTONES,
    )

    with SessionLocal() as session:
        movimientos = session.scalars(select(Movement).where(Movement.numero_operacion == "OP-PRIMERO")).all()
        assert len(movimientos) == 1


def test_boton_interactive_responde_igual_que_texto_libre(monkeypatch):
    """El "button_reply.id" que manda WhatsApp al tocar un boton tiene que
    interpretarse exactamente igual que si el operador hubiera tipeado esa
    misma palabra (mismo criterio que el callback_data de Telegram)."""
    _register_operator("3794500999")
    _clean_movement("OP-BOTON")
    with SessionLocal() as session:
        if session.scalar(select(BankAccount).where(BankAccount.alias == "empresa.mp")) is None:
            session.add(BankAccount(banco="Mercado Pago", numero_cuenta="1", alias="empresa.mp"))
            session.commit()
        ConversationService(session).start_transfer(
            "3794500999",
            ExtractedTransfer(Decimal("100.00"), datetime(2026, 8, 24), "OP-BOTON", cuenta_receptora="empresa.mp"),
        )

    sent: list[tuple[str, str, list | None]] = []

    async def fake_send(to: str, text: str, botones=None) -> None:
        sent.append((to, text, botones))

    monkeypatch.setattr(webhook, "send_whatsapp_message", fake_send)

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "3794500999",
                                    "type": "interactive",
                                    "interactive": {"type": "button_reply", "button_reply": {"id": "factura", "title": "Factura"}},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    response = client.post("/webhook", json=payload)

    assert response.status_code == 200
    assert sent == [("3794500999", "Indica el numero de factura del cliente asociado a este pago.", webhook.DESCARTAR_BOTONES)]

    _clean_movement("OP-BOTON")


def test_cerrar_reparto_notifica_al_otro_operador_asociado_directo_por_whatsapp(monkeypatch):
    """A diferencia de Telegram (que necesita resolver whatsapp_numero -> chat_id),
    las notificaciones de WhatsApp se mandan directo: pop_notificaciones() ya
    devuelve el whatsapp_numero, que es la misma direccion de mensajeria."""
    _register_operator("3794577001")
    _register_operator("3794577002")
    _clean_movil("M-TEST-CIERRE-WA")

    with SessionLocal() as session:
        session.add(Movil(numero="M-TEST-CIERRE-WA", nombre="Camion Test", responsable_operador_id=None))
        session.commit()
        service = ConversationService(session)
        service.handle_text("3794577001", "inicio movil M-TEST-CIERRE-WA reparto nro 42")
        service.handle_text("3794577001", "SI")
        service.handle_text("3794577002", "inicio movil M-TEST-CIERRE-WA reparto nro 42")

    sent: list[tuple[str, str, list | None]] = []

    async def fake_send(to: str, text: str, botones=None) -> None:
        sent.append((to, text, botones))

    monkeypatch.setattr(webhook, "send_whatsapp_message", fake_send)

    response = client.post("/webhook", json=_mensaje_texto("3794577002", "cerrar"))

    assert response.status_code == 200
    assert ("3794577002", "Salida Nº 42 cerrada.\n\nCierre Salida Nº 42\nSin comprobantes registrados.", None) in sent
    assert (
        "3794577001",
        "La Salida Nº 42 en el movil M-TEST-CIERRE-WA fue cerrada por Test.\n\nCierre Salida Nº 42\nSin comprobantes registrados.",
        None,
    ) in sent

    _clean_movil("M-TEST-CIERRE-WA")
    _clean_operator("3794577001")
    _clean_operator("3794577002")


def test_numero_no_registrado_recibe_el_aviso_una_sola_vez(monkeypatch):
    # Cada respuesta del bot se cobra: a un numero desconocido se le avisa una
    # vez, no en cada mensaje que mande.
    monkeypatch.setattr(webhook, "_avisos_no_habilitado", {})
    sent: list[tuple[str, str, list | None]] = []

    async def fake_send(to: str, text: str, botones=None) -> None:
        sent.append((to, text, botones))

    monkeypatch.setattr(webhook, "send_whatsapp_message", fake_send)

    client.post("/webhook", json=_mensaje_texto("5493794500001", "hola"))
    client.post("/webhook", json=_mensaje_texto("5493794500001", "hola?"))
    client.post("/webhook", json=_mensaje_texto("5493794500001", "alguien?"))

    assert sent == [("5493794500001", webhook.NUMERO_NO_HABILITADO_TEXTO, None)]


def test_imagen_con_caption_de_factura_pide_solo_confirmar(monkeypatch):
    _register_operator("3794500333")
    _clean_movement("OP-CAPTION")
    with SessionLocal() as session:
        if session.scalar(select(BankAccount).where(BankAccount.alias == "empresa.mp")) is None:
            session.add(BankAccount(banco="Mercado Pago", numero_cuenta="1", alias="empresa.mp"))
            session.commit()

    sent: list[tuple[str, str, list | None]] = []

    async def fake_send(to: str, text: str, botones=None) -> None:
        sent.append((to, text, botones))

    async def fake_download(media_id: str) -> tuple[bytes, str]:
        return b"fake-photo-bytes", "image/jpeg"

    def fake_extract(content_type: str, data: bytes) -> ExtractedTransfer:
        return ExtractedTransfer(Decimal("100.00"), datetime(2026, 8, 24), "OP-CAPTION", cuenta_receptora="empresa.mp")

    monkeypatch.setattr(webhook, "send_whatsapp_message", fake_send)
    monkeypatch.setattr(webhook, "_download_whatsapp_media", fake_download)
    monkeypatch.setattr(webhook, "extract_transfer", fake_extract)
    monkeypatch.setattr(webhook, "save_comprobante_archivo", lambda **kwargs: 1)

    client.post("/webhook", json=_mensaje_imagen("3794500333", caption="F 4455"))

    assert len(sent) == 1
    numero, texto, botones = sent[0]
    assert "Factura/cuenta: Factura: 4455" in texto
    assert "¿Registro el comprobante?" in texto
    assert botones == webhook.SI_NO_BOTONES
