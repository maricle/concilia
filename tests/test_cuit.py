import pytest

from app.cuit import formato_cuit, normalizar_cuit


@pytest.mark.parametrize(
    "valor,esperado",
    [
        ("20-12345678-6", "20123456786"),
        ("20123456786", "20123456786"),
        (" 27 33445566 7 ", "27334455667"),
        ("CUIT: 20-14981111-8", "20149811118"),
        (20149811118, "20149811118"),
        ("20-12345678-9", None),  # digito verificador mal: un numero mal leido
        ("2012345678", None),  # 10 digitos
        ("", None),
        (None, None),
    ],
)
def test_normalizar_cuit(valor, esperado):
    assert normalizar_cuit(valor) == esperado


def test_formato_cuit():
    assert formato_cuit("20123456786") == "20-12345678-6"
    assert formato_cuit(None) == "-"
