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
    "estructurados usando la herramienta provista. El monto es el dato mas importante: si no lo podes "
    "leer con confianza, dejalo en null en vez de inventar un valor. Para el resto de los campos, si no "
    "estas seguro tambien dejalos en null en vez de usar un texto de relleno como 'desconocido', 'N/A' "
    "o '<UNKNOWN>'."
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


def _parse_monto(valor: object) -> Decimal | None:
    if valor is None:
        return None
    try:
        return Decimal(str(valor))
    except InvalidOperation:
        return None


def _has_minimum_fields(result: dict | None) -> bool:
    """El monto es el unico dato indispensable para registrar el comprobante: sin el
    no hay nada que conciliar despues. La fecha, si no se puede leer, se completa con
    la fecha de hoy; el numero de operacion puede quedar vacio."""
    if result is None:
        return False
    return _parse_monto(result.get("monto")) is not None


def _parse_fecha_o_actual(valor: object) -> datetime:
    if _es_valor_valido(valor):
        try:
            return datetime.strptime(valor, "%Y-%m-%d")
        except ValueError:
            pass
    return datetime.utcnow()


def extract_transfer(content_type: str, data: bytes) -> ExtractedTransfer | None:
    """Interpreta un comprobante con Claude. Devuelve None si no se pudo leer el monto
    con confianza (el unico dato que bloquea el registro)."""
    client = Anthropic(api_key=get_settings().anthropic_api_key)

    result = _call_model(client, HAIKU_MODEL, content_type, data)
    if not _has_minimum_fields(result):
        result = _call_model(client, SONNET_MODEL, content_type, data)
    if not _has_minimum_fields(result):
        return None

    numero_operacion = result.get("numero_operacion")

    return ExtractedTransfer(
        monto=_parse_monto(result.get("monto")),
        fecha_transaccion=_parse_fecha_o_actual(result.get("fecha_transaccion")),
        numero_operacion=numero_operacion if _es_valor_valido(numero_operacion) else None,
        banco_emisor=result.get("banco_emisor"),
        cuenta_receptora=result.get("cuenta_receptora"),
        titular=result.get("titular"),
    )
