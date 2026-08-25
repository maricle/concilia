from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TypeVar

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import DeclarativeBase, InstrumentedAttribute, Session, selectinload

from .auth import verify_password
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
)
from .reconciliation import StatementParseError, match_statement, parse_statement_file

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

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


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_db)):
    if get_logged_in_user(request, db) is not None:
        return RedirectResponse("/config/operadores", status_code=303)
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
    return RedirectResponse("/config/operadores", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


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


@router.get("/comprobantes")
def list_movimientos(request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)):
    movimientos = db.scalars(
        select(Movement)
        .where(Movement.estado_registro == RecordState.CONFIRMADO)
        .options(selectinload(Movement.operador))
        .order_by(Movement.fecha_subida.desc())
    ).all()
    return templates.TemplateResponse(request, "movimientos.html", {"user": user, "movimientos": movimientos})


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
    monto: str = Form(...),
    numero_operacion: str = Form(...),
    banco_emisor: str = Form(""),
    cuenta_receptora_extraida: str = Form(""),
    titular: str = Form(""),
    factura_o_cuenta: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    movimiento = _get_or_redirect(db, Movement, movimiento_id, "/comprobantes")

    try:
        nueva_fecha = datetime.strptime(fecha_transaccion, "%Y-%m-%d")
        nuevo_monto = Decimal(monto)
    except (ValueError, InvalidOperation):
        return templates.TemplateResponse(
            request,
            "editar_movimiento.html",
            {"user": user, "movimiento": movimiento, "error": "Fecha o monto invalido."},
            status_code=400,
        )

    if _duplicate_exists(db, Movement.numero_operacion, numero_operacion, exclude_id=movimiento.id):
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
    movimiento.factura_o_cuenta = factura_o_cuenta or None
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


def _conciliaciones_context(db: Session, user: PanelUser, error: str | None) -> dict:
    cuentas = db.scalars(select(BankAccount).order_by(BankAccount.id)).all()
    movimientos_pendientes = db.scalars(
        select(Movement)
        .where(
            Movement.estado_registro == RecordState.CONFIRMADO,
            Movement.estado_conciliacion.in_([ReconciliationState.PENDIENTE, ReconciliationState.CON_DIFERENCIA]),
        )
        .options(selectinload(Movement.operador))
        .order_by(Movement.fecha_transaccion)
    ).all()
    lineas_pendientes = db.scalars(
        select(StatementLine)
        .where(StatementLine.estado == StatementLineState.PENDIENTE)
        .options(selectinload(StatementLine.resumen).selectinload(ImportedStatement.cuenta_bancaria))
        .order_by(StatementLine.fecha)
    ).all()
    lineas_conciliadas = db.scalars(
        select(StatementLine)
        .where(StatementLine.estado == StatementLineState.CONCILIADA)
        .options(
            selectinload(StatementLine.resumen).selectinload(ImportedStatement.cuenta_bancaria),
            selectinload(StatementLine.movimiento).selectinload(Movement.operador),
        )
        .order_by(StatementLine.fecha.desc())
    ).all()
    resumenes = db.scalars(
        select(ImportedStatement)
        .options(selectinload(ImportedStatement.cuenta_bancaria))
        .order_by(ImportedStatement.fecha_importacion.desc())
    ).all()
    return {
        "user": user,
        "cuentas": cuentas,
        "movimientos_pendientes": movimientos_pendientes,
        "lineas_pendientes": lineas_pendientes,
        "lineas_conciliadas": lineas_conciliadas,
        "resumenes": resumenes,
        "conteos_por_resumen": _conteos_por_resumen(db),
        "error": error,
    }


@router.get("/conciliaciones")
def conciliaciones(request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)):
    return templates.TemplateResponse(
        request, "conciliaciones.html", _conciliaciones_context(db, user, None)
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


@router.post("/conciliaciones/lineas/{linea_id}/emparejar")
def emparejar_linea(
    linea_id: int,
    request: Request,
    movimiento_id: int = Form(...),
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
    return RedirectResponse("/conciliaciones", status_code=303)


@router.post("/conciliaciones/lineas/{linea_id}/no-corresponde")
def marcar_linea_no_corresponde(
    linea_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    linea = db.get(StatementLine, linea_id)
    if linea is not None:
        linea.estado = StatementLineState.NO_CORRESPONDE
        db.commit()
    return RedirectResponse("/conciliaciones", status_code=303)
