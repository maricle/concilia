import csv
import io
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TypeVar

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import DeclarativeBase, InstrumentedAttribute, Session, selectinload

from .auth import hash_password, verify_password
from .db import SessionLocal
from .models import (
    BankAccount,
    ImportedStatement,
    Movement,
    Operator,
    PanelUser,
    ReconciliationState,
    RecordState,
    StatementLine,
    StatementLineState,
    TipoIdentificador,
)
from .reconciliation import StatementParseError, match_statement, parse_statement_file
from .storage import get_comprobante_archivo

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _fecha_transaccion_display(fecha: datetime | None) -> str:
    """La hora solo se muestra si se detecto (fecha.hour/minute != 0): si el
    comprobante no traia hora, fecha_transaccion queda en medianoche y mostrarla
    daria a entender que la hora se leyo del comprobante cuando en realidad no."""
    if fecha is None:
        return "-"
    if fecha.hour or fecha.minute:
        return fecha.strftime("%Y-%m-%d %H:%M")
    return fecha.strftime("%Y-%m-%d")


templates.env.filters["fecha_transaccion"] = _fecha_transaccion_display

_Model = TypeVar("_Model", bound=DeclarativeBase)


class NotAuthenticated(Exception):
    """Se levanta cuando una ruta protegida no tiene sesion de panel activa."""


class RedirectOnMissing(Exception):
    """Se levanta cuando un registro buscado por id no existe."""

    def __init__(self, redirect_to: str):
        self.redirect_to = redirect_to


def get_db():
    with SessionLocal() as session:
        yield session


def get_logged_in_user(request: Request, db: Session) -> PanelUser | None:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    user = db.get(PanelUser, user_id)
    return user if user is not None and user.activo else None


def require_user(request: Request, db: Session = Depends(get_db)) -> PanelUser:
    user = get_logged_in_user(request, db)
    if user is None:
        raise NotAuthenticated()
    return user


def _get_or_redirect(db: Session, model: type[_Model], id_: int, redirect_to: str) -> _Model:
    obj = db.get(model, id_)
    if obj is None:
        raise RedirectOnMissing(redirect_to)
    return obj


def _duplicate_exists(db: Session, field: InstrumentedAttribute, value: str, *, exclude_id: int | None = None) -> bool:
    query = select(field.class_).where(field == value)
    if exclude_id is not None:
        query = query.where(field.class_.id != exclude_id)
    return db.scalar(query) is not None


@router.get("/")
def raiz():
    return RedirectResponse("/login", status_code=303)


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_db)):
    if get_logged_in_user(request, db) is not None:
        return RedirectResponse("/resumen", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"user": None, "error": None})


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.scalar(select(PanelUser).where(PanelUser.email == email, PanelUser.activo.is_(True)))
    if user is None or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"user": None, "error": "Email o contrasena incorrectos."},
            status_code=401,
        )
    request.session["user_id"] = user.id
    return RedirectResponse("/resumen", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/config/usuarios")
def list_usuarios(request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)):
    usuarios = db.scalars(select(PanelUser).order_by(PanelUser.id)).all()
    return templates.TemplateResponse(request, "usuarios.html", {"user": user, "usuarios": usuarios, "error": None})


@router.post("/config/usuarios")
def create_usuario(
    request: Request,
    nombre: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    rol: str = Form("Administrador"),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    error = None
    if _duplicate_exists(db, PanelUser.email, email):
        error = f"Ya existe un usuario con el email {email}."
    elif len(password) < 8:
        error = "La contrasena tiene que tener al menos 8 caracteres."

    if error:
        usuarios = db.scalars(select(PanelUser).order_by(PanelUser.id)).all()
        return templates.TemplateResponse(
            request, "usuarios.html", {"user": user, "usuarios": usuarios, "error": error}, status_code=400
        )

    db.add(PanelUser(nombre=nombre, email=email, password_hash=hash_password(password), rol=rol))
    db.commit()
    return RedirectResponse("/config/usuarios", status_code=303)


@router.post("/config/usuarios/{usuario_id}/toggle")
def toggle_usuario(
    usuario_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    usuario = db.get(PanelUser, usuario_id)
    if usuario is not None and usuario.id != user.id:
        # No se permite que un usuario se desactive a si mismo, para evitar que
        # el panel quede sin nadie que pueda volver a activar cuentas.
        usuario.activo = not usuario.activo
        db.commit()
    return RedirectResponse("/config/usuarios", status_code=303)


@router.get("/config/usuarios/{usuario_id}/editar")
def editar_usuario_form(
    usuario_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    usuario = _get_or_redirect(db, PanelUser, usuario_id, "/config/usuarios")
    return templates.TemplateResponse(
        request, "editar_usuario.html", {"user": user, "usuario": usuario, "error": None}
    )


@router.post("/config/usuarios/{usuario_id}/editar")
def editar_usuario_submit(
    usuario_id: int,
    request: Request,
    nombre: str = Form(...),
    email: str = Form(...),
    rol: str = Form("Administrador"),
    password: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    usuario = _get_or_redirect(db, PanelUser, usuario_id, "/config/usuarios")

    error = None
    if _duplicate_exists(db, PanelUser.email, email, exclude_id=usuario.id):
        error = f"Ya existe otro usuario con el email {email}."
    elif password and len(password) < 8:
        error = "La contrasena tiene que tener al menos 8 caracteres."

    if error:
        return templates.TemplateResponse(
            request, "editar_usuario.html", {"user": user, "usuario": usuario, "error": error}, status_code=400
        )

    usuario.nombre = nombre
    usuario.email = email
    usuario.rol = rol
    if password:
        usuario.password_hash = hash_password(password)
    db.commit()
    return RedirectResponse("/config/usuarios", status_code=303)


@router.get("/config/operadores")
def list_operadores(request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)):
    operadores = db.scalars(select(Operator).order_by(Operator.id)).all()
    return templates.TemplateResponse(
        request, "operadores.html", {"user": user, "operadores": operadores, "error": None}
    )


@router.post("/config/operadores")
def create_operador(
    request: Request,
    nombre: str = Form(...),
    whatsapp_numero: str = Form(...),
    tipo: str = Form("Reparto"),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    if _duplicate_exists(db, Operator.whatsapp_numero, whatsapp_numero):
        operadores = db.scalars(select(Operator).order_by(Operator.id)).all()
        return templates.TemplateResponse(
            request,
            "operadores.html",
            {
                "user": user,
                "operadores": operadores,
                "error": f"Ya existe un operador con el numero {whatsapp_numero}.",
            },
            status_code=400,
        )

    db.add(Operator(nombre=nombre, whatsapp_numero=whatsapp_numero, tipo=tipo))
    db.commit()
    return RedirectResponse("/config/operadores", status_code=303)


@router.post("/config/operadores/{operador_id}/toggle")
def toggle_operador(
    operador_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    operador = db.get(Operator, operador_id)
    if operador is not None:
        operador.activo = not operador.activo
        db.commit()
    return RedirectResponse("/config/operadores", status_code=303)


@router.get("/config/operadores/{operador_id}/editar")
def editar_operador_form(
    operador_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    operador = _get_or_redirect(db, Operator, operador_id, "/config/operadores")
    return templates.TemplateResponse(
        request, "editar_operador.html", {"user": user, "operador": operador, "error": None}
    )


@router.post("/config/operadores/{operador_id}/editar")
def editar_operador_submit(
    operador_id: int,
    request: Request,
    nombre: str = Form(...),
    whatsapp_numero: str = Form(...),
    tipo: str = Form("Reparto"),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    operador = _get_or_redirect(db, Operator, operador_id, "/config/operadores")

    if _duplicate_exists(db, Operator.whatsapp_numero, whatsapp_numero, exclude_id=operador.id):
        return templates.TemplateResponse(
            request,
            "editar_operador.html",
            {"user": user, "operador": operador, "error": f"Ya existe otro operador con el numero {whatsapp_numero}."},
            status_code=400,
        )

    operador.nombre = nombre
    operador.whatsapp_numero = whatsapp_numero
    operador.tipo = tipo
    db.commit()
    return RedirectResponse("/config/operadores", status_code=303)


@router.get("/config/cuentas")
def list_cuentas(request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)):
    cuentas = db.scalars(select(BankAccount).order_by(BankAccount.id)).all()
    return templates.TemplateResponse(request, "cuentas.html", {"user": user, "cuentas": cuentas, "error": None})


@router.post("/config/cuentas")
def create_cuenta(
    request: Request,
    banco: str = Form(...),
    numero_cuenta: str = Form(...),
    alias: str = Form(...),
    moneda: str = Form("ARS"),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    db.add(BankAccount(banco=banco, numero_cuenta=numero_cuenta, alias=alias, moneda=moneda))
    db.commit()
    return RedirectResponse("/config/cuentas", status_code=303)


@router.get("/config/cuentas/{cuenta_id}/editar")
def editar_cuenta_form(
    cuenta_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    cuenta = _get_or_redirect(db, BankAccount, cuenta_id, "/config/cuentas")
    return templates.TemplateResponse(request, "editar_cuenta.html", {"user": user, "cuenta": cuenta, "error": None})


@router.post("/config/cuentas/{cuenta_id}/editar")
def editar_cuenta_submit(
    cuenta_id: int,
    request: Request,
    banco: str = Form(...),
    numero_cuenta: str = Form(...),
    alias: str = Form(...),
    moneda: str = Form("ARS"),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    cuenta = _get_or_redirect(db, BankAccount, cuenta_id, "/config/cuentas")

    cuenta.banco = banco
    cuenta.numero_cuenta = numero_cuenta
    cuenta.alias = alias
    cuenta.moneda = moneda
    db.commit()
    return RedirectResponse("/config/cuentas", status_code=303)


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
    hoy = datetime.utcnow().date()
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
    cuenta_id = int(cuenta_bancaria_id) if cuenta_bancaria_id else None
    movimientos = db.scalars(_resumen_query(vendedor, fecha_desde, fecha_hasta, cuenta_id)).all()
    filas = _resumen_por_operador(movimientos)
    cuentas = db.scalars(select(BankAccount).order_by(BankAccount.id)).all()
    conteo = _conteo_conciliacion(movimientos)

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
    filas = _resumen_por_operador(movimientos)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["Vendedor", "Telefono", "Tipo", "Total", "Comprobantes", "Conciliados con el banco", "Pendientes de conciliar", "Con diferencia"]
    )
    for fila in filas:
        writer.writerow(
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
        )

    return Response(
        content=("﻿" + buffer.getvalue()).encode("utf-8"),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="resumen.csv"'},
    )


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
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    query = (
        select(Movement)
        .where(Movement.estado_registro == RecordState.CONFIRMADO)
        .options(selectinload(Movement.operador), selectinload(Movement.cuenta_bancaria))
    )
    if banco:
        query = query.where(Movement.cuenta_bancaria_id == int(banco))
    if estado_conciliacion:
        query = query.where(Movement.estado_conciliacion == ReconciliationState(estado_conciliacion))
    if operador_id:
        query = query.where(Movement.operador_id == int(operador_id))
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
    movimientos = db.scalars(query.order_by(Movement.fecha_subida.desc())).all()
    cuentas = db.scalars(select(BankAccount).order_by(BankAccount.id)).all()
    operadores = db.scalars(select(Operator).order_by(Operator.nombre)).all()

    return templates.TemplateResponse(
        request,
        "movimientos.html",
        {
            "user": user,
            "movimientos": movimientos,
            "cuentas": cuentas,
            "operadores": operadores,
            "banco": banco,
            "estado_conciliacion": estado_conciliacion,
            "operador_id": operador_id,
            "fecha_transaccion_desde": fecha_transaccion_desde,
            "fecha_transaccion_hasta": fecha_transaccion_hasta,
            "fecha_subida_desde": fecha_subida_desde,
            "fecha_subida_hasta": fecha_subida_hasta,
            "q": q,
        },
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
        return RedirectResponse("/comprobantes", status_code=303)
    if archivo is None:
        return RedirectResponse("/comprobantes", status_code=303)

    return Response(
        content=archivo.contenido,
        media_type=archivo.content_type,
        headers={"Content-Disposition": f'inline; filename="{archivo.nombre_archivo}"'},
    )


@router.get("/comprobantes/{movimiento_id}/editar")
def editar_movimiento_form(
    movimiento_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    movimiento = _get_or_redirect(db, Movement, movimiento_id, "/comprobantes")
    return templates.TemplateResponse(
        request, "editar_movimiento.html", {"user": user, "movimiento": movimiento, "error": None}
    )


@router.post("/comprobantes/{movimiento_id}/editar")
def editar_movimiento_submit(
    movimiento_id: int,
    request: Request,
    fecha_transaccion: str = Form(...),
    monto: str = Form(""),
    numero_operacion: str = Form(""),
    banco_emisor: str = Form(""),
    cuenta_receptora_extraida: str = Form(""),
    titular: str = Form(""),
    factura_o_cuenta_tipo: str = Form(""),
    factura_o_cuenta_numero: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    movimiento = _get_or_redirect(db, Movement, movimiento_id, "/comprobantes")

    try:
        nueva_fecha = datetime.strptime(fecha_transaccion, "%Y-%m-%dT%H:%M")
        nuevo_monto = Decimal(monto) if monto.strip() else None
    except (ValueError, InvalidOperation):
        return templates.TemplateResponse(
            request,
            "editar_movimiento.html",
            {"user": user, "movimiento": movimiento, "error": "Fecha o monto invalido."},
            status_code=400,
        )

    numero_operacion = numero_operacion.strip() or None
    if numero_operacion and _duplicate_exists(db, Movement.numero_operacion, numero_operacion, exclude_id=movimiento.id):
        return templates.TemplateResponse(
            request,
            "editar_movimiento.html",
            {"user": user, "movimiento": movimiento, "error": f"Ya existe otro comprobante con el numero {numero_operacion}."},
            status_code=400,
        )

    movimiento.fecha_transaccion = nueva_fecha
    movimiento.monto = nuevo_monto
    movimiento.numero_operacion = numero_operacion
    movimiento.banco_emisor = banco_emisor or None
    movimiento.cuenta_receptora_extraida = cuenta_receptora_extraida or None
    movimiento.titular = titular or None
    movimiento.factura_o_cuenta_tipo = TipoIdentificador(factura_o_cuenta_tipo) if factura_o_cuenta_tipo else None
    movimiento.factura_o_cuenta_numero = factura_o_cuenta_numero or None
    db.commit()
    return RedirectResponse("/comprobantes", status_code=303)


@router.post("/comprobantes/{movimiento_id}/eliminar")
def eliminar_movimiento(
    movimiento_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    movimiento = db.get(Movement, movimiento_id)
    if movimiento is not None:
        # Si estaba emparejado con una linea de resumen, la linea vuelve a quedar
        # pendiente en vez de arrastrar una referencia rota a un movimiento borrado.
        lineas_vinculadas = db.scalars(select(StatementLine).where(StatementLine.movimiento_id == movimiento.id)).all()
        for linea in lineas_vinculadas:
            linea.movimiento_id = None
            linea.estado = StatementLineState.PENDIENTE
        db.delete(movimiento)
        db.commit()
    return RedirectResponse("/comprobantes", status_code=303)


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


def _conciliaciones_context(
    db: Session, user: PanelUser, error: str | None, resumen_id: str = "", mensaje: str | None = None
) -> dict:
    cuentas = db.scalars(select(BankAccount).order_by(BankAccount.id)).all()
    resumenes = db.scalars(
        select(ImportedStatement)
        .options(selectinload(ImportedStatement.cuenta_bancaria))
        .order_by(ImportedStatement.fecha_importacion.desc())
    ).all()

    resumen_seleccionado = None
    if resumen_id:
        resumen_seleccionado = next((r for r in resumenes if r.id == int(resumen_id)), None)

    movimientos_query = select(Movement).where(
        Movement.estado_registro == RecordState.CONFIRMADO,
        Movement.estado_conciliacion.in_([ReconciliationState.PENDIENTE, ReconciliationState.CON_DIFERENCIA]),
    )
    lineas_pendientes_query = select(StatementLine).where(StatementLine.estado == StatementLineState.PENDIENTE)
    lineas_conciliadas_query = select(StatementLine).where(StatementLine.estado == StatementLineState.CONCILIADA)

    if resumen_seleccionado is not None:
        # Un Movement no se asocia a una cuenta hasta que se concilia (ver
        # reconciliation.py), asi que "movimientos que podrian pertenecer a este
        # resumen" son los que ya quedaron pegados a su cuenta o todavia no tienen
        # ninguna -- mismo criterio que usa el motor de matching automatico.
        movimientos_query = movimientos_query.where(
            (Movement.cuenta_bancaria_id.is_(None))
            | (Movement.cuenta_bancaria_id == resumen_seleccionado.cuenta_bancaria_id)
        )
        lineas_pendientes_query = lineas_pendientes_query.where(StatementLine.resumen_id == resumen_seleccionado.id)
        lineas_conciliadas_query = lineas_conciliadas_query.where(StatementLine.resumen_id == resumen_seleccionado.id)

    movimientos_pendientes = db.scalars(
        movimientos_query.options(selectinload(Movement.operador)).order_by(Movement.fecha_transaccion)
    ).all()
    lineas_pendientes = db.scalars(
        lineas_pendientes_query.options(
            selectinload(StatementLine.resumen).selectinload(ImportedStatement.cuenta_bancaria)
        ).order_by(StatementLine.fecha)
    ).all()
    lineas_conciliadas = db.scalars(
        lineas_conciliadas_query.options(
            selectinload(StatementLine.resumen).selectinload(ImportedStatement.cuenta_bancaria),
            selectinload(StatementLine.movimiento).selectinload(Movement.operador),
        ).order_by(StatementLine.fecha.desc())
    ).all()
    return {
        "user": user,
        "cuentas": cuentas,
        "movimientos_pendientes": movimientos_pendientes,
        "lineas_pendientes": lineas_pendientes,
        "lineas_conciliadas": lineas_conciliadas,
        "resumenes": resumenes,
        "conteos_por_resumen": _conteos_por_resumen(db),
        "resumen_id": resumen_id,
        "resumen_seleccionado": resumen_seleccionado,
        "error": error,
        "mensaje": mensaje,
    }


@router.get("/conciliaciones")
def conciliaciones(
    request: Request, resumen_id: str = "", db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    return templates.TemplateResponse(
        request, "conciliaciones.html", _conciliaciones_context(db, user, None, resumen_id)
    )


@router.post("/conciliaciones/reconciliar")
def reconciliar_pendientes(
    request: Request,
    resumen_id: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    """Reintenta el matching automatico sobre las lineas pendientes SIN pedir un
    archivo nuevo -- util cuando lo que cambio no fue el resumen del banco sino que
    se confirmaron comprobantes nuevos despues de la ultima carga, que antes no
    tenian con que emparejar."""
    if resumen_id:
        resumen = db.get(ImportedStatement, int(resumen_id))
        resumenes = [resumen] if resumen is not None else []
    else:
        resumenes = db.scalars(select(ImportedStatement)).all()

    conciliadas = 0
    for resumen in resumenes:
        lineas_pendientes = db.scalars(
            select(StatementLine).where(
                StatementLine.resumen_id == resumen.id, StatementLine.estado == StatementLineState.PENDIENTE
            )
        ).all()
        if not lineas_pendientes:
            continue
        match_statement(db, resumen, lineas_pendientes)
        conciliadas += sum(1 for linea in lineas_pendientes if linea.estado == StatementLineState.CONCILIADA)
    db.commit()

    mensaje = (
        f"Se {'concilio' if conciliadas == 1 else 'conciliaron'} {conciliadas} "
        f"linea{'s' if conciliadas != 1 else ''} nueva{'s' if conciliadas != 1 else ''}."
        if conciliadas
        else "No se encontraron coincidencias nuevas."
    )
    return templates.TemplateResponse(
        request, "conciliaciones.html", _conciliaciones_context(db, user, None, resumen_id, mensaje)
    )


@router.post("/conciliaciones/importar")
async def importar_resumen(
    request: Request,
    archivo: UploadFile,
    cuenta_bancaria_id: int = Form(...),
    fecha: str = Form(...),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    contenido = await archivo.read()
    try:
        filas, formato = parse_statement_file(archivo.filename or "", contenido)
    except StatementParseError as exc:
        return templates.TemplateResponse(
            request, "conciliaciones.html", _conciliaciones_context(db, user, str(exc)), status_code=400
        )

    resumen = ImportedStatement(
        cuenta_bancaria_id=cuenta_bancaria_id,
        fecha=datetime.strptime(fecha, "%Y-%m-%d"),
        archivo_nombre=archivo.filename or "resumen",
        formato=formato,
        usuario_id=user.id,
    )
    db.add(resumen)
    db.flush()

    lineas = [
        StatementLine(
            resumen_id=resumen.id,
            resumen=resumen,
            fecha=fila.fecha,
            monto=fila.monto,
            descripcion=fila.descripcion,
            referencia=fila.referencia,
        )
        for fila in filas
    ]
    db.add_all(lineas)
    match_statement(db, resumen, lineas)
    db.commit()
    return RedirectResponse("/conciliaciones", status_code=303)


@router.post("/conciliaciones/resumenes/{resumen_id}/actualizar")
async def actualizar_resumen(
    resumen_id: int,
    request: Request,
    archivo: UploadFile,
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    """Vuelve a leer el mismo archivo de resumen (o una version mas nueva del banco,
    ej. un export acumulativo del dia al que se le siguieron agregando filas) y
    agrega solo las transacciones que todavia no estaban cargadas para este
    resumen -- no duplica las que ya se importaron ni toca sus lineas."""
    resumen = db.get(ImportedStatement, resumen_id)
    if resumen is None:
        return RedirectResponse("/conciliaciones", status_code=303)

    contenido = await archivo.read()
    try:
        filas, _formato = parse_statement_file(archivo.filename or "", contenido)
    except StatementParseError as exc:
        return templates.TemplateResponse(
            request, "conciliaciones.html", _conciliaciones_context(db, user, str(exc), str(resumen_id)), status_code=400
        )

    lineas_existentes = db.scalars(select(StatementLine).where(StatementLine.resumen_id == resumen.id)).all()
    restantes = Counter((linea.fecha, linea.monto, linea.referencia, linea.descripcion) for linea in lineas_existentes)

    nuevas: list[StatementLine] = []
    for fila in filas:
        clave = (fila.fecha, fila.monto, fila.referencia, fila.descripcion)
        if restantes[clave] > 0:
            # Esta fila del archivo ya estaba cargada -- se "consume" una ocurrencia
            # en vez de saltearla directamente, para no perder transacciones
            # legitimamente repetidas (mismo monto/fecha/referencia dos veces).
            restantes[clave] -= 1
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

    if nuevas:
        db.add_all(nuevas)
        match_statement(db, resumen, nuevas)
    db.commit()

    mensaje = (
        f"Se agregaron {len(nuevas)} transaccion{'es' if len(nuevas) != 1 else ''} nueva{'s' if len(nuevas) != 1 else ''} del archivo."
        if nuevas
        else "No se encontraron transacciones nuevas en el archivo: ya estaba todo cargado."
    )
    return templates.TemplateResponse(
        request, "conciliaciones.html", _conciliaciones_context(db, user, None, str(resumen_id), mensaje)
    )


@router.post("/conciliaciones/lineas/{linea_id}/emparejar")
def emparejar_linea(
    linea_id: int,
    request: Request,
    movimiento_id: int = Form(...),
    resumen_id: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    linea = db.get(StatementLine, linea_id)
    movimiento = db.get(Movement, movimiento_id)
    if linea is not None and movimiento is not None:
        linea.movimiento_id = movimiento.id
        linea.estado = StatementLineState.CONCILIADA
        movimiento.estado_conciliacion = ReconciliationState.CONCILIADO_MANUALMENTE
        if movimiento.cuenta_bancaria_id is None:
            movimiento.cuenta_bancaria_id = linea.resumen.cuenta_bancaria_id
        db.commit()
    destino = f"/conciliaciones?resumen_id={resumen_id}" if resumen_id else "/conciliaciones"
    return RedirectResponse(destino, status_code=303)


@router.post("/conciliaciones/lineas/{linea_id}/no-corresponde")
def marcar_linea_no_corresponde(
    linea_id: int,
    request: Request,
    resumen_id: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    linea = db.get(StatementLine, linea_id)
    if linea is not None:
        linea.estado = StatementLineState.NO_CORRESPONDE
        db.commit()
    destino = f"/conciliaciones?resumen_id={resumen_id}" if resumen_id else "/conciliaciones"
    return RedirectResponse(destino, status_code=303)
