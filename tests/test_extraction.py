from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import pytest

from app import extraction
from app.extraction import _es_valor_valido, _has_minimum_fields, extract_transfer


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


def test_has_minimum_fields_rejects_placeholder_numero_operacion():
    resultado = {"monto": 500, "fecha_transaccion": "2026-08-24", "numero_operacion": "<UNKNOWN>"}
    assert _has_minimum_fields(resultado) is False


def test_has_minimum_fields_accepts_real_values():
    resultado = {"monto": 500, "fecha_transaccion": "2026-08-24", "numero_operacion": "OP-123"}
    assert _has_minimum_fields(resultado) is True


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

    def create(self, **kwargs) -> _FakeResponse:
        resultado = self._responses.pop(0)
        if resultado is None:
            return _FakeResponse(content=[])
        return _FakeResponse(content=[_FakeBlock(type="tool_use", input=resultado)])


class _FakeAnthropic:
    def __init__(self, responses: list[dict | None]):
        self.messages = _FakeMessages(responses)


def test_extract_transfer_retries_with_sonnet_when_haiku_uses_placeholder(monkeypatch):
    haiku_result = {"monto": 500, "fecha_transaccion": "2026-08-24", "numero_operacion": "<UNKNOWN>"}
    sonnet_result = {"monto": 500, "fecha_transaccion": "2026-08-24", "numero_operacion": "OP-999"}
    fake_client = _FakeAnthropic([haiku_result, sonnet_result])
    monkeypatch.setattr(extraction, "Anthropic", lambda api_key: fake_client)

    transfer = extract_transfer("image/jpeg", b"fake-bytes")

    assert transfer is not None
    assert transfer.numero_operacion == "OP-999"
    assert transfer.monto == Decimal("500")
    assert transfer.fecha_transaccion == datetime(2026, 8, 24)


def test_extract_transfer_gives_up_when_both_models_use_placeholder(monkeypatch):
    placeholder_result = {"monto": 500, "fecha_transaccion": "2026-08-24", "numero_operacion": "<UNKNOWN>"}
    fake_client = _FakeAnthropic([placeholder_result, placeholder_result])
    monkeypatch.setattr(extraction, "Anthropic", lambda api_key: fake_client)

    assert extract_transfer("image/jpeg", b"fake-bytes") is None
