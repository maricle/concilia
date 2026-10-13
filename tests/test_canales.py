import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import canales, telegram, webhook
from app.db import Base
from app.models import Operator


def _sesion() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_ultimos_digitos_ignora_formato_y_prefijos():
    assert canales.ultimos_digitos("+54 9 379 450-0000") == "3794500000"
    assert canales.ultimos_digitos("3794500000") == "3794500000"
    assert canales.ultimos_digitos("12345") is None


def test_buscar_operador_por_telefono_compara_los_ultimos_10_digitos():
    db = _sesion()
    db.add_all(
        [
            Operator(nombre="Ana", whatsapp_numero="+54 9 379 450-0000"),
            Operator(nombre="Inactivo", whatsapp_numero="3794511111", activo=False),
            Operator(nombre="Vinculada", whatsapp_numero="3794522222", telegram_chat_id="99"),
        ]
    )
    db.commit()

    assert canales.buscar_operador_por_telefono(db, "5493794500000").nombre == "Ana"
    assert canales.buscar_operador_por_telefono(db, "3794511111") is None
    assert canales.buscar_operador_por_telefono(db, "3794522222").nombre == "Vinculada"
    assert canales.buscar_operador_por_telefono(db, "3794522222", solo_sin_telegram=True) is None


def test_cada_canal_dibuja_todos_los_teclados():
    for teclado in canales.Teclado:
        assert teclado in webhook.BOTONES_POR_TECLADO
        assert teclado in telegram.MARKUP_POR_TECLADO
    # WhatsApp admite hasta 3 botones por mensaje.
    assert all(len(botones) <= 3 for botones in webhook.BOTONES_POR_TECLADO.values())


def test_procesar_comprobante_ilegible_no_guarda_nada(monkeypatch):
    guardados: list[bytes] = []
    monkeypatch.setattr(canales, "extract_transfer", lambda content_type, data: None)
    monkeypatch.setattr(canales, "save_comprobante_archivo", lambda **kwargs: guardados.append(kwargs["contenido"]))

    async def descargar() -> tuple[bytes, str]:
        return b"foto", "image/jpeg"

    respuesta = asyncio.run(canales.procesar_comprobante("111", descargar, nombre_archivo="f.jpg", caption=None))

    assert respuesta == canales.Respuesta(canales.NO_PUDE_LEER_TEXTO)
    assert guardados == []
