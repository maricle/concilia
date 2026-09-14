import io
from collections import defaultdict
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import Movement, Operator, Reparto
from .numeros import formato_monto_ar as _formato_monto

_styles = getSampleStyleSheet()
_TITULO_STYLE = ParagraphStyle("TituloSalida", parent=_styles["Heading1"], fontSize=16, spaceAfter=6)
_SUBTITULO_STYLE = ParagraphStyle("SubtituloSalida", parent=_styles["Heading2"], fontSize=12, spaceAfter=6)
_NORMAL_STYLE = _styles["Normal"]
# Estilos de celda usados via Paragraph (no como texto plano) para que las
# tablas ajusten el texto largo al ancho de columna en vez de desbordarlo
# encima de la celda vecina (ej. numeros de operacion largos).
_CELDA_STYLE = ParagraphStyle("Celda", parent=_styles["Normal"], fontSize=8, leading=10)
_CELDA_STYLE_HEADER = ParagraphStyle("CeldaHeader", parent=_CELDA_STYLE, textColor=colors.white, fontName="Helvetica-Bold")
_CELDA_STYLE_RIGHT = ParagraphStyle("CeldaRight", parent=_CELDA_STYLE, alignment=TA_RIGHT)
_CELDA_STYLE_HEADER_RIGHT = ParagraphStyle("CeldaHeaderRight", parent=_CELDA_STYLE_HEADER, alignment=TA_RIGHT)


def _celda(texto: str, *, right: bool = False, header: bool = False) -> Paragraph:
    if header:
        style = _CELDA_STYLE_HEADER_RIGHT if right else _CELDA_STYLE_HEADER
    else:
        style = _CELDA_STYLE_RIGHT if right else _CELDA_STYLE
    return Paragraph(str(texto), style)


def _formato_fecha_hora(valor) -> str:
    return valor.strftime("%Y-%m-%d %H:%M") if valor else "-"


def _elementos_resumen_salida(reparto: Reparto, movimientos: list[Movement], operadores: list[Operator]) -> list:
    numero = reparto.numero_reparto if reparto.numero_reparto is not None else "sin numero"
    elementos = [
        Paragraph(f"Resumen de Salida Nº {numero}", _TITULO_STYLE),
        Paragraph(f"Movil: {reparto.movil.numero} - {reparto.movil.nombre}", _NORMAL_STYLE),
        Paragraph(f"Fecha: {reparto.fecha.strftime('%Y-%m-%d')}", _NORMAL_STYLE),
        Paragraph(f"Hora inicio: {_formato_fecha_hora(reparto.hora_inicio)}", _NORMAL_STYLE),
        Paragraph(f"Hora fin: {_formato_fecha_hora(reparto.hora_fin)}", _NORMAL_STYLE),
        Paragraph(
            "Operadores: " + (", ".join(operador.nombre for operador in operadores) or "-"),
            _NORMAL_STYLE,
        ),
        Spacer(1, 0.5 * cm),
    ]

    elementos.append(Paragraph("Comprobantes registrados", _SUBTITULO_STYLE))
    encabezado = ["Fecha transaccion", "Banco", "Factura/Cuenta", "N. operacion", "Vendedor", "Monto"]
    filas = [[_celda(texto, header=True, right=(i == len(encabezado) - 1)) for i, texto in enumerate(encabezado)]]
    total_general = Decimal("0")
    for movimiento in movimientos:
        if movimiento.monto is not None:
            total_general += movimiento.monto
        filas.append(
            [
                _celda(_formato_fecha_hora(movimiento.fecha_transaccion)),
                _celda(movimiento.cuenta_bancaria.banco if movimiento.cuenta_bancaria else "-"),
                _celda(movimiento.factura_o_cuenta_numero or "-"),
                _celda(movimiento.numero_operacion or "-"),
                _celda(movimiento.operador.nombre),
                _celda(_formato_monto(movimiento.monto), right=True),
            ]
        )
    if len(filas) == 1:
        filas.append([_celda("Sin comprobantes registrados")] + [_celda("")] * 5)

    tabla = Table(filas, repeatRows=1, colWidths=[2.6 * cm, 2.6 * cm, 2.8 * cm, 3 * cm, 3.2 * cm, 2.5 * cm])
    tabla.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4e73df")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    elementos.append(tabla)
    elementos.append(Spacer(1, 0.3 * cm))
    elementos.append(Paragraph(f"Total general: {_formato_monto(total_general)}", _SUBTITULO_STYLE))
    elementos.append(Spacer(1, 0.5 * cm))

    elementos.append(Paragraph("Desglose por cuenta bancaria", _SUBTITULO_STYLE))
    subtotales: dict[str, Decimal] = defaultdict(Decimal)
    cantidades: dict[str, int] = defaultdict(int)
    for movimiento in movimientos:
        banco = movimiento.cuenta_bancaria.banco if movimiento.cuenta_bancaria else "Sin banco"
        subtotales[banco] += movimiento.monto or Decimal("0")
        cantidades[banco] += 1
    filas_bancos = [
        [_celda("Banco", header=True), _celda("Cantidad", header=True, right=True), _celda("Subtotal", header=True, right=True)]
    ] + [
        [_celda(banco), _celda(cantidades[banco], right=True), _celda(_formato_monto(subtotal), right=True)]
        for banco, subtotal in sorted(subtotales.items())
    ]
    if len(filas_bancos) == 1:
        filas_bancos.append([_celda("Sin comprobantes registrados"), _celda(""), _celda("")])
    tabla_bancos = Table(filas_bancos, colWidths=[7 * cm, 2.5 * cm, 3.5 * cm])
    tabla_bancos.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4e73df")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    elementos.append(tabla_bancos)
    return elementos


def _construir_pdf(elementos: list) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
    )
    doc.build(elementos)
    return buffer.getvalue()


def generar_resumen_salida_pdf(reparto: Reparto, movimientos: list[Movement], operadores: list[Operator]) -> bytes:
    """Arma un PDF con los datos de la salida, el detalle de comprobantes
    registrados durante ella y un desglose de totales por cuenta bancaria."""
    return _construir_pdf(_elementos_resumen_salida(reparto, movimientos, operadores))


def generar_resumen_salidas_pdf(
    items: list[tuple[Reparto, list[Movement], list[Operator]]],
) -> bytes:
    """Igual que generar_resumen_salida_pdf pero para varias salidas juntas en un
    solo PDF -- una salida por pagina, para descargar el resumen de varias de
    una sola vez desde el listado de /repartos."""
    elementos: list = []
    for indice, (reparto, movimientos, operadores) in enumerate(items):
        if indice > 0:
            elementos.append(PageBreak())
        elementos.extend(_elementos_resumen_salida(reparto, movimientos, operadores))
    return _construir_pdf(elementos)
