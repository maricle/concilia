"""Salidas de reparto: listado, exportacion y PDF de cierre."""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..models import (
    Movement,
    Movil,
    Operator,
    PanelUser,
    Reparto,
    RepartoOperador,
)
from ..reportes import generar_resumen_salida_pdf, generar_resumen_salidas_pdf
from .comun import get_db, require_user, respuesta_xlsx, templates

router = APIRouter()
logger = logging.getLogger(__name__)

def _repartos_query(movil_id: str, fecha_desde: str, fecha_hasta: str, q: str):
    query = select(Reparto).options(selectinload(Reparto.movil))
    if movil_id:
        query = query.where(Reparto.movil_id == int(movil_id))
    if fecha_desde:
        query = query.where(Reparto.fecha >= datetime.strptime(fecha_desde, "%Y-%m-%d").date())
    if fecha_hasta:
        query = query.where(Reparto.fecha <= datetime.strptime(fecha_hasta, "%Y-%m-%d").date())
    if q.strip():
        needle = q.strip()
        condiciones = [Movil.numero.ilike(f"%{needle}%"), Movil.nombre.ilike(f"%{needle}%")]
        if needle.isdigit():
            condiciones.append(Reparto.numero_reparto == int(needle))
        query = query.join(Movil, Reparto.movil_id == Movil.id).where(or_(*condiciones))
    return query.order_by(Reparto.fecha.desc(), Reparto.movil_id, Reparto.hora_inicio.desc())


@router.get("/repartos")
def list_repartos(
    request: Request,
    movil_id: str = "",
    fecha_desde: str = "",
    fecha_hasta: str = "",
    q: str = "",
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    repartos = db.scalars(_repartos_query(movil_id, fecha_desde, fecha_hasta, q)).all()

    conteos_comprobantes: dict[int, int] = {}
    if repartos:
        conteos_comprobantes = dict(
            db.execute(
                select(Movement.reparto_id, func.count(Movement.id))
                .where(Movement.reparto_id.in_([reparto.id for reparto in repartos]))
                .group_by(Movement.reparto_id)
            ).all()
        )

    moviles = db.scalars(select(Movil).order_by(Movil.nombre)).all()

    return templates.TemplateResponse(
        request,
        "repartos.html",
        {
            "user": user,
            "repartos": repartos,
            "conteos_comprobantes": conteos_comprobantes,
            "moviles": moviles,
            "movil_id": movil_id,
            "fecha_desde": fecha_desde,
            "fecha_hasta": fecha_hasta,
            "q": q,
        },
    )


@router.get("/repartos/exportar")
def repartos_exportar(
    movil_id: str = "",
    fecha_desde: str = "",
    fecha_hasta: str = "",
    q: str = "",
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    repartos = db.scalars(_repartos_query(movil_id, fecha_desde, fecha_hasta, q)).all()
    conteos_comprobantes: dict[int, int] = {}
    if repartos:
        conteos_comprobantes = dict(
            db.execute(
                select(Movement.reparto_id, func.count(Movement.id))
                .where(Movement.reparto_id.in_([reparto.id for reparto in repartos]))
                .group_by(Movement.reparto_id)
            ).all()
        )
    filas = [
        [
            reparto.fecha.strftime("%Y-%m-%d"),
            reparto.movil.numero,
            reparto.movil.nombre,
            reparto.numero_reparto if reparto.numero_reparto is not None else "",
            reparto.hora_inicio.strftime("%H:%M"),
            reparto.hora_fin.strftime("%H:%M") if reparto.hora_fin else "",
            "Cerrada" if reparto.hora_fin else "Abierta",
            conteos_comprobantes.get(reparto.id, 0),
        ]
        for reparto in repartos
    ]
    return respuesta_xlsx(
        ["Fecha", "Movil", "Nombre movil", "Nro. Salida", "Hora inicio", "Hora fin", "Estado", "Comprobantes"],
        filas,
        "salidas.xlsx",
    )


def _movimientos_y_operadores_de_reparto(db: Session, reparto_id: int) -> tuple[list[Movement], list[Operator]]:
    movimientos = db.scalars(
        select(Movement)
        .where(Movement.reparto_id == reparto_id)
        .options(selectinload(Movement.cuenta_bancaria), selectinload(Movement.operador))
        .order_by(Movement.fecha_transaccion)
    ).all()
    operadores = db.scalars(
        select(Operator)
        .join(RepartoOperador, RepartoOperador.operador_id == Operator.id)
        .where(RepartoOperador.reparto_id == reparto_id)
    ).all()
    return movimientos, operadores


@router.get("/repartos/{reparto_id}/pdf")
def descargar_resumen_reparto(
    reparto_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    reparto = db.get(Reparto, reparto_id)
    if reparto is None or reparto.hora_fin is None:
        return RedirectResponse("/repartos", status_code=303)

    movimientos, operadores = _movimientos_y_operadores_de_reparto(db, reparto_id)

    pdf_bytes = generar_resumen_salida_pdf(reparto, movimientos, operadores)
    numero = reparto.numero_reparto if reparto.numero_reparto is not None else "sin_numero"
    nombre_archivo = f"salida_{numero}_{reparto.movil.numero}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nombre_archivo}"'},
    )


@router.post("/repartos/pdf")
def descargar_resumen_repartos(
    request: Request,
    reparto_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    if not reparto_ids:
        return RedirectResponse("/repartos", status_code=303)

    repartos = db.scalars(
        select(Reparto)
        .where(Reparto.id.in_(reparto_ids), Reparto.hora_fin.isnot(None))
        .options(selectinload(Reparto.movil))
        .order_by(Reparto.fecha, Reparto.movil_id, Reparto.hora_inicio)
    ).all()
    if not repartos:
        return RedirectResponse("/repartos", status_code=303)

    items = [(reparto, *_movimientos_y_operadores_de_reparto(db, reparto.id)) for reparto in repartos]

    pdf_bytes = generar_resumen_salidas_pdf(items)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="resumen_salidas.pdf"'},
    )


