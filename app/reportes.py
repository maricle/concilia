import io
from collections import defaultdict
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import Movement, Operator, Reparto


def _formato_monto(monto: Decimal | None) -> str:
    if monto is None:
        return "-"
    return f"${monto:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _formato_fecha_hora(valor) -> str:
    return valor.strftime("%Y-%m-%d %H:%M") if valor else "-"


def generar_resumen_salida_pdf(reparto: Reparto, movimientos: list[Movement], operadores: list[Operator]) -> bytes:
    """Arma un PDF con los datos de la salida, el detalle de comprobantes
    registrados durante ella y un desglose de totales por cuenta bancaria."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
    )
    styles = getSampleStyleSheet()
    titulo_style = ParagraphStyle("TituloSalida", parent=styles["Heading1"], fontSize=16, spaceAfter=6)
    subtitulo_style = ParagraphStyle("SubtituloSalida", parent=styles["Heading2"], fontSize=12, spaceAfter=6)
    normal_style = styles["Normal"]

    numero = reparto.numero_reparto if reparto.numero_reparto is not None else "sin numero"
    elementos = [
        Paragraph(f"Resumen de Salida Nº {numero}", titulo_style),
        Paragraph(f"Movil: {reparto.movil.numero} - {reparto.movil.nombre}", normal_style),
        Paragraph(f"Fecha: {reparto.fecha.strftime('%Y-%m-%d')}", normal_style),
        Paragraph(f"Hora inicio: {_formato_fecha_hora(reparto.hora_inicio)}", normal_style),
        Paragraph(f"Hora fin: {_formato_fecha_hora(reparto.hora_fin)}", normal_style),
        Paragraph(
            "Operadores: " + (", ".join(operador.nombre for operador in operadores) or "-"),
            normal_style,
        ),
        Spacer(1, 0.5 * cm),
    ]

    elementos.append(Paragraph("Comprobantes registrados", subtitulo_style))
    encabezado = ["Fecha transaccion", "Banco", "Factura/Cuenta", "N. operacion", "Vendedor", "Monto"]
    filas = [encabezado]
    total_general = Decimal("0")
    for movimiento in movimientos:
        if movimiento.monto is not None:
            total_general += movimiento.monto
        filas.append(
            [
                _formato_fecha_hora(movimiento.fecha_transaccion),
                movimiento.cuenta_bancaria.banco if movimiento.cuenta_bancaria else "-",
                movimiento.factura_o_cuenta_numero or "-",
                movimiento.numero_operacion or "-",
                movimiento.operador.nombre,
                _formato_monto(movimiento.monto),
            ]
        )
    if len(filas) == 1:
        filas.append(["Sin comprobantes registrados", "", "", "", "", ""])

    tabla = Table(filas, repeatRows=1, colWidths=[2.6 * cm, 2.6 * cm, 2.8 * cm, 3 * cm, 3.2 * cm, 2.5 * cm])
    tabla.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4e73df")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    elementos.append(tabla)
    elementos.append(Spacer(1, 0.3 * cm))
    elementos.append(Paragraph(f"Total general: {_formato_monto(total_general)}", subtitulo_style))
    elementos.append(Spacer(1, 0.5 * cm))

    elementos.append(Paragraph("Desglose por cuenta bancaria", subtitulo_style))
    subtotales: dict[str, Decimal] = defaultdict(Decimal)
    for movimiento in movimientos:
        banco = movimiento.cuenta_bancaria.banco if movimiento.cuenta_bancaria else "Sin banco"
        subtotales[banco] += movimiento.monto or Decimal("0")
    filas_bancos = [["Banco", "Subtotal"]] + [
        [banco, _formato_monto(subtotal)] for banco, subtotal in sorted(subtotales.items())
    ]
    if len(filas_bancos) == 1:
        filas_bancos.append(["Sin comprobantes registrados", ""])
    tabla_bancos = Table(filas_bancos, colWidths=[8 * cm, 4 * cm])
    tabla_bancos.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4e73df")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
            ]
        )
    )
    elementos.append(tabla_bancos)

    doc.build(elementos)
    return buffer.getvalue()
