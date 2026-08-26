from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app import extraction
from app.extraction import _build_tool, _es_numero_operacion_valido, _es_valor_valido, _has_minimum_fields, extract_transfer


@pytest.mark.parametrize(
    "valor,esperado",
    [
        ("3D5W612EW7W7V01W2GXYVR", True),
        ("174085816489", True),
        ("OP-123", True),
        (None, False),
        ("<UNKNOWN>", False),
        ("Número de operación no visible en el comprobante", False),
        ("No figura en el comprobante", False),
        ("0000003100045397444527", False),  # 22 digitos: pinta a CBU/CVU, no a numero de operacion
        ("12345678901234567890", False),  # 20 digitos, mismo caso limite
        ("1234567890123456789", True),  # 19 digitos: por debajo del umbral, se acepta
    ],
)
def test_es_numero_operacion_valido(valor, esperado):
    assert _es_numero_operacion_valido(valor) is esperado


def test_build_tool_without_cuentas_leaves_cuenta_receptora_freeform():
    tool = _build_tool([])
    assert "enum" not in tool["input_schema"]["properties"]["cuenta_receptora"]


def test_build_tool_with_cuentas_constrains_cuenta_receptora_to_aliases():
    tool = _build_tool([("empresa.mp", "123"), ("empresa.galicia", "456")])
    schema = tool["input_schema"]["properties"]["cuenta_receptora"]
    assert schema["enum"] == ["empresa.mp", "empresa.galicia", None]


@pytest.mark.parametrize(
    "valor,esperado",
    [
        ("OP-123", True),
        ("2026-08-24", True),
        (None, False),
        ("", False),
        ("   ", False),
        ("<UNKNOWN>", False),
        ("unknown", False),
        ("Unknown", False),
        ("N/A", False),
        ("desconocido", False),
        ("s/d", False),
        (123, False),
    ],
)
def test_es_valor_valido(valor, esperado):
    assert _es_valor_valido(valor) is esperado


def test_has_minimum_fields_requires_monto():
    resultado = {"monto": None, "fecha_transaccion": "2026-08-24", "numero_operacion": "OP-123"}
    assert _has_minimum_fields(resultado) is False


def test_has_minimum_fields_accepts_monto_even_without_fecha_ni_numero_operacion():
    resultado = {"monto": 500, "fecha_transaccion": None, "numero_operacion": None}
    assert _has_minimum_fields(resultado) is True


def test_has_minimum_fields_rejects_unparseable_monto():
    resultado = {"monto": "no es un numero", "fecha_transaccion": "2026-08-24", "numero_operacion": "OP-123"}
    assert _has_minimum_fields(resultado) is False


def test_few_shot_example_image_exists_and_loads():
    assert len(extraction._FEW_SHOT_EXAMPLES) >= 1
    for ejemplo in extraction._FEW_SHOT_EXAMPLES:
        contenido = extraction._leer_ejemplo(ejemplo["archivo"])
        assert len(contenido) > 0


def test_few_shot_messages_precede_the_real_question():
    mensajes = extraction._few_shot_messages("registrar_transferencia")
    # 3 mensajes por ejemplo: usuario+imagen, asistente+tool_use, usuario+tool_result.
    assert len(mensajes) == len(extraction._FEW_SHOT_EXAMPLES) * 3
    assert mensajes[0]["role"] == "user"
    assert mensajes[1]["role"] == "assistant"
    assert mensajes[1]["content"][0]["type"] == "tool_use"
    assert mensajes[1]["content"][0]["input"]["numero_operacion"] is None
    assert mensajes[2]["content"][0]["type"] == "tool_result"


@dataclass
class _FakeBlock:
    type: str
    input: dict


@dataclass
class _FakeResponse:
    content: list


class _FakeMessages:
    def __init__(self, responses: list[dict | None]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs) -> _FakeResponse:
        self.calls.append(kwargs)
        resultado = self._responses.pop(0)
        if resultado is None:
            return _FakeResponse(content=[])
        return _FakeResponse(content=[_FakeBlock(type="tool_use", input=resultado)])


class _FakeAnthropic:
    def __init__(self, responses: list[dict | None]):
        self.messages = _FakeMessages(responses)


def test_extract_transfer_retries_with_sonnet_when_haiku_misses_monto(monkeypatch):
    sin_monto = {"monto": None, "fecha_transaccion": "2026-08-24", "numero_operacion": "OP-999"}
    con_monto = {"monto": 500, "fecha_transaccion": "2026-08-24", "numero_operacion": "OP-999"}
    fake_client = _FakeAnthropic([sin_monto, con_monto])
    monkeypatch.setattr(extraction, "Anthropic", lambda api_key: fake_client)

    transfer = extract_transfer("image/jpeg", b"fake-bytes")

    assert transfer is not None
    assert transfer.numero_operacion == "OP-999"
    assert transfer.monto == Decimal("500")
    assert transfer.fecha_transaccion == datetime(2026, 8, 24)


def test_extract_transfer_gives_up_when_both_models_miss_monto(monkeypatch):
    sin_monto = {"monto": None, "fecha_transaccion": "2026-08-24", "numero_operacion": "OP-1"}
    fake_client = _FakeAnthropic([sin_monto, sin_monto])
    monkeypatch.setattr(extraction, "Anthropic", lambda api_key: fake_client)

    assert extract_transfer("image/jpeg", b"fake-bytes") is None


def test_extract_transfer_registers_with_missing_fecha_and_numero_operacion(monkeypatch):
    resultado = {"monto": 500, "fecha_transaccion": None, "numero_operacion": None}
    fake_client = _FakeAnthropic([resultado])  # una sola respuesta: no debe reintentar, ya tiene monto
    monkeypatch.setattr(extraction, "Anthropic", lambda api_key: fake_client)

    antes = datetime.utcnow()
    transfer = extract_transfer("image/jpeg", b"fake-bytes")
    despues = datetime.utcnow()

    assert transfer is not None
    assert transfer.monto == Decimal("500")
    assert transfer.numero_operacion is None
    assert antes - timedelta(seconds=5) <= transfer.fecha_transaccion <= despues + timedelta(seconds=5)


def test_extract_transfer_passes_cuentas_validas_as_enum_to_the_model(monkeypatch):
    resultado = {"monto": 500, "fecha_transaccion": "2026-08-24", "numero_operacion": "OP-1", "cuenta_receptora": "empresa.mp"}
    fake_client = _FakeAnthropic([resultado])
    monkeypatch.setattr(extraction, "Anthropic", lambda api_key: fake_client)

    transfer = extract_transfer("image/jpeg", b"fake-bytes", [("empresa.mp", "123"), ("empresa.galicia", "456")])

    assert transfer.cuenta_receptora == "empresa.mp"
    tool_usado = fake_client.messages.calls[0]["tools"][0]
    assert tool_usado["input_schema"]["properties"]["cuenta_receptora"]["enum"] == [
        "empresa.mp",
        "empresa.galicia",
        None,
    ]


def test_extract_transfer_treats_placeholder_numero_operacion_as_missing(monkeypatch):
    resultado = {"monto": 500, "fecha_transaccion": "2026-08-24", "numero_operacion": "<UNKNOWN>"}
    fake_client = _FakeAnthropic([resultado])
    monkeypatch.setattr(extraction, "Anthropic", lambda api_key: fake_client)

    transfer = extract_transfer("image/jpeg", b"fake-bytes")

    assert transfer is not None
    assert transfer.numero_operacion is None


def test_extract_transfer_treats_explanatory_sentence_as_missing_numero_operacion(monkeypatch):
    resultado = {
        "monto": 500,
        "fecha_transaccion": "2026-08-24",
        "numero_operacion": "Número de operación no visible en el comprobante",
    }
    fake_client = _FakeAnthropic([resultado])
    monkeypatch.setattr(extraction, "Anthropic", lambda api_key: fake_client)

    transfer = extract_transfer("image/jpeg", b"fake-bytes")

    assert transfer is not None
    assert transfer.numero_operacion is None


def test_extract_transfer_treats_cvu_shaped_value_as_missing_numero_operacion(monkeypatch):
    resultado = {"monto": 500, "fecha_transaccion": "2026-08-24", "numero_operacion": "0000003100045397444527"}
    fake_client = _FakeAnthropic([resultado])
    monkeypatch.setattr(extraction, "Anthropic", lambda api_key: fake_client)

    transfer = extract_transfer("image/jpeg", b"fake-bytes")

    assert transfer is not None
    assert transfer.numero_operacion is None
