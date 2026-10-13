"""Comprobantes: listado, exportacion, carga manual, correccion y eliminacion."""

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..conversation import buscar_cuenta_bancaria
from ..extraction import extract_transfer
from ..models import (
    BankAccount,
    Movement,
    Movil,
    Operator,
    PanelUser,
    ReconciliationState,
    RecordState,
    StatementLine,
    StatementLineState,
    TipoIdentificador,
)
from ..storage import get_comprobante_archivo, save_comprobante_archivo
from ..zona_horaria import hoy_argentina
from .comun import (
    del_dia,
    existe_duplicado,
    get_db,
    obtener_o_redirigir,
    require_admin,
    require_user,
    respuesta_xlsx,
    templates,
    volver_a,
)

router = APIRouter()
logger = logging.getLogger(__name__)

COMPROBANTES_POR_PAGINA = 50
TODOS_LOS_DIAS = "todos"


def _dia_seleccionado(fecha: str, hay_rango_de_fechas: bool) -> date | None:
    """Dia por el que se filtra el listado (fecha de transaccion), igual que en
    /conciliaciones: hoy por defecto, `fecha=todos` para ver todos los dias.
    Si se usa un rango de fechas de los filtros avanzados, manda el rango y no
    se filtra por dia (si no, los dos filtros se pisarian)."""
    if fecha == TODOS_LOS_DIAS or hay_rango_de_fechas:
        return None
    try:
        return datetime.strptime(fecha, "%Y-%m-%d").date()
    except ValueError:
        return hoy_argentina()

_COLUMNAS_ORDENABLES = {
    "fecha_transaccion": Movement.fecha_transaccion,
    "fecha_subida": Movement.fecha_subida,
    "monto": Movement.monto,
    "numero_operacion": Movement.numero_operacion,
    "conciliacion": Movement.estado_conciliacion,
}


def _comprobantes_query(
    banco: str,
    estado_conciliacion: str,
    operador_id: str,
    fecha_transaccion_desde: str,
    fecha_transaccion_hasta: str,
    fecha_subida_desde: str,
    fecha_subida_hasta: str,
    q: str,
    sort: str = "",
    order: str = "asc",
    movil_id: str = "",
    dia: date | None = None,
):
    query = (
        select(Movement)
        .where(Movement.estado_registro == RecordState.CONFIRMADO)
        .options(
            selectinload(Movement.operador),
            selectinload(Movement.cuenta_bancaria),
            selectinload(Movement.movil),
            selectinload(Movement.reparto),
        )
    )
    if banco:
        query = query.where(Movement.cuenta_bancaria_id == int(banco))
    if estado_conciliacion:
        query = query.where(Movement.estado_conciliacion == ReconciliationState(estado_conciliacion))
    if operador_id:
        query = query.where(Movement.operador_id == int(operador_id))
    if movil_id:
        query = query.where(Movement.movil_id == int(movil_id))
    if dia is not None:
        query = query.where(del_dia(Movement.fecha_transaccion, dia))
    if fecha_transaccion_desde:
        query = query.where(Movement.fecha_transaccion >= datetime.strptime(fecha_transaccion_desde, "%Y-%m-%d"))
    if fecha_transaccion_hasta:
        query = query.where(
            Movement.fecha_transaccion < datetime.strptime(fecha_transaccion_hasta, "%Y-%m-%d") + timedelta(days=1)
        )
    if fecha_subida_desde:
        query = query.where(Movement.fecha_subida >= datetime.strptime(fecha_subida_desde, "%Y-%m-%d"))
    if fecha_subida_hasta:
        query = query.where(Movement.fecha_subida < datetime.strptime(fecha_subida_hasta, "%Y-%m-%d") + timedelta(days=1))
    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.where(
            or_(
                Movement.numero_operacion.ilike(needle),
                Movement.factura_o_cuenta_numero.ilike(needle),
                Movement.titular.ilike(needle),
                Movement.banco_emisor.ilike(needle),
            )
        )
    if sort == "vendedor":
        query = query.join(Operator, Movement.operador_id == Operator.id)
        columna_orden = Operator.nombre
    elif sort == "cuenta_banco":
        query = query.outerjoin(BankAccount, Movement.cuenta_bancaria_id == BankAccount.id)
        columna_orden = BankAccount.banco
    else:
        columna_orden = _COLUMNAS_ORDENABLES.get(sort, Movement.fecha_subida)
    descendente = order == "desc" if sort else True  # sin sort explicito, default = fecha_subida desc (como antes)
    orden = columna_orden.desc() if descendente else columna_orden.asc()
    return query.order_by(orden.nullslast())


@router.get("/comprobantes")
def list_movimientos(
    request: Request,
    banco: str = "",
    estado_conciliacion: str = "",
    operador_id: str = "",
    fecha_transaccion_desde: str = "",
    fecha_transaccion_hasta: str = "",
    fecha_subida_desde: str = "",
    fecha_subida_hasta: str = "",
    q: str = "",
    sort: str = "",
    order: str = "asc",
    movil_id: str = "",
    fecha: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    dia = _dia_seleccionado(
        fecha, any((fecha_transaccion_desde, fecha_transaccion_hasta, fecha_subida_desde, fecha_subida_hasta))
    )
    query = _comprobantes_query(
        banco, estado_conciliacion, operador_id,
        fecha_transaccion_desde, fecha_transaccion_hasta, fecha_subida_desde, fecha_subida_hasta, q,
        sort, order, movil_id, dia,
    )
    # Paginado: el listado crece todos los dias y antes se traia entero (con sus
    # relaciones) en cada visita. Exportar sigue bajando todo lo filtrado.
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    total_paginas = max((total + COMPROBANTES_POR_PAGINA - 1) // COMPROBANTES_POR_PAGINA, 1)
    pagina = min(max(page, 1), total_paginas)
    movimientos = db.scalars(query.limit(COMPROBANTES_POR_PAGINA).offset((pagina - 1) * COMPROBANTES_POR_PAGINA)).all()
    cuentas = db.scalars(select(BankAccount).order_by(BankAccount.id)).all()
    operadores = db.scalars(select(Operator).order_by(Operator.nombre)).all()
    moviles = db.scalars(select(Movil).order_by(Movil.nombre)).all()

    return templates.TemplateResponse(
        request,
        "movimientos.html",
        {
            "user": user,
            "movimientos": movimientos,
            "dia": dia,
            "fecha": fecha,
            "hay_rango_de_fechas": any(
                (fecha_transaccion_desde, fecha_transaccion_hasta, fecha_subida_desde, fecha_subida_hasta)
            ),
            "dia_anterior": (dia - timedelta(days=1)).isoformat() if dia else None,
            "dia_siguiente": (dia + timedelta(days=1)).isoformat() if dia else None,
            "pagina": pagina,
            "total_paginas": total_paginas,
            "total_movimientos": total,
            "cuentas": cuentas,
            "operadores": operadores,
            "moviles": moviles,
            "banco": banco,
            "estado_conciliacion": estado_conciliacion,
            "operador_id": operador_id,
            "fecha_transaccion_desde": fecha_transaccion_desde,
            "fecha_transaccion_hasta": fecha_transaccion_hasta,
            "fecha_subida_desde": fecha_subida_desde,
            "fecha_subida_hasta": fecha_subida_hasta,
            "q": q,
            "sort": sort,
            "order": order,
            "movil_id": movil_id,
        },
    )


@router.get("/comprobantes/exportar")
def comprobantes_exportar(
    banco: str = "",
    estado_conciliacion: str = "",
    operador_id: str = "",
    fecha_transaccion_desde: str = "",
    fecha_transaccion_hasta: str = "",
    fecha_subida_desde: str = "",
    fecha_subida_hasta: str = "",
    q: str = "",
    sort: str = "",
    order: str = "asc",
    movil_id: str = "",
    fecha: str = "",
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    dia = _dia_seleccionado(
        fecha, any((fecha_transaccion_desde, fecha_transaccion_hasta, fecha_subida_desde, fecha_subida_hasta))
    )
    movimientos = db.scalars(
        _comprobantes_query(
            banco, estado_conciliacion, operador_id,
            fecha_transaccion_desde, fecha_transaccion_hasta, fecha_subida_desde, fecha_subida_hasta, q,
            sort, order, movil_id, dia,
        )
    ).all()

    filas = [
        [
            movimiento.fecha_transaccion.strftime("%Y-%m-%d %H:%M") if movimiento.fecha_transaccion else "",
            movimiento.fecha_subida.strftime("%Y-%m-%d %H:%M"),
            movimiento.factura_o_cuenta_tipo.value if movimiento.factura_o_cuenta_tipo else "",
            movimiento.factura_o_cuenta_numero or "",
            movimiento.cuenta_bancaria.banco if movimiento.cuenta_bancaria else "Sin banco",
            movimiento.banco_emisor or "",
            movimiento.titular or "",
            movimiento.numero_operacion or "",
            movimiento.monto if movimiento.monto is not None else "",
            movimiento.operador.nombre,
            movimiento.movil.numero if movimiento.movil else "",
            movimiento.reparto.numero_reparto if movimiento.reparto and movimiento.reparto.numero_reparto is not None else "",
            movimiento.estado_conciliacion.value,
        ]
        for movimiento in movimientos
    ]
    return respuesta_xlsx(
        [
            "Fecha transaccion", "Fecha subida", "Tipo", "Nro. factura/cuenta", "Cuenta banco", "Banco emisor",
            "Titular/Emisor", "N. operacion", "Monto", "Vendedor", "Movil", "Nro. Salida", "Conciliacion",
        ],
        filas,
        "comprobantes.xlsx",
    )


@router.get("/comprobantes/{movimiento_id}/archivo")
def ver_archivo_movimiento(
    movimiento_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    movimiento = db.get(Movement, movimiento_id)
    if movimiento is None or movimiento.archivo_id is None:
        return RedirectResponse("/comprobantes", status_code=303)

    try:
        archivo = get_comprobante_archivo(movimiento.archivo_id)
    except Exception:
        logger.exception("No se pudo leer el archivo del comprobante %s", movimiento_id)
        return RedirectResponse("/comprobantes", status_code=303)
    if archivo is None:
        return RedirectResponse("/comprobantes", status_code=303)

    return Response(
        content=archivo.contenido,
        media_type=archivo.content_type,
        headers={"Content-Disposition": f'inline; filename="{archivo.nombre_archivo}"'},
    )


@router.post("/comprobantes/nuevo/extraer")
async def extraer_datos_comprobante(
    archivo: UploadFile, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    """Corre sobre el archivo adjunto la misma extraccion con IA que usa el flujo
    de Telegram (app/extraction.py), para precompletar el formulario de carga
    manual en vez de tipear todo a mano. Devuelve el resultado como JSON: el
    formulario sigue siendo la fuente de verdad, esto solo lo pre-llena."""
    contenido = await archivo.read()
    if not contenido:
        return {"ok": False, "error": "El archivo esta vacio."}

    try:
        transfer = extract_transfer(archivo.content_type or "application/octet-stream", contenido)
    except Exception:
        # extract_transfer no atrapa errores de la API de Anthropic (ver mismo
        # patron en telegram.py) -- aca si hace falta, porque el punto de este
        # endpoint es degradar sin romper el alta manual (completar a mano sigue
        # funcionando aunque la extraccion falle por un problema de red/API).
        logger.exception("Fallo la extraccion de datos para la carga manual de comprobantes")
        return {"ok": False, "error": "No pudimos leer el comprobante. Completa los datos a mano."}

    if transfer is None:
        return {"ok": False, "error": "No pudimos leer el comprobante. Completa los datos a mano."}

    cuenta = buscar_cuenta_bancaria(db, transfer.cuenta_receptora)
    return {
        "ok": True,
        "monto": str(transfer.monto) if transfer.monto is not None else None,
        "fecha_transaccion": transfer.fecha_transaccion.strftime("%Y-%m-%dT%H:%M"),
        "numero_operacion": transfer.numero_operacion,
        "banco_emisor": transfer.banco_emisor,
        "titular": transfer.titular,
        "cuenta_bancaria_id": cuenta.id if cuenta is not None else None,
    }


@router.get("/comprobantes/nuevo")
def nuevo_movimiento_form(request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)):
    return templates.TemplateResponse(
        request,
        "nuevo_movimiento.html",
        {
            "user": user,
            "operadores": db.scalars(select(Operator).where(Operator.activo.is_(True)).order_by(Operator.nombre)).all(),
            "cuentas": db.scalars(select(BankAccount).order_by(BankAccount.id)).all(),
            "error": None,
        },
    )


@router.post("/comprobantes/nuevo")
async def nuevo_movimiento_submit(
    request: Request,
    operador_id: str = Form(...),
    cuenta_bancaria_id: str = Form(...),
    fecha_transaccion: str = Form(...),
    monto: str = Form(...),
    numero_operacion: str = Form(""),
    banco_emisor: str = Form(""),
    titular: str = Form(""),
    factura_o_cuenta_tipo: str = Form(""),
    factura_o_cuenta_numero: str = Form(""),
    archivo: UploadFile | None = None,
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    def _reformar(error: str):
        return templates.TemplateResponse(
            request,
            "nuevo_movimiento.html",
            {
                "user": user,
                "operadores": db.scalars(select(Operator).where(Operator.activo.is_(True)).order_by(Operator.nombre)).all(),
                "cuentas": db.scalars(select(BankAccount).order_by(BankAccount.id)).all(),
                "error": error,
            },
            status_code=400,
        )

    try:
        nueva_fecha = datetime.strptime(fecha_transaccion, "%Y-%m-%dT%H:%M")
        nuevo_monto = Decimal(monto)
    except (ValueError, InvalidOperation):
        return _reformar("Fecha o monto invalido.")

    numero_operacion = numero_operacion.strip() or None
    if numero_operacion and existe_duplicado(db, Movement.numero_operacion, numero_operacion):
        return _reformar(f"Ya existe otro comprobante con el numero {numero_operacion}.")

    archivo_id = None
    if archivo is not None and archivo.filename:
        contenido = await archivo.read()
        if contenido:
            archivo_id = save_comprobante_archivo(archivo.filename, archivo.content_type or "application/octet-stream", contenido)

    movimiento = Movement(
        operador_id=int(operador_id),
        cuenta_bancaria_id=int(cuenta_bancaria_id),
        fecha_transaccion=nueva_fecha,
        monto=nuevo_monto,
        numero_operacion=numero_operacion,
        banco_emisor=banco_emisor or None,
        titular=titular or None,
        factura_o_cuenta_tipo=TipoIdentificador(factura_o_cuenta_tipo) if factura_o_cuenta_tipo else None,
        factura_o_cuenta_numero=factura_o_cuenta_numero or None,
        archivo_id=archivo_id,
        origen="panel",
        estado_registro=RecordState.CONFIRMADO,
    )
    db.add(movimiento)
    db.commit()
    return RedirectResponse("/comprobantes", status_code=303)


@router.get("/comprobantes/{movimiento_id}/editar")
def editar_movimiento_form(
    movimiento_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    movimiento = obtener_o_redirigir(db, Movement, movimiento_id, "/comprobantes")
    cuentas = db.scalars(select(BankAccount).order_by(BankAccount.id)).all()
    return templates.TemplateResponse(
        request, "editar_movimiento.html", {"user": user, "movimiento": movimiento, "cuentas": cuentas, "error": None}
    )


@router.post("/comprobantes/{movimiento_id}/editar")
def editar_movimiento_submit(
    movimiento_id: int,
    request: Request,
    fecha_transaccion: str = Form(...),
    monto: str = Form(""),
    numero_operacion: str = Form(""),
    banco_emisor: str = Form(""),
    cuenta_bancaria_id: str = Form(""),
    titular: str = Form(""),
    factura_o_cuenta_tipo: str = Form(""),
    factura_o_cuenta_numero: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    movimiento = obtener_o_redirigir(db, Movement, movimiento_id, "/comprobantes")
    cuentas = db.scalars(select(BankAccount).order_by(BankAccount.id)).all()

    try:
        nueva_fecha = datetime.strptime(fecha_transaccion, "%Y-%m-%dT%H:%M")
        nuevo_monto = Decimal(monto) if monto.strip() else None
    except (ValueError, InvalidOperation):
        return templates.TemplateResponse(
            request,
            "editar_movimiento.html",
            {"user": user, "movimiento": movimiento, "cuentas": cuentas, "error": "Fecha o monto invalido."},
            status_code=400,
        )

    numero_operacion = numero_operacion.strip() or None
    if numero_operacion and existe_duplicado(db, Movement.numero_operacion, numero_operacion, exclude_id=movimiento.id):
        return templates.TemplateResponse(
            request,
            "editar_movimiento.html",
            {
                "user": user,
                "movimiento": movimiento,
                "cuentas": cuentas,
                "error": f"Ya existe otro comprobante con el numero {numero_operacion}.",
            },
            status_code=400,
        )

    movimiento.fecha_transaccion = nueva_fecha
    movimiento.monto = nuevo_monto
    movimiento.numero_operacion = numero_operacion
    movimiento.banco_emisor = banco_emisor or None
    movimiento.cuenta_bancaria_id = int(cuenta_bancaria_id) if cuenta_bancaria_id else None
    movimiento.titular = titular or None
    movimiento.factura_o_cuenta_tipo = TipoIdentificador(factura_o_cuenta_tipo) if factura_o_cuenta_tipo else None
    movimiento.factura_o_cuenta_numero = factura_o_cuenta_numero or None
    db.commit()
    return RedirectResponse(volver_a(request, "/comprobantes"), status_code=303)


def _eliminar_movimiento(db: Session, movimiento: Movement) -> None:
    # Si estaba emparejado con una linea de resumen, la linea vuelve a quedar
    # pendiente en vez de arrastrar una referencia rota a un movimiento borrado.
    lineas_vinculadas = db.scalars(select(StatementLine).where(StatementLine.movimiento_id == movimiento.id)).all()
    for linea in lineas_vinculadas:
        linea.movimiento_id = None
        linea.estado = StatementLineState.PENDIENTE
    db.delete(movimiento)


@router.post("/comprobantes/{movimiento_id}/eliminar")
def eliminar_movimiento(
    movimiento_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    movimiento = db.get(Movement, movimiento_id)
    if movimiento is not None:
        _eliminar_movimiento(db, movimiento)
        db.commit()
    return RedirectResponse(volver_a(request, "/comprobantes"), status_code=303)


@router.post("/comprobantes/eliminar-lote")
def eliminar_movimientos_lote(
    request: Request,
    movimiento_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    for movimiento_id in movimiento_ids:
        movimiento = db.get(Movement, movimiento_id)
        if movimiento is not None:
            _eliminar_movimiento(db, movimiento)
    db.commit()
    return RedirectResponse(volver_a(request, "/comprobantes"), status_code=303)


