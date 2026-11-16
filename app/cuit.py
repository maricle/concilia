"""CUIT/CUIL: se guarda solo con digitos (11) y se muestra como 20-12345678-9.
Lo usan la extraccion de comprobantes, la lectura de extractos bancarios y la
conciliacion (comparar el CUIT de quien pago en los dos lados)."""

_PESOS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)


def _digito_verificador_ok(digitos: str) -> bool:
    resto = sum(int(d) * p for d, p in zip(digitos[:10], _PESOS, strict=True)) % 11
    esperado = {0: 0, 1: 9}.get(resto, 11 - resto)
    return int(digitos[10]) == esperado


def normalizar_cuit(texto: object) -> str | None:
    """Los 11 digitos del CUIT/CUIL, o None si no hay uno valido. Con el digito
    verificador mal se descarta: casi siempre es un digito mal leido de la foto o
    mal tipeado, y un CUIT equivocado haria fallar la conciliacion."""
    if texto is None:
        return None
    digitos = "".join(c for c in str(texto) if c.isdigit())
    if len(digitos) != 11 or not _digito_verificador_ok(digitos):
        return None
    return digitos


def formato_cuit(cuit: str | None) -> str:
    if not cuit:
        return "-"
    if len(cuit) != 11:
        return cuit
    return f"{cuit[:2]}-{cuit[2:10]}-{cuit[10]}"
