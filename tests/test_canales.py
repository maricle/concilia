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


def test_turno_de_serializa_los_pedidos_de_un_mismo_operador():
    # WhatsApp, Telegram y la web usan el mismo turno (clave whatsapp_numero):
    # dos pedidos del mismo operador no se intercalan; los de otro, si.
    eventos: list[str] = []

    async def pedido(numero: str, nombre: str) -> None:
        async with canales.turno_de(numero):
            eventos.append(f"{nombre}-inicio")
            await asyncio.sleep(0.01)
            eventos.append(f"{nombre}-fin")

    async def correr() -> None:
        await asyncio.gather(pedido("111", "a"), pedido("111", "b"), pedido("222", "c"))

    asyncio.run(correr())

    assert canales.turno_de("111") is canales.turno_de("111")
    posicion = {evento: i for i, evento in enumerate(eventos)}
    assert posicion["a-fin"] < posicion["b-inicio"]
    assert posicion["c-inicio"] < posicion["a-fin"]  # otro operador no espera
