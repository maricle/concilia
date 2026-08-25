from dataclasses import dataclass
from datetime import datetime, timedelta
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


def test_has_minimum_fields_requires_monto():
    resultado = {"monto": None, "fecha_transaccion": "2026-08-24", "numero_operacion": "OP-123"}
    assert _has_minimum_fields(resultado) is False


def test_has_minimum_fields_accepts_monto_even_without_fecha_ni_numero_operacion():
    resultado = {"monto": 500, "fecha_transaccion": None, "numero_operacion": None}
    assert _has_minimum_fields(resultado) is True


def test_has_minimum_fields_rejects_unparseable_monto():
    resultado = {"monto": "no es un numero", "fecha_transaccion": "2026-08-24", "numero_operacion": "OP-123"}
    assert _has_minimum_fields(resultado) is False


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


def test_extract_transfer_treats_placeholder_numero_operacion_as_missing(monkeypatch):
    resultado = {"monto": 500, "fecha_transaccion": "2026-08-24", "numero_operacion": "<UNKNOWN>"}
    fake_client = _FakeAnthropic([resultado])
    monkeypatch.setattr(extraction, "Anthropic", lambda api_key: fake_client)

    transfer = extract_transfer("image/jpeg", b"fake-bytes")

    assert transfer is not None
    assert transfer.numero_operacion is None
