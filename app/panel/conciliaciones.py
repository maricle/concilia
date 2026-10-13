"""Conciliaciones: importacion de resumenes bancarios, resolucion manual y cierre del dia."""

import contextlib
import logging
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..bancos import icono_banco
from ..models import (
    BankAccount,
    CierreDiario,
    ImportedStatement,
    Movement,
    Operator,
    PanelUser,
    ReconciliationState,
    RecordState,
    Reparto,
    RepartoOperador,
    StatementLine,
    StatementLineState,
)
from ..numeros import parse_monto_ar
from ..reconciliation import StatementParseError, match_statement, parse_statement_file
from ..storage import get_comprobante_archivo, save_comprobante_archivo
from ..zona_horaria import hoy_argentina
from .comun import get_db, require_admin, respuesta_xlsx, templates

router = APIRouter()
logger = logging.getLogger(__name__)

def _conteos_por_resumen(db: Session) -> dict[int, dict[str, int]]:
    """Cantidad de lineas por estado de conciliacion, agrupadas por resumen importado."""
    filas = db.execute(
        select(StatementLine.resumen_id, StatementLine.estado, Movement.estado_conciliacion).outerjoin(
            Movement, StatementLine.movimiento_id == Movement.id
        )
    ).all()
    conteos: dict[int, dict[str, int]] = {}
    for resumen_id, estado_linea, estado_conciliacion in filas:
        bucket = conteos.setdefault(resumen_id, {"conciliados": 0, "a_revisar": 0, "pendientes": 0, "no_corresponde": 0})
        if estado_linea == StatementLineState.PENDIENTE:
            bucket["pendientes"] += 1
        elif estado_linea == StatementLineState.NO_CORRESPONDE:
            bucket["no_corresponde"] += 1
        elif estado_conciliacion == ReconciliationState.CON_DIFERENCIA:
            bucket["a_revisar"] += 1
        else:
            bucket["conciliados"] += 1
    return conteos


def _eliminar_resumen(db: Session, resumen: ImportedStatement) -> None:
    """Elimina un resumen importado junto con sus lineas. Los movimientos que
    habian quedado conciliados (Conciliado, Con diferencia o Conciliado
    manualmente) a traves de una linea de este resumen vuelven a Pendiente -- no
    tiene sentido que sigan figurando como conciliados contra un resumen que ya
    no existe, mismo criterio que _eliminar_movimiento usa en la otra direccion."""
    lineas = db.scalars(select(StatementLine).where(StatementLine.resumen_id == resumen.id)).all()
    for linea in lineas:
        if linea.movimiento_id is not None:
            movimiento = db.get(Movement, linea.movimiento_id)
            if movimiento is not None:
                movimiento.estado_conciliacion = ReconciliationState.PENDIENTE
        db.delete(linea)
    db.delete(resumen)


@router.post("/conciliaciones/resumenes/{resumen_id}/eliminar")
def eliminar_resumen(
    resumen_id: int,
    request: Request,
    fecha: str = Form(""),
    banco: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    resumen = db.get(ImportedStatement, resumen_id)
    if resumen is not None and _cierre_del_dia(db, resumen.fecha.date()) is None:
        _eliminar_resumen(db, resumen)
        db.commit()
    if fecha or banco:
        return _volver_a_conciliaciones(fecha, banco)
    return RedirectResponse("/conciliaciones/resumenes", status_code=303)


def _parse_fecha_o_hoy(fecha: str) -> date:
    if fecha:
        try:
            return datetime.strptime(fecha, "%Y-%m-%d").date()
        except ValueError:
            pass
    return hoy_argentina()


def bancos_disponibles(db: Session) -> list[str]:
    return list(db.scalars(select(BankAccount.banco).distinct().order_by(BankAccount.banco)).all())


def _cuenta_ids_por_banco(db: Session, banco: str) -> list[int]:
    return list(db.scalars(select(BankAccount.id).where(BankAccount.banco == banco)).all())


def _banco_o_default(db: Session, banco: str, bancos: list[str]) -> str:
    if banco and banco in bancos:
        return banco
    return bancos[0] if bancos else ""


def _cierre_del_dia(db: Session, fecha: date) -> CierreDiario | None:
    return db.scalar(select(CierreDiario).where(CierreDiario.fecha == fecha))


def _dia_cerrado_error(db: Session, fecha: date) -> str | None:
    if _cierre_del_dia(db, fecha) is not None:
        return f"El dia {fecha.isoformat()} ya esta cerrado. Reabrilo desde 'Ver resumen del dia' para modificarlo."
    return None


def _salidas_por_banco_del_dia(db: Session, fecha: date) -> dict[str, int]:
    """Un query para toda la tira de pestañas: cantidad de salidas distintas con
    comprobantes ese dia, agrupadas por banco."""
    filas = db.execute(
        select(BankAccount.banco, func.count(func.distinct(Movement.reparto_id)))
        .select_from(Movement)
        .join(BankAccount, Movement.cuenta_bancaria_id == BankAccount.id)
        .where(Movement.estado_registro == RecordState.CONFIRMADO, func.date(Movement.fecha_transaccion) == fecha)
        .group_by(BankAccount.banco)
    ).all()
    return dict(filas)


def _sin_banco_identificado_del_dia(db: Session, fecha: date) -> tuple[int, Decimal]:
    cantidad, total = db.execute(
        select(func.count(Movement.id), func.coalesce(func.sum(Movement.monto), 0)).where(
            Movement.estado_registro == RecordState.CONFIRMADO,
            func.date(Movement.fecha_transaccion) == fecha,
            Movement.cuenta_bancaria_id.is_(None),
        )
    ).one()
    return cantidad or 0, total or Decimal("0")


def panel_banco(db: Session, fecha: date, banco: str) -> dict:
    """KPIs + estado del banco seleccionado para el dia seleccionado -- todo
    acotado a (fecha, banco), nunca al historico completo."""
    cuenta_ids = _cuenta_ids_por_banco(db, banco)
    filtro_movimientos_dia = (
        Movement.estado_registro == RecordState.CONFIRMADO,
        func.date(Movement.fecha_transaccion) == fecha,
        Movement.cuenta_bancaria_id.in_(cuenta_ids),
    )

    cantidad_comprobantes, total_declarado = db.execute(
        select(func.count(Movement.id), func.coalesce(func.sum(Movement.monto), 0)).where(*filtro_movimientos_dia)
    ).one()
    total_declarado = total_declarado or Decimal("0")

    cantidad_salidas = db.scalar(
        select(func.count(func.distinct(Movement.reparto_id))).where(*filtro_movimientos_dia)
    ) or 0

    resumenes = db.scalars(
        select(ImportedStatement).where(
            ImportedStatement.cuenta_bancaria_id.in_(cuenta_ids), func.date(ImportedStatement.fecha) == fecha
        )
    ).all()

    if not resumenes:
        return {
            "banco": banco,
            "icono": icono_banco(banco),
            "cantidad_comprobantes": cantidad_comprobantes or 0,
            "cantidad_salidas": cantidad_salidas,
            "total_declarado": total_declarado,
            "total_banco": None,
            "diferencia": None,
            "estado": "sin_resumen",
            "resumenes": [],
        }

    resumen_ids = [r.id for r in resumenes]
    total_banco = db.scalar(
        select(func.coalesce(func.sum(StatementLine.monto), 0)).where(
            StatementLine.resumen_id.in_(resumen_ids), StatementLine.estado == StatementLineState.CONCILIADA
        )
    ) or Decimal("0")
    diferencia = total_banco - total_declarado

    hay_pendientes = db.scalar(
        select(func.count(StatementLine.id)).where(
            StatementLine.resumen_id.in_(resumen_ids), StatementLine.estado == StatementLineState.PENDIENTE
        )
    ) or 0
    hay_con_diferencia = db.scalar(
        select(func.count(Movement.id)).where(
            *filtro_movimientos_dia, Movement.estado_conciliacion == ReconciliationState.CON_DIFERENCIA
        )
    ) or 0

    if hay_pendientes or hay_con_diferencia:
        estado = "a_revisar"
    elif diferencia != 0:
        estado = "error"
    else:
        estado = "conciliado"

    return {
        "banco": banco,
        "icono": icono_banco(banco),
        "cantidad_comprobantes": cantidad_comprobantes or 0,
        "cantidad_salidas": cantidad_salidas,
        "total_declarado": total_declarado,
        "total_banco": total_banco,
        "diferencia": diferencia,
        "estado": estado,
        "resumenes": resumenes,
    }


def _dia_puede_cerrarse(db: Session, fecha: date, bancos: list[str]) -> bool:
    return all(panel_banco(db, fecha, banco)["estado"] not in ("a_revisar", "error") for banco in bancos)


def _movimientos_del_dia_query(fecha: date, cuenta_ids: list[int], estado: str, search: str, operador_id: str, reparto_id: str):
    query = select(Movement).where(
        Movement.estado_registro == RecordState.CONFIRMADO,
        func.date(Movement.fecha_transaccion) == fecha,
        Movement.cuenta_bancaria_id.in_(cuenta_ids),
    )
    if estado:
        query = query.where(Movement.estado_conciliacion == ReconciliationState(estado))
    if operador_id:
        query = query.where(Movement.operador_id == int(operador_id))
    if reparto_id:
        query = query.where(Movement.reparto_id == int(reparto_id))
    if search:
        like = f"%{search}%"
        condiciones = [
            Movement.numero_operacion.ilike(like),
            Movement.titular.ilike(like),
            Movement.factura_o_cuenta_numero.ilike(like),
        ]
        with contextlib.suppress(ValueError):
            condiciones.append(Movement.monto == parse_monto_ar(search))
        query = query.where(or_(*condiciones))
    return query


def _movimiento_row_dict(movimiento: Movement) -> dict:
    reparto_label = "-"
    if movimiento.reparto is not None and movimiento.reparto.numero_reparto is not None:
        reparto_label = f"#{movimiento.reparto.numero_reparto} - {movimiento.operador.nombre}"
    return {
        "id": movimiento.id,
        "fecha_hora": movimiento.fecha_transaccion.strftime("%d/%m %H:%M") if movimiento.fecha_transaccion else "-",
        "comprobante": movimiento.factura_o_cuenta_numero or movimiento.numero_operacion or "-",
        "titular": movimiento.titular or "-",
        "monto": str(movimiento.monto) if movimiento.monto is not None else None,
        "reparto_id": movimiento.reparto_id,
        "reparto_label": reparto_label,
        "operador": movimiento.operador.nombre,
        "estado": movimiento.estado_conciliacion.value,
    }


def _salidas_del_dia(db: Session, fecha: date, banco: str) -> list[dict]:
    cuenta_ids = _cuenta_ids_por_banco(db, banco)
    filas = db.execute(
        select(
            Movement.reparto_id,
            func.count(Movement.id),
            func.coalesce(func.sum(Movement.monto), 0),
            func.sum(case((Movement.estado_conciliacion == ReconciliationState.CON_DIFERENCIA, 1), else_=0)),
            func.sum(case((Movement.estado_conciliacion == ReconciliationState.PENDIENTE, 1), else_=0)),
        )
        .where(
            Movement.estado_registro == RecordState.CONFIRMADO,
            func.date(Movement.fecha_transaccion) == fecha,
            Movement.cuenta_bancaria_id.in_(cuenta_ids),
            Movement.reparto_id.is_not(None),
        )
        .group_by(Movement.reparto_id)
    ).all()
    if not filas:
        return []

    reparto_ids = [fila[0] for fila in filas]
    repartos = {
        reparto.id: reparto
        for reparto in db.scalars(
            select(Reparto).where(Reparto.id.in_(reparto_ids)).options(selectinload(Reparto.movil))
        ).all()
    }
    operadores_por_reparto: dict[int, list[str]] = {}
    for reparto_id, nombre in db.execute(
        select(RepartoOperador.reparto_id, Operator.nombre)
        .join(Operator, Operator.id == RepartoOperador.operador_id)
        .where(RepartoOperador.reparto_id.in_(reparto_ids))
        .order_by(RepartoOperador.asociado_en)
    ).all():
        operadores_por_reparto.setdefault(reparto_id, []).append(nombre)

    resultado = []
    for reparto_id, cantidad, total, con_diferencia, pendientes in filas:
        reparto = repartos.get(reparto_id)
        nombres = operadores_por_reparto.get(reparto_id, [])
        operador_label = nombres[0] if nombres else "-"
        if len(nombres) > 1:
            operador_label += f" y {len(nombres) - 1} mas"
        estado = "a_revisar" if (con_diferencia or pendientes) else "conciliado"
        resultado.append(
            {
                "reparto_id": reparto_id,
                "numero_reparto": reparto.numero_reparto if reparto else None,
                "movil": reparto.movil.numero if reparto and reparto.movil else "-",
                "operador": operador_label,
                "cantidad_comprobantes": cantidad,
                "total": str(total),
                "estado": estado,
            }
        )
    resultado.sort(key=lambda fila: fila["numero_reparto"] or 0)
    return resultado


def _serializar_panel(panel: dict) -> dict:
    return {
        "banco": panel["banco"],
        "icono": panel["icono"],
        "cantidad_comprobantes": panel["cantidad_comprobantes"],
        "cantidad_salidas": panel["cantidad_salidas"],
        "total_declarado": str(panel["total_declarado"]),
        "total_banco": str(panel["total_banco"]) if panel["total_banco"] is not None else None,
        "diferencia": str(panel["diferencia"]) if panel["diferencia"] is not None else None,
        "estado": panel["estado"],
        "resumenes": [
            {"id": r.id, "archivo_nombre": r.archivo_nombre, "fecha_importacion": r.fecha_importacion.strftime("%Y-%m-%d %H:%M")}
            for r in panel["resumenes"]
        ],
    }


def _pantalla_conciliaciones(
    request: Request,
    db: Session,
    user: PanelUser,
    fecha: date,
    banco: str = "",
    *,
    error: str | None = None,
    mensaje: str | None = None,
):
    """Renderiza /conciliaciones para (fecha, banco). Con error responde 400."""
    return templates.TemplateResponse(
        request,
        "conciliaciones.html",
        _construir_contexto_conciliaciones(db, user, fecha, banco, error=error, mensaje=mensaje),
        status_code=400 if error else 200,
    )


def _volver_a_conciliaciones(fecha: str, banco: str, resumen_id: str = "") -> RedirectResponse:
    """Despues de una accion, vuelve a la vista desde la que se hizo: la de
    (fecha, banco), o la vieja por resumen_id, o la pantalla por defecto."""
    if fecha or banco:
        destino = f"/conciliaciones?{urlencode({'fecha': fecha, 'banco': banco})}"
    elif resumen_id:
        destino = f"/conciliaciones?{urlencode({'resumen_id': resumen_id})}"
    else:
        destino = "/conciliaciones"
    return RedirectResponse(destino, status_code=303)


def _construir_contexto_conciliaciones(
    db: Session, user: PanelUser, fecha: date, banco: str = "", error: str | None = None, mensaje: str | None = None
) -> dict:
    bancos = bancos_disponibles(db)
    banco = _banco_o_default(db, banco, bancos)

    panel = panel_banco(db, fecha, banco) if banco else None
    movimientos: list[Movement] = []
    total_movimientos = 0
    if banco:
        cuenta_ids = _cuenta_ids_por_banco(db, banco)
        query = _movimientos_del_dia_query(fecha, cuenta_ids, "", "", "", "")
        total_movimientos = db.scalar(select(func.count()).select_from(query.subquery())) or 0
        movimientos = db.scalars(
            query.options(selectinload(Movement.operador), selectinload(Movement.reparto))
            .order_by(Movement.fecha_transaccion.desc())
            .limit(25)
        ).all()
    salidas = _salidas_del_dia(db, fecha, banco) if banco else []
    sin_banco_cantidad, sin_banco_total = _sin_banco_identificado_del_dia(db, fecha)

    lineas_pendientes: list[StatementLine] = []
    if panel is not None and panel["resumenes"]:
        resumen_ids = [r.id for r in panel["resumenes"]]
        lineas_pendientes = db.scalars(
            select(StatementLine)
            .where(StatementLine.resumen_id.in_(resumen_ids), StatementLine.estado == StatementLineState.PENDIENTE)
            .order_by(StatementLine.fecha)
        ).all()

    # Antes mostraba TODO lo importado alguna vez, sin relacion con el banco/dia
    # que se esta mirando arriba -- panel["resumenes"] ya es exactamente esa
    # misma lista (mismo filtro que usa panel_banco para las lineas pendientes),
    # asi que no hace falta una query aparte.
    resumenes_historicos = sorted(
        panel["resumenes"] if panel is not None else [], key=lambda r: r.fecha_importacion, reverse=True
    )
    operadores = db.scalars(select(Operator).where(Operator.activo.is_(True)).order_by(Operator.nombre)).all()

    return {
        "user": user,
        "fecha": fecha,
        "fecha_anterior": (fecha - timedelta(days=1)).isoformat(),
        "fecha_siguiente": (fecha + timedelta(days=1)).isoformat(),
        "bancos": bancos,
        "banco_seleccionado": banco,
        "iconos_por_banco": {b: icono_banco(b) for b in bancos},
        "conteos_salidas_por_banco": _salidas_por_banco_del_dia(db, fecha),
        "panel": panel,
        "movimientos": [_movimiento_row_dict(m) for m in movimientos],
        "total_movimientos": total_movimientos,
        "salidas": salidas,
        "sin_banco_cantidad": sin_banco_cantidad,
        "sin_banco_total": sin_banco_total,
        "lineas_pendientes": lineas_pendientes,
        "cierre": _cierre_del_dia(db, fecha),
        "resumenes_historicos": resumenes_historicos,
        "conteos_por_resumen": _conteos_por_resumen(db),
        "cuentas": db.scalars(select(BankAccount).where(BankAccount.banco == banco).order_by(BankAccount.id)).all()
        if banco
        else [],
        "operadores": operadores,
        "error": error,
        "mensaje": mensaje,
    }


@router.get("/conciliaciones")
def conciliaciones(
    request: Request,
    fecha: str = "",
    banco: str = "",
    resumen_id: str = "",
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    if resumen_id:
        # Compat con links/bookmarks viejos que apuntaban a un resumen puntual --
        # se resuelve a la (fecha, banco) equivalente en la vista nueva.
        resumen = db.get(ImportedStatement, int(resumen_id))
        if resumen is None:
            return RedirectResponse("/conciliaciones", status_code=303)
        cuenta = db.get(BankAccount, resumen.cuenta_bancaria_id)
        destino = f"/conciliaciones?fecha={resumen.fecha.date().isoformat()}"
        if cuenta is not None:
            destino += f"&banco={cuenta.banco}"
        return RedirectResponse(destino, status_code=303)

    fecha_obj = _parse_fecha_o_hoy(fecha)
    return _pantalla_conciliaciones(request, db, user, fecha_obj, banco)


@router.get("/conciliaciones/panel.json")
def conciliaciones_panel_json(
    fecha: str = "", banco: str = "", db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    fecha_obj = _parse_fecha_o_hoy(fecha)
    bancos = bancos_disponibles(db)
    banco = _banco_o_default(db, banco, bancos)
    panel = panel_banco(db, fecha_obj, banco) if banco else None
    sin_banco_cantidad, sin_banco_total = _sin_banco_identificado_del_dia(db, fecha_obj)
    return {
        "fecha": fecha_obj.isoformat(),
        "banco": banco,
        "panel": _serializar_panel(panel) if panel else None,
        "cerrado": _cierre_del_dia(db, fecha_obj) is not None,
        "sin_banco_cantidad": sin_banco_cantidad,
        "sin_banco_total": str(sin_banco_total),
    }


@router.get("/conciliaciones/movimientos.json")
def conciliaciones_movimientos_json(
    fecha: str = "",
    banco: str = "",
    page: int = 1,
    page_size: int = 25,
    estado: str = "",
    search: str = "",
    operador_id: str = "",
    reparto_id: str = "",
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    fecha_obj = _parse_fecha_o_hoy(fecha)
    cuenta_ids = _cuenta_ids_por_banco(db, banco)
    query = _movimientos_del_dia_query(fecha_obj, cuenta_ids, estado, search, operador_id, reparto_id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0

    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    movimientos = db.scalars(
        query.options(selectinload(Movement.operador), selectinload(Movement.reparto))
        .order_by(Movement.fecha_transaccion.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()
    total_pages = max((total + page_size - 1) // page_size, 1)
    return {
        "items": [_movimiento_row_dict(m) for m in movimientos],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


@router.get("/conciliaciones/movimientos/exportar")
def conciliaciones_movimientos_exportar(
    fecha: str = "",
    banco: str = "",
    estado: str = "",
    search: str = "",
    operador_id: str = "",
    reparto_id: str = "",
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    fecha_obj = _parse_fecha_o_hoy(fecha)
    cuenta_ids = _cuenta_ids_por_banco(db, banco)
    query = _movimientos_del_dia_query(fecha_obj, cuenta_ids, estado, search, operador_id, reparto_id)
    movimientos = db.scalars(
        query.options(selectinload(Movement.operador), selectinload(Movement.reparto))
        .order_by(Movement.fecha_transaccion.desc())
    ).all()
    filas = [
        [
            movimiento.fecha_transaccion.strftime("%Y-%m-%d %H:%M") if movimiento.fecha_transaccion else "",
            movimiento.factura_o_cuenta_numero or movimiento.numero_operacion or "",
            movimiento.titular or "",
            movimiento.monto if movimiento.monto is not None else "",
            movimiento.reparto.numero_reparto if movimiento.reparto and movimiento.reparto.numero_reparto is not None else "",
            movimiento.operador.nombre,
            movimiento.estado_conciliacion.value,
        ]
        for movimiento in movimientos
    ]
    return respuesta_xlsx(
        ["Fecha / Hora", "Comprobante", "Titular", "Importe", "Salida", "Operador", "Estado"],
        filas,
        f"conciliaciones_{banco or 'todos'}_{fecha_obj.isoformat()}.xlsx",
    )


@router.get("/conciliaciones/salidas.json")
def conciliaciones_salidas_json(
    fecha: str = "",
    banco: str = "",
    page: int = 1,
    page_size: int = 25,
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    fecha_obj = _parse_fecha_o_hoy(fecha)
    salidas = _salidas_del_dia(db, fecha_obj, banco)
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    total = len(salidas)
    inicio = (page - 1) * page_size
    total_pages = max((total + page_size - 1) // page_size, 1)
    return {
        "items": salidas[inicio : inicio + page_size],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


@router.get("/conciliaciones/resumen-dia.json")
def conciliaciones_resumen_dia_json(
    fecha: str = "", db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    fecha_obj = _parse_fecha_o_hoy(fecha)
    bancos = bancos_disponibles(db)
    paneles = [_serializar_panel(panel_banco(db, fecha_obj, banco)) for banco in bancos]
    sin_banco_cantidad, sin_banco_total = _sin_banco_identificado_del_dia(db, fecha_obj)
    return {
        "fecha": fecha_obj.isoformat(),
        "bancos": paneles,
        "sin_banco_cantidad": sin_banco_cantidad,
        "sin_banco_total": str(sin_banco_total),
        "cerrado": _cierre_del_dia(db, fecha_obj) is not None,
        "puede_cerrarse": _dia_puede_cerrarse(db, fecha_obj, bancos),
    }


@router.post("/conciliaciones/cierres")
def cerrar_dia(
    request: Request, fecha: str = Form(...), db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    fecha_obj = _parse_fecha_o_hoy(fecha)
    bancos = bancos_disponibles(db)
    if not _dia_puede_cerrarse(db, fecha_obj, bancos):
        return _pantalla_conciliaciones(request, db, user, fecha_obj, error="No se puede cerrar el dia: hay bancos pendientes de revision.")
    if _cierre_del_dia(db, fecha_obj) is None:
        db.add(CierreDiario(fecha=fecha_obj, cerrado_por_id=user.id))
        db.commit()
    return RedirectResponse(f"/conciliaciones?fecha={fecha_obj.isoformat()}", status_code=303)


@router.post("/conciliaciones/cierres/{fecha}/reabrir")
def reabrir_dia(
    fecha: str, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    fecha_obj = _parse_fecha_o_hoy(fecha)
    cierre = _cierre_del_dia(db, fecha_obj)
    if cierre is not None:
        db.delete(cierre)
        db.commit()
    return RedirectResponse(f"/conciliaciones?fecha={fecha_obj.isoformat()}", status_code=303)


def _reconciliar_pendientes_de_resumenes(db: Session, resumenes: list[ImportedStatement]) -> int:
    """Reintenta el matching automatico sobre las lineas pendientes de cada resumen
    dado, salteando los dias cerrados. Devuelve cuantas lineas quedaron conciliadas.
    Compartido por el boton manual "Reintentar conciliacion" y por la carga de un
    resumen nuevo (que tambien reintenta las lineas pendientes viejas de esa misma
    cuenta, no solo las recien subidas)."""
    conciliadas = 0
    for resumen in resumenes:
        if _cierre_del_dia(db, resumen.fecha.date()) is not None:
            continue
        lineas_pendientes = db.scalars(
            select(StatementLine).where(
                StatementLine.resumen_id == resumen.id, StatementLine.estado == StatementLineState.PENDIENTE
            )
        ).all()
        if not lineas_pendientes:
            continue
        match_statement(db, resumen, lineas_pendientes)
        conciliadas += sum(1 for linea in lineas_pendientes if linea.estado == StatementLineState.CONCILIADA)
    return conciliadas


@router.post("/conciliaciones/reconciliar")
def reconciliar_pendientes(
    request: Request,
    fecha: str = Form(""),
    banco: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    """Reintenta el matching automatico sobre las lineas pendientes SIN pedir un
    archivo nuevo -- util cuando lo que cambio no fue el resumen del banco sino que
    se confirmaron comprobantes nuevos despues de la ultima carga, que antes no
    tenian con que emparejar. Se aplica sobre todos los resumenes del banco
    seleccionado (o de todos si no se paso banco), salteando los dias cerrados."""
    fecha_obj = _parse_fecha_o_hoy(fecha)
    if banco:
        cuenta_ids = _cuenta_ids_por_banco(db, banco)
        resumenes = db.scalars(select(ImportedStatement).where(ImportedStatement.cuenta_bancaria_id.in_(cuenta_ids))).all()
    else:
        resumenes = db.scalars(select(ImportedStatement)).all()

    conciliadas = _reconciliar_pendientes_de_resumenes(db, resumenes)
    db.commit()

    mensaje = (
        f"Se {'concilio' if conciliadas == 1 else 'conciliaron'} {conciliadas} "
        f"linea{'s' if conciliadas != 1 else ''} nueva{'s' if conciliadas != 1 else ''}."
        if conciliadas
        else "No se encontraron coincidencias nuevas."
    )
    return _pantalla_conciliaciones(request, db, user, fecha_obj, banco, mensaje=mensaje)


@router.post("/conciliaciones/importar")
async def importar_resumen(
    request: Request,
    archivo: UploadFile,
    cuenta_bancaria_id: int = Form(...),
    fecha: str = Form(...),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    fecha_obj = datetime.strptime(fecha, "%Y-%m-%d").date()
    cuenta = db.get(BankAccount, cuenta_bancaria_id)
    banco = cuenta.banco if cuenta is not None else ""

    error_cierre = _dia_cerrado_error(db, fecha_obj)
    if error_cierre:
        return _pantalla_conciliaciones(request, db, user, fecha_obj, banco, error=error_cierre)

    contenido = await archivo.read()
    try:
        filas, formato = parse_statement_file(archivo.filename or "", contenido)
    except StatementParseError as exc:
        return _pantalla_conciliaciones(request, db, user, fecha_obj, banco, error=str(exc))

    archivo_id = _guardar_archivo_resumen(archivo, contenido, formato, "resumen")

    resumen = ImportedStatement(
        cuenta_bancaria_id=cuenta_bancaria_id,
        fecha=datetime.strptime(fecha, "%Y-%m-%d"),
        archivo_nombre=archivo.filename or "resumen",
        formato=formato,
        usuario_id=user.id,
        archivo_id=archivo_id,
    )
    db.add(resumen)
    db.flush()

    # No duplicar lineas ya cargadas en OTRO resumen de esta misma cuenta (ej. el
    # mismo extracto subido dos veces sin querer, o un resumen nuevo que se
    # superpone en fechas con uno anterior) -- mismo criterio que ya usaba
    # actualizar_resumen para no duplicar dentro de un mismo resumen, extendido a
    # toda la cuenta.
    lineas_existentes = db.scalars(
        select(StatementLine)
        .join(ImportedStatement, StatementLine.resumen_id == ImportedStatement.id)
        .where(ImportedStatement.cuenta_bancaria_id == cuenta_bancaria_id, ImportedStatement.id != resumen.id)
    ).all()
    nuevas, duplicadas = _lineas_nuevas(resumen, filas, lineas_existentes)
    db.add_all(nuevas)
    if nuevas:
        match_statement(db, resumen, nuevas)

    # Ademas de las lineas recien subidas, reintentar tambien contra las lineas
    # pendientes de resumenes anteriores de esta misma cuenta -- un comprobante
    # que quedo sin emparejar la vez pasada puede calzar con algo de este archivo,
    # sin que el administrador tenga que apretar "Reintentar conciliacion" aparte.
    resumenes_cuenta = db.scalars(
        select(ImportedStatement).where(ImportedStatement.cuenta_bancaria_id == cuenta_bancaria_id)
    ).all()
    _reconciliar_pendientes_de_resumenes(db, resumenes_cuenta)

    db.commit()

    partes = [f"Se importaron {len(nuevas)} transaccion{'es' if len(nuevas) != 1 else ''} nueva{'s' if len(nuevas) != 1 else ''}."]
    if duplicadas:
        partes.append(f"Se omitieron {duplicadas} duplicada{'s' if duplicadas != 1 else ''} (ya estaban cargadas).")
    mensaje = " ".join(partes)

    return _pantalla_conciliaciones(request, db, user, fecha_obj, banco, mensaje=mensaje)


@router.post("/conciliaciones/resumenes/{resumen_id}/actualizar")
async def actualizar_resumen(
    resumen_id: int,
    request: Request,
    archivo: UploadFile,
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    """Vuelve a leer el mismo archivo de resumen (o una version mas nueva del banco,
    ej. un export acumulativo del dia al que se le siguieron agregando filas) y
    agrega solo las transacciones que todavia no estaban cargadas para este
    resumen -- no duplica las que ya se importaron ni toca sus lineas."""
    resumen = db.get(ImportedStatement, resumen_id)
    if resumen is None:
        return RedirectResponse("/conciliaciones", status_code=303)
    fecha_obj = resumen.fecha.date()
    cuenta = db.get(BankAccount, resumen.cuenta_bancaria_id)
    banco = cuenta.banco if cuenta is not None else ""

    error_cierre = _dia_cerrado_error(db, fecha_obj)
    if error_cierre:
        return _pantalla_conciliaciones(request, db, user, fecha_obj, banco, error=error_cierre)

    contenido = await archivo.read()
    try:
        filas, formato = parse_statement_file(archivo.filename or "", contenido)
    except StatementParseError as exc:
        return _pantalla_conciliaciones(request, db, user, fecha_obj, banco, error=str(exc))

    # El archivo re-subido reemplaza al guardado (puede ser una version mas nueva
    # y mas completa del mismo resumen) -- queda el ultimo, no se conservan los
    # anteriores.
    resumen.archivo_id = _guardar_archivo_resumen(archivo, contenido, formato, resumen.archivo_nombre)

    lineas_existentes = db.scalars(select(StatementLine).where(StatementLine.resumen_id == resumen.id)).all()
    nuevas, _ = _lineas_nuevas(resumen, filas, lineas_existentes)

    if nuevas:
        db.add_all(nuevas)
        match_statement(db, resumen, nuevas)
    db.commit()

    mensaje = (
        f"Se agregaron {len(nuevas)} transaccion{'es' if len(nuevas) != 1 else ''} nueva{'s' if len(nuevas) != 1 else ''} del archivo."
        if nuevas
        else "No se encontraron transacciones nuevas en el archivo: ya estaba todo cargado."
    )
    return _pantalla_conciliaciones(request, db, user, fecha_obj, banco, mensaje=mensaje)


_TIPO_POR_FORMATO = {"csv": "text/csv"}
_TIPO_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _guardar_archivo_resumen(archivo: UploadFile, contenido: bytes, formato: str, nombre_por_defecto: str) -> int:
    content_type = archivo.content_type or _TIPO_POR_FORMATO.get(formato, _TIPO_XLSX)
    return save_comprobante_archivo(archivo.filename or nombre_por_defecto, content_type, contenido)


def _lineas_nuevas(
    resumen: ImportedStatement, filas: list, lineas_existentes: list[StatementLine]
) -> tuple[list[StatementLine], int]:
    """Las filas del archivo que todavia no estaban cargadas, como lineas del
    resumen, y cuantas se omitieron por repetidas. Cada fila ya cargada
    "consume" una sola ocurrencia en vez de descartar todas las iguales: dos
    transferencias legitimas con mismo monto, fecha y referencia no se pierden."""
    restantes = Counter((linea.fecha, linea.monto, linea.referencia, linea.descripcion) for linea in lineas_existentes)
    nuevas: list[StatementLine] = []
    duplicadas = 0
    for fila in filas:
        clave = (fila.fecha, fila.monto, fila.referencia, fila.descripcion)
        if restantes[clave] > 0:
            restantes[clave] -= 1
            duplicadas += 1
            continue
        nuevas.append(
            StatementLine(
                resumen_id=resumen.id,
                resumen=resumen,
                fecha=fila.fecha,
                monto=fila.monto,
                descripcion=fila.descripcion,
                referencia=fila.referencia,
            )
        )
    return nuevas, duplicadas


def _resumenes_query(banco: str, fecha_desde: str, fecha_hasta: str, q: str):
    query = select(ImportedStatement).options(selectinload(ImportedStatement.cuenta_bancaria)).join(
        BankAccount, ImportedStatement.cuenta_bancaria_id == BankAccount.id
    )
    if banco:
        query = query.where(BankAccount.banco == banco)
    if fecha_desde:
        query = query.where(func.date(ImportedStatement.fecha) >= fecha_desde)
    if fecha_hasta:
        query = query.where(func.date(ImportedStatement.fecha) <= fecha_hasta)
    if q.strip():
        query = query.where(ImportedStatement.archivo_nombre.ilike(f"%{q.strip()}%"))
    return query.order_by(ImportedStatement.fecha_importacion.desc())


@router.get("/conciliaciones/resumenes")
def list_resumenes(
    request: Request,
    banco: str = "",
    fecha_desde: str = "",
    fecha_hasta: str = "",
    q: str = "",
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    resumenes = db.scalars(_resumenes_query(banco, fecha_desde, fecha_hasta, q)).all()
    return templates.TemplateResponse(
        request,
        "resumenes.html",
        {
            "user": user,
            "resumenes": resumenes,
            "conteos_por_resumen": _conteos_por_resumen(db),
            "bancos": bancos_disponibles(db),
            "banco": banco,
            "fecha_desde": fecha_desde,
            "fecha_hasta": fecha_hasta,
            "q": q,
        },
    )


@router.get("/conciliaciones/resumenes/{resumen_id}/descargar")
def descargar_resumen(
    resumen_id: int, db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    resumen = db.get(ImportedStatement, resumen_id)
    if resumen is None or resumen.archivo_id is None:
        return RedirectResponse("/conciliaciones/resumenes", status_code=303)
    archivo = get_comprobante_archivo(resumen.archivo_id)
    if archivo is None:
        return RedirectResponse("/conciliaciones/resumenes", status_code=303)
    return Response(
        content=archivo.contenido,
        media_type=archivo.content_type,
        headers={"Content-Disposition": f'attachment; filename="{archivo.nombre_archivo}"'},
    )


@router.post("/conciliaciones/lineas/{linea_id}/emparejar")
def emparejar_linea(
    linea_id: int,
    request: Request,
    movimiento_id: int = Form(...),
    resumen_id: str = Form(""),
    fecha: str = Form(""),
    banco: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    linea = db.get(StatementLine, linea_id)
    movimiento = db.get(Movement, movimiento_id)
    if linea is not None and movimiento is not None:
        fecha_obj = linea.fecha.date()
        if _cierre_del_dia(db, fecha_obj) is not None:
            return RedirectResponse(
                f"/conciliaciones?fecha={fecha or fecha_obj.isoformat()}&banco={banco}", status_code=303
            )
        # Un movimiento ya conciliado (con esta linea o con otra) no se puede
        # volver a emparejar a mano -- si no se chequea esto aca, elegir del
        # dropdown un movimiento que ya tenia otra linea vinculada lo reasigna
        # en silencio y deja a la linea original con una referencia obsoleta
        # (bug real, ver tambien _candidatos_iniciales en reconciliation.py).
        if movimiento.estado_conciliacion != ReconciliationState.PENDIENTE:
            return _pantalla_conciliaciones(request, db, user, fecha_obj, banco, error="Ese movimiento ya esta conciliado con otra linea.")
        linea.movimiento_id = movimiento.id
        linea.estado = StatementLineState.CONCILIADA
        movimiento.estado_conciliacion = ReconciliationState.CONCILIADO_MANUALMENTE
        if movimiento.cuenta_bancaria_id is None:
            movimiento.cuenta_bancaria_id = linea.resumen.cuenta_bancaria_id
        db.commit()
    return _volver_a_conciliaciones(fecha, banco, resumen_id)


@router.post("/conciliaciones/movimientos/{movimiento_id}/aceptar")
def aceptar_diferencia(
    movimiento_id: int,
    fecha: str = Form(""),
    banco: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    """El administrador reviso un movimiento "con diferencia" (emparejado con una
    linea del banco, pero con monto, fecha o emisor que no coinciden del todo) y
    confirma que el emparejamiento esta bien -- pasa a Conciliado manualmente.
    Sin esta accion no habia forma de sacarlo de "con diferencia": editar el
    comprobante no vuelve a correr el matching, y mientras quede uno asi el banco
    sigue A REVISAR y el dia no se puede cerrar."""
    movimiento = db.get(Movement, movimiento_id)
    if (
        movimiento is not None
        and movimiento.estado_conciliacion == ReconciliationState.CON_DIFERENCIA
        and _cierre_del_dia(db, movimiento.fecha_transaccion.date()) is None
    ):
        movimiento.estado_conciliacion = ReconciliationState.CONCILIADO_MANUALMENTE
        db.commit()
    return _volver_a_conciliaciones(fecha, banco)


@router.post("/conciliaciones/lineas/{linea_id}/no-corresponde")
def marcar_linea_no_corresponde(
    linea_id: int,
    request: Request,
    resumen_id: str = Form(""),
    fecha: str = Form(""),
    banco: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    linea = db.get(StatementLine, linea_id)
    if linea is not None and _cierre_del_dia(db, linea.fecha.date()) is None:
        linea.estado = StatementLineState.NO_CORRESPONDE
        db.commit()
    return _volver_a_conciliaciones(fecha, banco, resumen_id)
