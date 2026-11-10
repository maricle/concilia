"""Salidas de reparto: listado, exportacion, PDF de cierre y, para
administradores, crear, cerrar, reabrir y eliminar salidas."""

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
from ..servicios import (
    MovilConOtraSalida,
    SalidaEnOtroMovil,
    reparto_abierto_del_movil,
    reparto_abierto_del_operador,
    ubicar_salida,
)
from ..zona_horaria import ahora_argentina, hoy_argentina
from .comun import (
    con_parametros,
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

def _repartos_query(movil_id: str, fecha_desde: str, fecha_hasta: str, q: str):
    query = select(Reparto).options(
        selectinload(Reparto.movil),
        selectinload(Reparto.abierta_por_operador),
        selectinload(Reparto.abierta_por_usuario),
        selectinload(Reparto.reabierta_por_usuario),
    )
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


def _comprobantes_de_repartos(db: Session, repartos) -> tuple[dict[int, int], dict[int, Movement]]:
    """Cantidad de comprobantes y el ultimo comprobante (el mas reciente en
    subirse) de cada salida."""
    ids = [reparto.id for reparto in repartos]
    if not ids:
        return {}, {}
    conteos = dict(
        db.execute(
            select(Movement.reparto_id, func.count(Movement.id))
            .where(Movement.reparto_id.in_(ids))
            .group_by(Movement.reparto_id)
        ).all()
    )
    ultimas_subidas = (
        select(Movement.reparto_id, func.max(Movement.fecha_alta).label("subida"))
        .where(Movement.reparto_id.in_(ids))
        .group_by(Movement.reparto_id)
        .subquery()
    )
    ultimos: dict[int, Movement] = {}
    for movimiento in db.scalars(
        select(Movement)
        .join(
            ultimas_subidas,
            (Movement.reparto_id == ultimas_subidas.c.reparto_id) & (Movement.fecha_alta == ultimas_subidas.c.subida),
        )
        .options(selectinload(Movement.operador))
        .order_by(Movement.id)
    ):
        ultimos[movimiento.reparto_id] = movimiento
    return conteos, ultimos


def abierta_por(reparto: Reparto) -> str:
    if reparto.abierta_por_usuario is not None:
        return f"{reparto.abierta_por_usuario.nombre} (panel)"
    if reparto.abierta_por_operador is not None:
        return reparto.abierta_por_operador.nombre
    return "-"


def texto_ultimo_comprobante(movimiento: Movement | None) -> str:
    if movimiento is None:
        return ""
    texto = movimiento.fecha_alta.strftime("%Y-%m-%d %H:%M")
    if movimiento.monto is not None:
        monto = f"{movimiento.monto:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
        texto += f" - ${monto}"
    return texto


templates.env.globals["abierta_por"] = abierta_por


@router.get("/repartos")
def list_repartos(
    request: Request,
    movil_id: str = "",
    fecha_desde: str = "",
    fecha_hasta: str = "",
    q: str = "",
    error: str = "",
    mensaje: str = "",
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    repartos = db.scalars(_repartos_query(movil_id, fecha_desde, fecha_hasta, q)).all()
    conteos_comprobantes, ultimos_comprobantes = _comprobantes_de_repartos(db, repartos)
    moviles = db.scalars(select(Movil).order_by(Movil.nombre)).all()

    return templates.TemplateResponse(
        request,
        "repartos.html",
        {
            "user": user,
            "repartos": repartos,
            "conteos_comprobantes": conteos_comprobantes,
            "ultimos_comprobantes": ultimos_comprobantes,
            "moviles": moviles,
            "error": error or None,
            "mensaje": mensaje or None,
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
    conteos_comprobantes, ultimos_comprobantes = _comprobantes_de_repartos(db, repartos)
    filas = [
        [
            reparto.fecha.strftime("%Y-%m-%d"),
            reparto.movil.numero,
            reparto.movil.nombre,
            reparto.numero_reparto if reparto.numero_reparto is not None else "",
            reparto.hora_inicio.strftime("%H:%M"),
            reparto.hora_fin.strftime("%H:%M") if reparto.hora_fin else "",
            "Cerrada" if reparto.hora_fin else "Abierta",
            abierta_por(reparto),
            conteos_comprobantes.get(reparto.id, 0),
            texto_ultimo_comprobante(ultimos_comprobantes.get(reparto.id)),
        ]
        for reparto in repartos
    ]
    return respuesta_xlsx(
        [
            "Fecha",
            "Movil",
            "Nombre movil",
            "Nro. Salida",
            "Hora inicio",
            "Hora fin",
            "Estado",
            "Abierta por",
            "Comprobantes",
            "Ultimo comprobante",
        ],
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
        return RedirectResponse(volver_a(request, "/repartos"), status_code=303)

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
        return RedirectResponse(volver_a(request, "/repartos"), status_code=303)

    repartos = db.scalars(
        select(Reparto)
        .where(Reparto.id.in_(reparto_ids), Reparto.hora_fin.isnot(None))
        .options(selectinload(Reparto.movil))
        .order_by(Reparto.fecha, Reparto.movil_id, Reparto.hora_inicio)
    ).all()
    if not repartos:
        return RedirectResponse(volver_a(request, "/repartos"), status_code=303)

    items = [(reparto, *_movimientos_y_operadores_de_reparto(db, reparto.id)) for reparto in repartos]

    pdf_bytes = generar_resumen_salidas_pdf(items)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="resumen_salidas.pdf"'},
    )


# ---- Administracion de salidas desde el panel (solo administradores) ----
# Sin historial de cambios todavia (pendiente en el spec): quien abrio y quien
# reabrio queda en la propia salida.


def _volver_con(request: Request, **parametros: str) -> RedirectResponse:
    return RedirectResponse(con_parametros(volver_a(request, "/repartos"), **parametros), status_code=303)


@router.post("/repartos/nueva")
def crear_salida_panel(
    request: Request,
    movil_id: int = Form(...),
    numero_reparto: str = Form(...),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    """Abre una salida sin operadores: se suman despues con el numero, igual
    que a una salida que abrio un companero."""
    numero = numero_reparto.strip()
    if not numero.isdigit() or int(numero) <= 0:
        return _volver_con(request, error="El numero de salida tiene que ser un numero mayor a cero.")
    movil = db.get(Movil, movil_id)
    if movil is None or not movil.activo:
        return _volver_con(request, error="Elegí un movil activo.")

    resultado = ubicar_salida(db, int(numero), movil)
    if isinstance(resultado, Reparto):
        return _volver_con(request, error=f"La Salida Nº {numero} ya esta abierta en el movil {movil.numero}.")
    if isinstance(resultado, MovilConOtraSalida):
        error = f"El movil {movil.numero} ya tiene abierta la Salida Nº {resultado.reparto.numero_reparto}: cerrala primero."
        return _volver_con(request, error=error)
    if isinstance(resultado, SalidaEnOtroMovil):
        error = f"La Salida Nº {numero} ya esta abierta en el movil {resultado.reparto.movil.numero}."
        return _volver_con(request, error=error)

    db.add(
        Reparto(
            movil_id=movil.id,
            fecha=hoy_argentina(),
            hora_inicio=ahora_argentina(),
            numero_reparto=int(numero),
            abierta_por_usuario_id=user.id,
        )
    )
    db.commit()
    return _volver_con(request, mensaje=f"Salida Nº {numero} abierta en el movil {movil.numero}.")


@router.post("/repartos/{reparto_id}/cerrar")
def cerrar_salida_panel(
    reparto_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    """Cierra la salida para todos sus operadores. Como al cerrar desde la web
    del operador, no se avisa a nadie por chat."""
    reparto = obtener_o_redirigir(db, Reparto, reparto_id, "/repartos")
    if reparto.hora_fin is None:
        reparto.hora_fin = ahora_argentina()
        db.commit()
    return _volver_con(request, mensaje=f"Salida Nº {reparto.numero_reparto} cerrada.")


@router.post("/repartos/{reparto_id}/reabrir")
def reabrir_salida_panel(
    reparto_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    """Vuelve a abrir una salida cerrada, con los mismos operadores. No se puede
    si el movil ya tiene otra salida abierta, si hay otra abierta con el mismo
    numero, o si alguno de sus operadores ya esta en otra salida abierta (un
    operador nunca esta en dos salidas abiertas a la vez)."""
    reparto = obtener_o_redirigir(db, Reparto, reparto_id, "/repartos")
    if reparto.hora_fin is None:
        return _volver_con(request, error=f"La Salida Nº {reparto.numero_reparto} ya esta abierta.")

    en_el_movil = reparto_abierto_del_movil(db, reparto.movil_id)
    if en_el_movil is not None:
        error = f"El movil {reparto.movil.numero} ya tiene abierta la Salida Nº {en_el_movil.numero_reparto}."
        return _volver_con(request, error=error)
    mismo_numero = db.scalar(
        select(Reparto).where(Reparto.numero_reparto == reparto.numero_reparto, Reparto.hora_fin.is_(None))
    )
    if mismo_numero is not None:
        error = f"Ya hay otra Salida Nº {reparto.numero_reparto} abierta (movil {mismo_numero.movil.numero})."
        return _volver_con(request, error=error)
    ocupados = [
        operador.nombre
        for operador in db.scalars(
            select(Operator)
            .join(RepartoOperador, RepartoOperador.operador_id == Operator.id)
            .where(RepartoOperador.reparto_id == reparto.id)
            .order_by(Operator.nombre)
        )
        if reparto_abierto_del_operador(db, operador.id) is not None
    ]
    if ocupados:
        verbo = "estan" if len(ocupados) > 1 else "esta"
        return _volver_con(request, error=f"No se puede reabrir: {', '.join(ocupados)} {verbo} en otra salida abierta.")

    reparto.hora_fin = None
    reparto.reabierta_por_usuario_id = user.id
    reparto.reabierta_en = ahora_argentina()
    db.commit()
    return _volver_con(request, mensaje=f"Salida Nº {reparto.numero_reparto} reabierta.")


@router.post("/repartos/{reparto_id}/eliminar")
def eliminar_salida_panel(
    reparto_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    """Borra una salida cargada por error. Solo si no tiene comprobantes: son
    registros de plata y no se pierden por borrar la salida."""
    reparto = obtener_o_redirigir(db, Reparto, reparto_id, "/repartos")
    cantidad = db.scalar(select(func.count(Movement.id)).where(Movement.reparto_id == reparto.id))
    if cantidad:
        plural = "s" if cantidad != 1 else ""
        error = f"No se puede eliminar la Salida Nº {reparto.numero_reparto}: tiene {cantidad} comprobante{plural}."
        return _volver_con(request, error=error)
    numero = reparto.numero_reparto
    for asociado in db.scalars(select(RepartoOperador).where(RepartoOperador.reparto_id == reparto.id)):
        db.delete(asociado)
    db.delete(reparto)
    db.commit()
    logger.info("Salida %s (id %s) eliminada por el usuario de panel %s", numero, reparto_id, user.id)
    return _volver_con(request, mensaje=f"Salida Nº {numero} eliminada.")
