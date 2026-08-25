import base64
from datetime import datetime
from decimal import Decimal, InvalidOperation

from anthropic import Anthropic

from .config import get_settings
from .conversation import ExtractedTransfer

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-5"

_TOOL = {
    "name": "registrar_transferencia",
    "description": "Estructura los datos de una transferencia bancaria a partir de un comprobante.",
    "input_schema": {
        "type": "object",
        "properties": {
            "monto": {"type": "number", "description": "Monto de la transferencia"},
            "fecha_transaccion": {"type": "string", "description": "Fecha de la operacion, formato YYYY-MM-DD"},
            "numero_operacion": {"type": "string", "description": "Numero de operacion o referencia"},
            "banco_emisor": {"type": ["string", "null"], "description": "Banco desde el que se hizo la transferencia"},
            "cuenta_receptora": {"type": ["string", "null"], "description": "CVU/alias/cuenta receptora si figura"},
            "titular": {"type": ["string", "null"], "description": "Titular de la cuenta si figura"},
        },
        "required": ["monto", "fecha_transaccion", "numero_operacion"],
    },
}

SYSTEM_PROMPT = (
    "Sos un asistente que lee comprobantes de transferencias bancarias en espanol y extrae sus datos "
    "estructurados usando la herramienta provista. Si no podes leer con confianza el monto, la fecha o "
    "el numero de operacion, dejá ese campo vacio en vez de inventar un valor."
)


def _content_block(content_type: str, data: bytes) -> dict:
    encoded = base64.b64encode(data).decode()
    if content_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": content_type, "data": encoded}}
    return {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": encoded}}


def _call_model(client: Anthropic, model: str, content_type: str, data: bytes) -> dict | None:
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": _TOOL["name"]},
        messages=[
            {
                "role": "user",
                "content": [
                    _content_block(content_type, data),
                    {"type": "text", "text": "Extrae los datos de este comprobante de transferencia."},
                ],
            }
        ],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return None


def _has_minimum_fields(result: dict | None) -> bool:
    if result is None:
        return False
    return bool(result.get("monto") and result.get("fecha_transaccion") and result.get("numero_operacion"))


def extract_transfer(content_type: str, data: bytes) -> ExtractedTransfer | None:
    """Interpreta un comprobante con Claude. Devuelve None si no se pudo leer con confianza."""
    client = Anthropic(api_key=get_settings().anthropic_api_key)

    result = _call_model(client, HAIKU_MODEL, content_type, data)
    if not _has_minimum_fields(result):
        result = _call_model(client, SONNET_MODEL, content_type, data)
    if not _has_minimum_fields(result):
        return None

    try:
        monto = Decimal(str(result["monto"]))
        fecha_transaccion = datetime.strptime(result["fecha_transaccion"], "%Y-%m-%d")
    except (InvalidOperation, ValueError, KeyError):
        return None

    return ExtractedTransfer(
        monto=monto,
        fecha_transaccion=fecha_transaccion,
        numero_operacion=str(result["numero_operacion"]),
        banco_emisor=result.get("banco_emisor"),
        cuenta_receptora=result.get("cuenta_receptora"),
        titular=result.get("titular"),
    )
