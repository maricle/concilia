"""Interpretacion de montos en formato numerico argentino (punto = separador de
miles, coma = separador decimal), compartida entre extraction.py (comprobantes
leidos por Claude) y reconciliation.py (resumenes bancarios en CSV/XLSX)."""

from decimal import Decimal, InvalidOperation


def parse_monto_ar(raw: object) -> Decimal:
    """Convierte un monto a Decimal asumiendo convencion argentina cuando hay
    ambiguedad. El caso problematico es un monto SIN coma: "58.316" son 58316
    pesos (punto de miles), no 58,316 (58 con 316 milesimos) -- los montos en
    pesos practicamente nunca tienen 3 decimales, asi que un grupo de exactamente
    3 digitos despues del ultimo punto, sin coma en el numero, se interpreta como
    separador de miles."""
    text = str(raw).strip().replace("$", "").replace(" ", "")
    if "," in text and "." in text:
        if text.rindex(",") > text.rindex("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    elif "." in text:
        entero, _, ultimo_grupo = text.rpartition(".")
        if entero and len(ultimo_grupo) == 3:
            text = text.replace(".", "")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"No se pudo interpretar el monto '{raw}'.") from exc
