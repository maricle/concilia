import base64
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from anthropic import Anthropic

from .config import get_settings
from .conversation import ExtractedTransfer

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-5"

_FEW_SHOT_DIR = Path(__file__).parent / "few_shot"

# Ejemplos fijos que se muestran antes de cada extraccion real, para corregir errores
# ya observados en produccion. Las imagenes son comprobantes SINTETICOS (datos
# inventados) que reproducen la estructura problematica, no comprobantes reales de
# clientes -- no hay que agregar aca ningun archivo con datos personales de verdad.
_FEW_SHOT_EXAMPLES = [
    {
        "archivo": "mercadopago_sin_numero_operacion.jpg",
        "media_type": "image/jpeg",
        "salida_esperada": {
            "monto": 8750,
            "fecha_transaccion": "2026-03-12",
            "numero_operacion": None,
            "banco_emisor": "Mercado Pago",
            "cuenta_receptora": None,
            "titular": "Empresa Ejemplo Sociedad Anonima",
        },
    },
]

def _build_tool(cuentas_validas: list[tuple[str, str]]) -> dict:
    """Arma el schema de la herramienta de extraccion. Si se pasan cuentas_validas
    (alias, numero_cuenta) de las cuentas bancarias de la empresa, cuenta_receptora
    queda restringido a elegir uno de esos alias exactos (o null) en vez de
    transcribir libremente un CBU/CVU: es mucho mas confiable que Claude compare
    contra una lista corta a que transcriba 22 digitos sin error y que despues
    nosotros lo matcheemos, y evita que confunda la cuenta de origen con la de
    destino."""
    if cuentas_validas:
        listado = "; ".join(f"'{alias}' (numero de cuenta {numero})" for alias, numero in cuentas_validas)
        cuenta_receptora_schema = {
            "type": ["string", "null"],
            "enum": [alias for alias, _ in cuentas_validas] + [None],
            "description": (
                "Cual de las cuentas bancarias de la empresa RECIBE el dinero en este comprobante (la seccion "
                "'Para'/'Destino' -- nunca la de quien envia, 'De'/'Origen'). Las cuentas posibles son: "
                f"{listado}. Devolve exactamente el alias tal como esta escrito si el comprobante corresponde "
                "a una de esas cuentas, o null si no corresponde a ninguna."
            ),
        }
    else:
        cuenta_receptora_schema = {
            "type": ["string", "null"],
            "description": (
                "Identificador de la cuenta que RECIBE el dinero (el destinatario, la seccion 'Para' o "
                "'Destino' del comprobante) -- nunca la cuenta de quien envia ('De'/'Origen'). Puede ser "
                "CBU, CVU o alias, el que figure."
            ),
        }
    return {
        "name": "registrar_transferencia",
        "description": "Estructura los datos de una transferencia bancaria a partir de un comprobante.",
        "input_schema": {
            "type": "object",
            "properties": {
                "monto": {"type": ["number", "null"], "description": "Monto de la transferencia"},
                "fecha_transaccion": {
                    "type": ["string", "null"],
                    "description": (
                        "Fecha de la operacion. Si el comprobante tambien muestra la hora, devolvela en formato "
                        "'YYYY-MM-DD HH:MM' (24 horas); si solo hay fecha, devolve 'YYYY-MM-DD'."
                    ),
                },
                "numero_operacion": {
                    "type": ["string", "null"],
                    "description": (
                        "Numero de operacion, transaccion o comprobante (el identificador de ESTA transferencia "
                        "puntual, tipicamente etiquetado 'Numero de operacion', 'ID de transaccion' o similar). "
                        "Nunca un CBU, CVU, alias o numero de cuenta -- esos van en cuenta_receptora, no aca. Si "
                        "no ves un numero de operacion claramente identificado como tal, dejalo en null: no "
                        "expliques por que falta, no describas el comprobante, simplemente null."
                    ),
                },
                "banco_emisor": {
                    "type": ["string", "null"],
                    "description": "Banco desde el que se hizo la transferencia",
                },
                "cuenta_receptora": cuenta_receptora_schema,
                "titular": {
                    "type": ["string", "null"],
                    "description": (
                        "Nombre del titular de la cuenta de ORIGEN (quien envia el dinero -- la seccion "
                        "'De'/'Origen'/'Cuenta debito' del comprobante), si figura. Nunca el nombre de la "
                        "empresa que recibe el pago: la cuenta receptora siempre es una de las cuentas de "
                        "la empresa (ver cuenta_receptora), asi que su titular nunca va aca."
                    ),
                },
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


def _es_numero_operacion_valido(valor: object) -> bool:
    """Ademas del chequeo de placeholders general, un numero de operacion real nunca
    es una oracion (Claude a veces explica en texto por que no lo encuentra en vez de
    devolver null) ni una cadena de puros digitos tan larga como un CBU/CVU (Claude a
    veces confunde la cuenta con el numero de operacion)."""
    if not _es_valor_valido(valor):
        return False
    normalizado = valor.strip()
    if len(normalizado.split()) >= 3:
        return False
    if normalizado.isdigit() and len(normalizado) >= 20:
        return False
    return True


def _content_block(content_type: str, data: bytes) -> dict:
    encoded = base64.b64encode(data).decode()
    if content_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": content_type, "data": encoded}}
    return {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": encoded}}


@lru_cache
def _leer_ejemplo(archivo: str) -> bytes:
    return (_FEW_SHOT_DIR / archivo).read_bytes()


def _few_shot_messages(tool_name: str) -> list[dict]:
    """Turnos previos (usuario con la imagen, asistente con la extraccion correcta)
    que se anteponen a la consulta real, para fijar por ejemplo concreto un patron
    que ya fallo antes en vez de depender solo de la descripcion en texto."""
    mensajes: list[dict] = []
    for indice, ejemplo in enumerate(_FEW_SHOT_EXAMPLES):
        contenido = _leer_ejemplo(ejemplo["archivo"])
        tool_use_id = f"toolu_ejemplo_{indice}"
        mensajes.append(
            {
                "role": "user",
                "content": [
                    _content_block(ejemplo["media_type"], contenido),
                    {"type": "text", "text": "Extrae los datos de este comprobante de transferencia."},
                ],
            }
        )
        mensajes.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": ejemplo["salida_esperada"]}
                ],
            }
        )
        mensajes.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "Datos registrados."}],
            }
        )
    return mensajes


def _call_model(client: Anthropic, model: str, content_type: str, data: bytes, tool: dict) -> dict | None:
    messages = _few_shot_messages(tool["name"]) + [
        {
            "role": "user",
            "content": [
                _content_block(content_type, data),
                {"type": "text", "text": "Extrae los datos de este comprobante de transferencia."},
            ],
        }
    ]
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=messages,
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


_FORMATOS_FECHA_TRANSACCION = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d")


def _parse_fecha_o_actual(valor: object) -> datetime:
    if _es_valor_valido(valor):
        for formato in _FORMATOS_FECHA_TRANSACCION:
            try:
                return datetime.strptime(valor.strip(), formato)
            except ValueError:
                continue
    return datetime.utcnow()


def extract_transfer(
    content_type: str, data: bytes, cuentas_validas: list[tuple[str, str]] | None = None
) -> ExtractedTransfer | None:
    """Interpreta un comprobante con Claude. Devuelve None si no se pudo leer el monto
    con confianza (el unico dato que bloquea el registro). cuentas_validas es la lista
    de (alias, numero_cuenta) de las cuentas bancarias de la empresa, para que Claude
    elija cual de ellas recibio el pago en vez de transcribir un CBU/CVU a mano."""
    client = Anthropic(api_key=get_settings().anthropic_api_key)
    tool = _build_tool(cuentas_validas or [])

    result = _call_model(client, HAIKU_MODEL, content_type, data, tool)
    if not _has_minimum_fields(result):
        result = _call_model(client, SONNET_MODEL, content_type, data, tool)
    if not _has_minimum_fields(result):
        return None

    numero_operacion = result.get("numero_operacion")

    return ExtractedTransfer(
        monto=_parse_monto(result.get("monto")),
        fecha_transaccion=_parse_fecha_o_actual(result.get("fecha_transaccion")),
        numero_operacion=numero_operacion if _es_numero_operacion_valido(numero_operacion) else None,
        banco_emisor=result.get("banco_emisor"),
        cuenta_receptora=result.get("cuenta_receptora"),
        titular=result.get("titular"),
    )
