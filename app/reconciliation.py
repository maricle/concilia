import csv
import io
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from .cuit import normalizar_cuit
from .models import (
    ImportedStatement,
    Movement,
    ReconciliationState,
    RecordState,
    StatementLine,
    StatementLineState,
)
from .numeros import parse_monto_ar

DEFAULT_TOLERANCE_DIAS = 1

# Nombres de columna que se reconocen solos. Sirven para sugerir las columnas la
# primera vez que se importa un extracto de un banco (el administrador las
# confirma y quedan guardadas en FormatoExtracto) y para leer sin formato guardado.
_FECHA_ALIASES = {"fecha", "date", "fecha operacion", "fecha de la operacion", "fecha de origen"}
_MONTO_ALIASES = {
    "monto",
    "importe",
    "amount",
    "credito",
    "creditos",
    "haber",
    "importe credito",
    "valor de la compra",
}
_DESCRIPCION_ALIASES = {"descripcion", "concepto", "detalle", "description"}
_REFERENCIA_ALIASES = {
    "referencia",
    "numero_operacion",
    "nro_operacion",
    "nro operacion",
    "comprobante",
    "numero de comprobante",
    "numero de operacion",
    "id de operacion en mercado pago",
}
_TITULAR_ALIASES = {"titular", "nombre", "pagador", "ordenante", "nombre del pagador", "leyendas adicionales1"}
_CUIT_ALIASES = {
    "cuit",
    "cuil",
    "cuit/cuil",
    "cuit cuil",
    "cuit ordenante",
    "numero de identificacion del pagador",
    "leyendas adicionales2",
}

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y")

# El encabezado se busca en las primeras filas: algunos bancos ponen antes un
# titulo, la cuenta o el periodo.
_FILAS_ANTES_DEL_ENCABEZADO = 30


@dataclass
class StatementRow:
    fecha: datetime
    monto: Decimal
    descripcion: str | None
    referencia: str | None
    titular: str | None = None
    cuit: str | None = None


@dataclass
class ColumnasExtracto:
    """Nombre (tal como figura en el archivo) de la columna de cada dato."""

    fecha: str | None = None
    monto: str | None = None
    descripcion: str | None = None
    referencia: str | None = None
    titular: str | None = None
    cuit: str | None = None


# (campo, etiqueta en la pantalla, obligatorio)
CAMPOS_EXTRACTO = (
    ("fecha", "Fecha", True),
    ("monto", "Monto acreditado", True),
    ("referencia", "Numero de operacion / referencia", False),
    ("titular", "Titular (quien transfiere)", False),
    ("cuit", "CUIT/CUIL de quien transfiere", False),
    ("descripcion", "Descripcion", False),
)


class StatementParseError(ValueError):
    pass


def _normalize_header(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().strip().lower()
    return " ".join(text.split())


def _parse_fecha(raw: str) -> datetime:
    raw = raw.strip()
    try:
        # Cubre timestamps ISO 8601 completos, con hora y zona horaria (ej. Mercado
        # Pago: "2026-08-21T18:16:29.000-04:00"); la tolerancia de matching es por
        # dia, asi que solo interesa la fecha, no la hora ni la zona horaria.
        parsed = datetime.fromisoformat(raw)
        return datetime(parsed.year, parsed.month, parsed.day)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise StatementParseError(f"No se pudo interpretar la fecha '{raw}'.")


def _fecha_de_celda(celda: object) -> datetime:
    if isinstance(celda, datetime):
        return celda
    if isinstance(celda, date):
        return datetime(celda.year, celda.month, celda.day)
    return _parse_fecha(str(celda))


def _monto_de_celda(celda: object) -> Decimal:
    # Excel entrega los numeros ya interpretados: no pasan por la convencion
    # argentina de puntos y comas.
    if isinstance(celda, int | float | Decimal) and not isinstance(celda, bool):
        return Decimal(str(celda))
    try:
        return parse_monto_ar(celda)
    except ValueError as exc:
        raise StatementParseError(str(exc)) from exc


def _texto_de_celda(celda: object) -> str | None:
    if celda in (None, ""):
        return None
    if isinstance(celda, float) and celda.is_integer():
        celda = int(celda)  # un CUIT o una referencia que Excel guardo como numero
    texto = str(celda).strip()
    return texto or None


def leer_tabla(nombre_archivo: str, contenido: bytes) -> tuple[list[list], str]:
    """Todas las filas del archivo, celda por celda, y su formato (csv o xlsx)."""
    lower = nombre_archivo.lower()
    if lower.endswith(".csv"):
        try:
            texto = contenido.decode("utf-8-sig")
        except UnicodeDecodeError:
            texto = contenido.decode("latin-1")  # exportaciones de algunos bancos
        muestra = texto[:2048]
        delimitador = ";" if muestra.count(";") > muestra.count(",") else ","
        filas = [list(fila) for fila in csv.reader(io.StringIO(texto), delimiter=delimitador)]
        formato = "csv"
    elif lower.endswith(".xlsx"):
        workbook = load_workbook(io.BytesIO(contenido), data_only=True)
        filas = [list(fila) for fila in workbook.active.iter_rows(values_only=True)]
        formato = "xlsx"
    else:
        raise StatementParseError("Formato no soportado todavia: usa CSV o XLSX.")
    if not any(any(celda not in (None, "") for celda in fila) for fila in filas):
        raise StatementParseError("El archivo esta vacio.")
    return filas, formato


def _indice_de(encabezados: list, nombre: str | None) -> int | None:
    if not nombre:
        return None
    buscado = _normalize_header(nombre)
    for indice, encabezado in enumerate(encabezados):
        if encabezado is not None and _normalize_header(encabezado) == buscado:
            return indice
    return None


def _es_texto_de_encabezado(celda: object) -> bool:
    if not isinstance(celda, str) or not celda.strip():
        return False
    try:
        parse_monto_ar(celda)
        return False
    except ValueError:
        pass
    try:
        _parse_fecha(celda)
        return False
    except StatementParseError:
        return True


def buscar_encabezado(filas: list[list], columnas: ColumnasExtracto | None = None) -> int | None:
    """Indice de la fila de titulos. Con columnas guardadas, la primera fila que
    tiene las de fecha y monto (None si el archivo no las tiene: es otro formato).
    Sin columnas: la primera con titulos reconocibles de fecha y monto, y si no
    hay, la primera fila con mas titulos de texto (los datos tienen fechas y
    montos, el encabezado solo texto)."""
    primeras = filas[:_FILAS_ANTES_DEL_ENCABEZADO]
    if columnas is not None:
        for indice, fila in enumerate(primeras):
            if _indice_de(fila, columnas.fecha) is not None and _indice_de(fila, columnas.monto) is not None:
                return indice
        return None
    for indice, fila in enumerate(primeras):
        normalizados = {_normalize_header(c) for c in fila if c is not None}
        if normalizados & _FECHA_ALIASES and normalizados & _MONTO_ALIASES:
            return indice
    puntajes = [sum(1 for c in fila if _es_texto_de_encabezado(c)) for fila in primeras]
    if not puntajes or max(puntajes) < 2:
        return None
    return puntajes.index(max(puntajes))


def sugerir_columnas(encabezados: list) -> ColumnasExtracto:
    """Columnas reconocidas por su nombre (ver los *_ALIASES)."""

    def primera(aliases: set[str]) -> str | None:
        for encabezado in encabezados:
            if encabezado is not None and _normalize_header(encabezado) in aliases:
                return str(encabezado).strip()
        return None

    return ColumnasExtracto(
        fecha=primera(_FECHA_ALIASES),
        monto=primera(_MONTO_ALIASES),
        descripcion=primera(_DESCRIPCION_ALIASES),
        referencia=primera(_REFERENCIA_ALIASES),
        titular=primera(_TITULAR_ALIASES),
        cuit=primera(_CUIT_ALIASES),
    )


def leer_filas(filas: list[list], indice_encabezado: int, columnas: ColumnasExtracto) -> list[StatementRow]:
    """Las lineas del extracto debajo del encabezado. Se saltean las que no tienen
    fecha o monto, y las de monto cero (en extractos con columnas separadas de
    debitos y creditos, un debito deja el credito vacio o en cero)."""
    encabezados = filas[indice_encabezado]
    idx_fecha = _indice_de(encabezados, columnas.fecha)
    idx_monto = _indice_de(encabezados, columnas.monto)
    if idx_fecha is None or idx_monto is None:
        raise StatementParseError(
            "El archivo debe tener columnas de fecha y monto (por ejemplo 'Fecha' e 'Importe')."
        )
    idx_descripcion = _indice_de(encabezados, columnas.descripcion)
    idx_referencia = _indice_de(encabezados, columnas.referencia)
    idx_titular = _indice_de(encabezados, columnas.titular)
    idx_cuit = _indice_de(encabezados, columnas.cuit)

    def celda(fila: list, indice: int | None) -> object:
        return fila[indice] if indice is not None and indice < len(fila) else None

    rows: list[StatementRow] = []
    for fila in filas[indice_encabezado + 1 :]:
        fecha_cell = celda(fila, idx_fecha)
        monto_cell = celda(fila, idx_monto)
        if fecha_cell in (None, "") or monto_cell in (None, ""):
            continue
        monto = _monto_de_celda(monto_cell)
        if monto == 0:
            continue
        rows.append(
            StatementRow(
                fecha=_fecha_de_celda(fecha_cell),
                monto=monto,
                descripcion=_texto_de_celda(celda(fila, idx_descripcion)),
                referencia=_texto_de_celda(celda(fila, idx_referencia)),
                titular=_texto_de_celda(celda(fila, idx_titular)),
                cuit=normalizar_cuit(_texto_de_celda(celda(fila, idx_cuit))),
            )
        )
    return rows


def leer_sin_formato(filas: list[list]) -> list[StatementRow]:
    """Lee el extracto reconociendo las columnas solo por su nombre."""
    indice = buscar_encabezado(filas)
    if indice is None:
        raise StatementParseError(
            "El archivo debe tener columnas de fecha y monto (por ejemplo 'Fecha' e 'Importe')."
        )
    return leer_filas(filas, indice, sugerir_columnas(filas[indice]))


def parse_csv(contenido: bytes) -> list[StatementRow]:
    return leer_sin_formato(leer_tabla("extracto.csv", contenido)[0])


def parse_xlsx(contenido: bytes) -> list[StatementRow]:
    return leer_sin_formato(leer_tabla("extracto.xlsx", contenido)[0])


def parse_statement_file(nombre_archivo: str, contenido: bytes) -> tuple[list[StatementRow], str]:
    filas, formato = leer_tabla(nombre_archivo, contenido)
    return leer_sin_formato(filas), formato


def _candidatos_iniciales(session: Session, cuenta_bancaria_id: int) -> list[Movement]:
    """Movimientos que podrian matchear contra algun resumen de esta cuenta, consultados
    una unica vez: por linea solo se filtra en memoria (ver match_statement).

    Se excluye cualquier movimiento que ya tenga una StatementLine vinculada (via
    match_statement en una corrida anterior, o emparejado a mano) -- sin este
    filtro, un movimiento en CON_DIFERENCIA (esperando resolucion manual sobre esa
    linea puntual) podia volver a aparecer como candidato en una corrida posterior
    y quedar reasignado a otra linea, dejando la primera con una referencia obsoleta
    (bug real, confirmado con un repro manual)."""
    ya_vinculados = select(StatementLine.movimiento_id).where(StatementLine.movimiento_id.isnot(None))
    movimientos = session.scalars(
        select(Movement).where(
            Movement.estado_registro == RecordState.CONFIRMADO,
            Movement.estado_conciliacion.in_([ReconciliationState.PENDIENTE, ReconciliationState.CON_DIFERENCIA]),
            Movement.id.notin_(ya_vinculados),
        )
    ).all()
    return [
        m for m in movimientos if m.cuenta_bancaria_id is None or m.cuenta_bancaria_id == cuenta_bancaria_id
    ]


def _dentro_de_fecha(movimiento: Movement, linea: StatementLine, tolerancia_dias: int) -> bool:
    if movimiento.fecha_transaccion is None:
        return False
    return abs((movimiento.fecha_transaccion - linea.fecha).days) <= tolerancia_dias


_STOPWORDS_EMISOR = {"de", "del", "la", "el", "los", "las", "sa", "srl", "sociedad", "anonima"}


def _palabras_significativas(valor: str | None) -> set[str]:
    if not valor:
        return set()
    normalizado = _normalize_header(valor)
    return {palabra for palabra in normalizado.split() if len(palabra) >= 3 and palabra not in _STOPWORDS_EMISOR}


def _emisor_coincide(movimiento: Movement, linea: StatementLine) -> bool | None:
    """Si los dos lados tienen CUIT/CUIL, decide el CUIT: es el dato exacto. Si
    no, compara el titular extraido del comprobante contra la descripcion/pagador
    de la linea del resumen bancario -- son dos fuentes de texto libre distintas
    (una la escribe el banco, la otra la interpreta el LLM), asi que la
    comparacion es por superposicion de palabras significativas normalizadas, no
    por igualdad exacta (ej. "Jose Maria Ramirez" vs "RAMIREZ JOSE MARIA" matchea
    por compartir "ramirez"/"jose"/"maria"). Devuelve None (no concluyente) si a
    alguno de los dos lados le falta texto para comparar -- la ausencia de dato no
    debe invalidar un match que ya tiene referencia+importe+fecha a favor."""
    if movimiento.cuit and linea.cuit:
        return movimiento.cuit == linea.cuit
    palabras_movimiento = _palabras_significativas(movimiento.titular)
    palabras_linea = _palabras_significativas(linea.titular) | _palabras_significativas(linea.descripcion)
    if not palabras_movimiento or not palabras_linea:
        return None
    return bool(palabras_movimiento & palabras_linea)


def match_statement(
    session: Session,
    resumen: ImportedStatement,
    lineas: list[StatementLine],
    tolerancia_dias: int = DEFAULT_TOLERANCE_DIAS,
) -> None:
    usados: set[int] = set()
    pool = _candidatos_iniciales(session, resumen.cuenta_bancaria_id)

    for linea in lineas:
        candidatos = [m for m in pool if m.id not in usados]

        # El numero de operacion es el dato mas confiable cuando esta disponible en
        # ambos lados: un match exacto de referencia manda por encima de la fecha,
        # porque el monto/fecha extraidos del comprobante pueden venir mal (OCR,
        # error de tipeo del vendedor) sin que eso signifique que no es el mismo
        # movimiento. Antes de confirmar igual se valida importe, fecha y emisor --
        # si alguno no calza (el emisor solo cuenta cuando hay evidencia clara de
        # que NO son la misma persona, no por falta de dato) queda "con diferencia"
        # en vez de "conciliado", para que el administrador lo revise.
        if linea.referencia:
            por_referencia = [m for m in candidatos if m.numero_operacion == linea.referencia]
            if len(por_referencia) == 1:
                candidato = por_referencia[0]
                coincide = (
                    candidato.monto == linea.monto
                    and _dentro_de_fecha(candidato, linea, tolerancia_dias)
                    and _emisor_coincide(candidato, linea) is not False
                )
                estado = ReconciliationState.CONCILIADO if coincide else ReconciliationState.CON_DIFERENCIA
                _asignar(linea, candidato, estado, usados)
                continue
            if len(por_referencia) > 1:
                continue

        # Sin numero de operacion: importe es el criterio principal, fecha el
        # segundo -- ambos son obligatorios, no hay match sin los dos. Con esos
        # dos solos alcanza para vincular la linea (evita dejar todo pendiente),
        # pero no para darlo por CONFIABLE: sin una referencia que lo avale, el
        # emisor es la unica corroboracion posible, y si no hay coincidencia
        # positiva de palabras (ya sea porque falta el dato o porque el nombre no
        # tiene nada que ver) el match queda "con diferencia" como advertencia de
        # que solo se verifico fecha+monto, para que el administrador lo confirme.
        por_fecha = [m for m in candidatos if _dentro_de_fecha(m, linea, tolerancia_dias)]
        exactos = [m for m in por_fecha if m.monto == linea.monto]
        if len(exactos) == 1:
            candidato = exactos[0]
            estado = (
                ReconciliationState.CONCILIADO
                if _emisor_coincide(candidato, linea) is True
                else ReconciliationState.CON_DIFERENCIA
            )
            _asignar(linea, candidato, estado, usados)
            continue
        if len(exactos) > 1:
            por_emisor = [m for m in exactos if _emisor_coincide(m, linea) is True]
            if len(por_emisor) == 1:
                _asignar(linea, por_emisor[0], ReconciliationState.CONCILIADO, usados)
            continue


def _asignar(
    linea: StatementLine, movimiento: Movement, estado: ReconciliationState, usados: set[int]
) -> None:
    linea.movimiento_id = movimiento.id
    linea.estado = StatementLineState.CONCILIADA
    movimiento.estado_conciliacion = estado
    if movimiento.cuenta_bancaria_id is None:
        movimiento.cuenta_bancaria_id = linea.resumen.cuenta_bancaria_id
    usados.add(movimiento.id)
