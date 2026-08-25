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
            "monto": {"type": ["number", "null"], "description": "Monto de la transferencia"},
            "fecha_transaccion": {
                "type": ["string", "null"],
                "description": "Fecha de la operacion, formato YYYY-MM-DD",
            },
            "numero_operacion": {"type": ["string", "null"], "description": "Numero de operacion o referencia"},
            "banco_emisor": {"type": ["string", "null"], "description": "Banco desde el que se hizo la transferencia"},
            "cuenta_receptora": {
                "type": ["string", "null"],
                "description": "Identificador de la cuenta receptora si figura: CBU, CVU o alias (cualquiera de los tres que aparezca en el comprobante)",
            },
            "titular": {"type": ["string", "null"], "description": "Titular de la cuenta si figura"},
        },
        "required": [],
    },
}

SYSTEM_PROMPT = (
    "Sos un asistente que lee comprobantes de transferencias bancarias en espanol y extrae sus datos "
    "estructurados usando la herramienta provista. Si no podes leer con confianza el monto, la fecha o "
    "el numero de operacion, dejá ese campo directamente en null: nunca inventes un valor ni uses un "
    "texto de relleno como 'desconocido', 'N/A' o '<UNKNOWN>'."
)

_VALORES_PLACEHOLDER = {"unknown", "n/a", "na", "desconocido", "no disponible", "none", "null", "-", "s/d"}


def _es_valor_valido(valor: object) -> bool:
    if not isinstance(valor, str):
        return False
    normalizado = valor.strip().strip("<>").strip().lower()
    return bool(normalizado) and normalizado not in _VALORES_PLACEHOLDER


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
    """Fecha y numero de operacion son indispensables para identificar el movimiento
    y evitar duplicados; el monto es deseable pero si no se puede leer con confianza
    se registra igual (el administrador puede completarlo despues desde el panel)."""
    if result is None:
        return False
    return _es_valor_valido(result.get("fecha_transaccion")) and _es_valor_valido(result.get("numero_operacion"))


def _parse_monto(valor: object) -> Decimal | None:
    if valor is None:
        return None
    try:
        return Decimal(str(valor))
    except InvalidOperation:
        return None


def extract_transfer(content_type: str, data: bytes) -> ExtractedTransfer | None:
    """Interpreta un comprobante con Claude. Devuelve None si no se pudo leer con confianza."""
    client = Anthropic(api_key=get_settings().anthropic_api_key)

    result = _call_model(client, HAIKU_MODEL, content_type, data)
    if not _has_minimum_fields(result):
        result = _call_model(client, SONNET_MODEL, content_type, data)
    if not _has_minimum_fields(result):
        return None

    try:
        fecha_transaccion = datetime.strptime(result["fecha_transaccion"], "%Y-%m-%d")
    except (ValueError, KeyError):
        return None

    return ExtractedTransfer(
        monto=_parse_monto(result.get("monto")),
        fecha_transaccion=fecha_transaccion,
        numero_operacion=str(result["numero_operacion"]),
        banco_emisor=result.get("banco_emisor"),
        cuenta_receptora=result.get("cuenta_receptora"),
        titular=result.get("titular"),
    )
