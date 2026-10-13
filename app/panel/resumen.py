"""Dashboard /resumen y su exportacion."""

import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..bancos import icono_banco
from ..models import (
    BankAccount,
    Movement,
    Operator,
    PanelUser,
    ReconciliationState,
    RecordState,
    Reparto,
    RepartoOperador,
)
from ..zona_horaria import hoy_argentina
from .comun import get_db, require_user, respuesta_xlsx, templates
from .conciliaciones import bancos_disponibles, panel_banco

router = APIRouter()
logger = logging.getLogger(__name__)

def _resumen_query(vendedor: str, fecha_desde: str, fecha_hasta: str, cuenta_bancaria_id: int | None):
    query = (
        select(Movement)
        .join(Operator)
        .where(Movement.estado_registro == RecordState.CONFIRMADO)
        .options(selectinload(Movement.operador))
    )
    if vendedor.strip():
        query = query.where(Operator.nombre.ilike(f"%{vendedor.strip()}%"))
    if fecha_desde:
        query = query.where(Movement.fecha_transaccion >= datetime.strptime(fecha_desde, "%Y-%m-%d"))
    if fecha_hasta:
        query = query.where(Movement.fecha_transaccion < datetime.strptime(fecha_hasta, "%Y-%m-%d") + timedelta(days=1))
    if cuenta_bancaria_id is not None:
        query = query.where(Movement.cuenta_bancaria_id == cuenta_bancaria_id)
    return query


def _resumen_por_operador(movimientos: list[Movement]) -> list[dict]:
    por_operador: dict[int, dict] = {}
    for movimiento in movimientos:
        fila = por_operador.setdefault(
            movimiento.operador_id,
            {
                "operador": movimiento.operador,
                "total": Decimal("0"),
                "cantidad": 0,
                "conciliados": 0,
                "pendientes": 0,
                "diferencia": 0,
            },
        )
        fila["cantidad"] += 1
        if movimiento.monto is not None:
            fila["total"] += movimiento.monto
        if movimiento.estado_conciliacion in (ReconciliationState.CONCILIADO, ReconciliationState.CONCILIADO_MANUALMENTE):
            fila["conciliados"] += 1
        elif movimiento.estado_conciliacion == ReconciliationState.CON_DIFERENCIA:
            fila["diferencia"] += 1
        else:
            fila["pendientes"] += 1
    filas = sorted(por_operador.values(), key=lambda f: f["operador"].nombre)
    for fila in filas:
        fila["pct_conciliado"] = round(fila["conciliados"] / fila["cantidad"] * 100) if fila["cantidad"] else 0
    return filas


def _conteo_conciliacion(movimientos: list[Movement]) -> dict[str, int]:
    conteo = {"conciliados": 0, "pendientes": 0, "diferencia": 0}
    for movimiento in movimientos:
        if movimiento.estado_conciliacion in (ReconciliationState.CONCILIADO, ReconciliationState.CONCILIADO_MANUALMENTE):
            conteo["conciliados"] += 1
        elif movimiento.estado_conciliacion == ReconciliationState.CON_DIFERENCIA:
            conteo["diferencia"] += 1
        else:
            conteo["pendientes"] += 1
    return conteo


def _actividad_ultimos_dias(db: Session, dias: int = 14) -> list[dict]:
    """Cantidad de comprobantes confirmados por dia (segun fecha_transaccion), para
    el sparkline de actividad. Es independiente de los filtros del formulario --
    siempre muestra el pulso general de los ultimos N dias, no el resultado filtrado."""
    hoy = hoy_argentina()
    desde = hoy - timedelta(days=dias - 1)
    movimientos = db.scalars(
        select(Movement).where(
            Movement.estado_registro == RecordState.CONFIRMADO,
            Movement.fecha_transaccion >= datetime(desde.year, desde.month, desde.day),
        )
    ).all()
    conteo_por_dia: dict = {}
    for movimiento in movimientos:
        if movimiento.fecha_transaccion is None:
            continue
        dia = movimiento.fecha_transaccion.date()
        conteo_por_dia[dia] = conteo_por_dia.get(dia, 0) + 1
    return [
        {"fecha": (desde + timedelta(days=i)).strftime("%d/%m"), "cantidad": conteo_por_dia.get(desde + timedelta(days=i), 0)}
        for i in range(dias)
    ]


def _sparkline_svg(dias_data: list[dict], ancho: int = 1180, alto: int = 96, pad: int = 4) -> dict:
    """Calcula los puntos del sparkline server-side (sin JS en el cliente): una
    polilinea simple mas el area rellena debajo, escaladas al maximo del periodo."""
    cantidades = [d["cantidad"] for d in dias_data]
    maximo = max(cantidades) if cantidades and max(cantidades) > 0 else 1
    paso_x = (ancho - pad * 2) / (len(dias_data) - 1) if len(dias_data) > 1 else 0

    def y_para(valor: int) -> float:
        return alto - pad - (valor / maximo) * (alto - pad * 2 - 14)

    puntos = [(pad + i * paso_x, y_para(c)) for i, c in enumerate(cantidades)]
    line_path = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in puntos)
    area_path = line_path + f" L {puntos[-1][0]:.1f},{alto} L {puntos[0][0]:.1f},{alto} Z"
    return {
        "ancho": ancho,
        "alto": alto,
        "line_path": line_path,
        "area_path": area_path,
        "dot_x": puntos[-1][0],
        "dot_y": puntos[-1][1],
    }


def _ultimos_movimientos(db: Session, limite: int = 8) -> list[Movement]:
    return db.scalars(
        select(Movement)
        .where(Movement.estado_registro == RecordState.CONFIRMADO)
        .options(selectinload(Movement.operador))
        .order_by(Movement.fecha_subida.desc())
        .limit(limite)
    ).all()


def _bancos_del_dia(db: Session) -> list[dict]:
    """Un panel por banco (mismo shape que panel_banco, de /conciliaciones) para
    cada banco con al menos un comprobante hoy -- usado en el home."""
    hoy = hoy_argentina()
    resultado = [
        panel
        for banco in bancos_disponibles(db)
        if (panel := panel_banco(db, hoy, banco))["cantidad_comprobantes"] > 0
    ]
    resultado.sort(key=lambda p: p["cantidad_comprobantes"], reverse=True)
    return resultado


def _kpis_hoy(db: Session, bancos_del_dia: list[dict]) -> dict:
    hoy = hoy_argentina()
    ayer = hoy - timedelta(days=1)

    def _conteo_y_monto(dia: date) -> tuple[int, Decimal]:
        cantidad, monto = db.execute(
            select(func.count(Movement.id), func.coalesce(func.sum(Movement.monto), 0)).where(
                Movement.estado_registro == RecordState.CONFIRMADO,
                func.date(Movement.fecha_transaccion) == dia,
            )
        ).one()
        return cantidad or 0, monto or Decimal("0")

    def _variacion(valor_hoy, valor_ayer) -> str | None:
        if not valor_ayer:
            return None
        cambio = (valor_hoy - valor_ayer) / valor_ayer * 100
        signo = "+" if cambio >= 0 else ""
        return f"{signo}{cambio:.0f}% vs ayer"

    comprobantes_hoy, monto_hoy = _conteo_y_monto(hoy)
    comprobantes_ayer, monto_ayer = _conteo_y_monto(ayer)

    salidas_activas = db.scalar(select(func.count(Reparto.id)).where(Reparto.hora_fin.is_(None))) or 0
    operadores_en_salida = db.scalar(
        select(func.count(func.distinct(RepartoOperador.operador_id)))
        .join(Reparto, RepartoOperador.reparto_id == Reparto.id)
        .where(Reparto.hora_fin.is_(None))
    ) or 0

    return {
        "comprobantes_hoy": comprobantes_hoy,
        "comprobantes_hoy_variacion": _variacion(comprobantes_hoy, comprobantes_ayer),
        "monto_hoy": monto_hoy,
        "monto_hoy_variacion": _variacion(monto_hoy, monto_ayer),
        "salidas_activas": salidas_activas,
        "operadores_en_salida": operadores_en_salida,
        "bancos_en_uso": len(bancos_del_dia),
        "bancos_conciliados": sum(1 for b in bancos_del_dia if b["estado"] == "conciliado"),
    }


def _comprobantes_y_montos_ultimos_dias(db: Session, dias: int = 7) -> list[dict]:
    """Mismo patron que _actividad_ultimos_dias: trae los movimientos del rango y
    agrupa en Python (no con func.date() agrupado en SQL, que devuelve tipos
    distintos en SQLite vs Postgres)."""
    hoy = hoy_argentina()
    desde = hoy - timedelta(days=dias - 1)
    movimientos = db.scalars(
        select(Movement).where(
            Movement.estado_registro == RecordState.CONFIRMADO,
            Movement.fecha_transaccion >= datetime(desde.year, desde.month, desde.day),
        )
    ).all()
    por_dia: dict[date, dict] = {}
    for movimiento in movimientos:
        if movimiento.fecha_transaccion is None:
            continue
        dia = movimiento.fecha_transaccion.date()
        bucket = por_dia.setdefault(dia, {"cantidad": 0, "monto": Decimal("0")})
        bucket["cantidad"] += 1
        if movimiento.monto is not None:
            bucket["monto"] += movimiento.monto
    return [
        {
            "fecha": (desde + timedelta(days=i)).strftime("%d/%m"),
            "cantidad": por_dia.get(desde + timedelta(days=i), {}).get("cantidad", 0),
            "monto": float(por_dia.get(desde + timedelta(days=i), {}).get("monto", Decimal("0"))),
        }
        for i in range(dias)
    ]


def _comprobantes_por_banco_hoy(db: Session) -> list[dict]:
    hoy = hoy_argentina()
    filas = db.execute(
        select(BankAccount.banco, func.count(Movement.id))
        .select_from(Movement)
        .outerjoin(BankAccount, Movement.cuenta_bancaria_id == BankAccount.id)
        .where(Movement.estado_registro == RecordState.CONFIRMADO, func.date(Movement.fecha_transaccion) == hoy)
        .group_by(BankAccount.banco)
    ).all()
    total = sum(cantidad for _, cantidad in filas) or 1
    resultado = [
        {
            "banco": banco or "Sin banco",
            "cantidad": cantidad,
            "pct": round(cantidad / total * 100, 1),
            "color": icono_banco(banco)["color"] if banco else "#9395A8",
        }
        for banco, cantidad in filas
    ]
    resultado.sort(key=lambda f: f["cantidad"], reverse=True)
    return resultado


@router.get("/resumen")
def resumen(
    request: Request,
    vendedor: str = "",
    fecha_desde: str = "",
    fecha_hasta: str = "",
    cuenta_bancaria_id: str = "",
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    if not fecha_desde and not fecha_hasta:
        # Sin filtro de fecha explicito, el Resumen arranca mostrando el mes en
        # curso (no todo el historico) -- fecha_hasta queda abierta para que
        # incluya lo que se cargue el resto del mes sin tener que recalcularla.
        fecha_desde = hoy_argentina().replace(day=1).strftime("%Y-%m-%d")

    cuenta_id = int(cuenta_bancaria_id) if cuenta_bancaria_id else None
    movimientos = db.scalars(_resumen_query(vendedor, fecha_desde, fecha_hasta, cuenta_id)).all()
    filas = _resumen_por_operador(movimientos)
    cuentas = db.scalars(select(BankAccount).order_by(BankAccount.id)).all()
    conteo = _conteo_conciliacion(movimientos)

    bancos_del_dia = _bancos_del_dia(db)
    comprobantes_y_montos = _comprobantes_y_montos_ultimos_dias(db)
    comprobantes_por_banco_hoy = _comprobantes_por_banco_hoy(db)
    resumen_data_json = json.dumps({"dias": comprobantes_y_montos, "porBanco": comprobantes_por_banco_hoy})

    return templates.TemplateResponse(
        request,
        "resumen.html",
        {
            "user": user,
            "filas": filas,
            "cantidad_vendedores": len(filas),
            "monto_total": sum((f["total"] for f in filas), Decimal("0")),
            "cantidad_comprobantes": len(movimientos),
            "conciliados": conteo["conciliados"],
            "pendientes": conteo["pendientes"],
            "diferencia": conteo["diferencia"],
            "vendedor": vendedor,
            "fecha_desde": fecha_desde,
            "fecha_hasta": fecha_hasta,
            "cuenta_bancaria_id": cuenta_bancaria_id,
            "cuentas": cuentas,
            "actividad_dias": (actividad_dias := _actividad_ultimos_dias(db)),
            "sparkline": _sparkline_svg(actividad_dias),
            "ultimos_movimientos": _ultimos_movimientos(db),
            "hoy": hoy_argentina(),
            "kpis_hoy": _kpis_hoy(db, bancos_del_dia),
            "bancos_del_dia": bancos_del_dia,
            "comprobantes_por_banco_hoy": comprobantes_por_banco_hoy,
            "resumen_data_json": resumen_data_json,
        },
    )


@router.get("/resumen/exportar")
def resumen_exportar(
    vendedor: str = "",
    fecha_desde: str = "",
    fecha_hasta: str = "",
    cuenta_bancaria_id: str = "",
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    cuenta_id = int(cuenta_bancaria_id) if cuenta_bancaria_id else None
    movimientos = db.scalars(_resumen_query(vendedor, fecha_desde, fecha_hasta, cuenta_id)).all()
    filas_operador = _resumen_por_operador(movimientos)

    filas = [
        [
            fila["operador"].nombre,
            fila["operador"].whatsapp_numero,
            fila["operador"].tipo,
            fila["total"],
            fila["cantidad"],
            fila["conciliados"],
            fila["pendientes"],
            fila["diferencia"],
        ]
        for fila in filas_operador
    ]
    return respuesta_xlsx(
        ["Vendedor", "Telefono", "Tipo", "Total", "Comprobantes", "Conciliados con el banco", "Pendientes de conciliar", "Con diferencia"],
        filas,
        "resumen.xlsx",
    )


