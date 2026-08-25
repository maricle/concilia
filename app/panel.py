from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

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


def get_db():
    with SessionLocal() as session:
        yield session


def get_logged_in_user(request: Request, db: Session) -> PanelUser | None:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    user = db.get(PanelUser, user_id)
    return user if user is not None and user.activo else None


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
def list_operadores(request: Request, db: Session = Depends(get_db)):
    user = get_logged_in_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
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
):
    user = get_logged_in_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    existing = db.scalar(select(Operator).where(Operator.whatsapp_numero == whatsapp_numero))
    if existing is not None:
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
def toggle_operador(operador_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_logged_in_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    operador = db.get(Operator, operador_id)
    if operador is not None:
        operador.activo = not operador.activo
        db.commit()
    return RedirectResponse("/config/operadores", status_code=303)


@router.get("/config/cuentas")
def list_cuentas(request: Request, db: Session = Depends(get_db)):
    user = get_logged_in_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
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
):
    user = get_logged_in_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    db.add(BankAccount(banco=banco, numero_cuenta=numero_cuenta, alias=alias, moneda=moneda))
    db.commit()
    return RedirectResponse("/config/cuentas", status_code=303)


@router.get("/comprobantes")
def list_movimientos(request: Request, db: Session = Depends(get_db)):
    user = get_logged_in_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    movimientos = db.scalars(
        select(Movement)
        .where(Movement.estado_registro == RecordState.CONFIRMADO)
        .order_by(Movement.fecha_subida.desc())
    ).all()
    return templates.TemplateResponse(request, "movimientos.html", {"user": user, "movimientos": movimientos})


def _conciliaciones_context(db: Session, user: PanelUser, error: str | None) -> dict:
    cuentas = db.scalars(select(BankAccount).order_by(BankAccount.id)).all()
    movimientos_pendientes = db.scalars(
        select(Movement)
        .where(
            Movement.estado_registro == RecordState.CONFIRMADO,
            Movement.estado_conciliacion.in_([ReconciliationState.PENDIENTE, ReconciliationState.CON_DIFERENCIA]),
        )
        .order_by(Movement.fecha_transaccion)
    ).all()
    lineas_pendientes = db.scalars(
        select(StatementLine)
        .where(StatementLine.estado == StatementLineState.PENDIENTE)
        .order_by(StatementLine.fecha)
    ).all()
    resumenes = db.scalars(select(ImportedStatement).order_by(ImportedStatement.fecha_importacion.desc())).all()
    return {
        "user": user,
        "cuentas": cuentas,
        "movimientos_pendientes": movimientos_pendientes,
        "lineas_pendientes": lineas_pendientes,
        "resumenes": resumenes,
        "error": error,
    }


@router.get("/conciliaciones")
def conciliaciones(request: Request, db: Session = Depends(get_db)):
    user = get_logged_in_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

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
):
    user = get_logged_in_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

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
):
    user = get_logged_in_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

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
def marcar_linea_no_corresponde(linea_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_logged_in_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    linea = db.get(StatementLine, linea_id)
    if linea is not None:
        linea.estado = StatementLineState.NO_CORRESPONDE
        db.commit()
    return RedirectResponse("/conciliaciones", status_code=303)
