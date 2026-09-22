import io
import json
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TypeVar
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import DeclarativeBase, InstrumentedAttribute, Session, selectinload

from .auth import hash_password, verify_password
from .bancos import icono_banco
from .conversation import _find_cuenta_bancaria
from .db import SessionLocal
from .extraction import extract_transfer
from .models import (
    BankAccount,
    CierreDiario,
    ImportedStatement,
    Movement,
    Movil,
    Operator,
    PanelUser,
    ReconciliationState,
    RecordState,
    Reparto,
    RepartoOperador,
    StatementLine,
    StatementLineState,
    TipoIdentificador,
)
from .numeros import parse_monto_ar
from .reconciliation import StatementParseError, match_statement, parse_statement_file
from .reportes import generar_resumen_salida_pdf, generar_resumen_salidas_pdf
from .storage import get_comprobante_archivo, save_comprobante_archivo
from .zona_horaria import hoy_argentina

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


def _sort_url(request: Request, campo: str) -> str:
    """URL para el link de un encabezado de columna ordenable: conserva todos los
    filtros/parametros actuales de la pagina, pisando solo sort/order. Si ya se
    esta ordenando por esa misma columna invierte la direccion; si no, arranca
    ascendente."""
    parametros = dict(request.query_params)
    ya_activo = parametros.get("sort") == campo
    nuevo_orden = "desc" if ya_activo and parametros.get("order", "asc") == "asc" else "asc"
    parametros["sort"] = campo
    parametros["order"] = nuevo_orden
    return f"{request.url.path}?{urlencode(parametros)}"


templates.env.globals["sort_url"] = _sort_url
templates.env.globals["icono_banco"] = icono_banco


def _xlsx_response(headers: list[str], filas: list[list], filename: str) -> Response:
    """Arma un .xlsx descargable a partir de un encabezado y filas de valores --
    usado por todos los botones "Exportar" del panel, en vez de CSV."""
    workbook = Workbook()
    hoja = workbook.active
    hoja.append(headers)
    for fila in filas:
        hoja.append(fila)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return Response(
        content=buffer.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


def _validar_responsable_movil(db: Session, responsable_operador_id: int | None) -> str | None:
    """Un movil no se puede guardar sin un responsable que tenga celular cargado
    -- devuelve el mensaje de error, o None si esta todo bien."""
    if responsable_operador_id is None:
        return "El movil necesita un operador responsable."
    responsable = db.get(Operator, responsable_operador_id)
    if responsable is None:
        return "El operador responsable seleccionado no existe."
    if not responsable.whatsapp_numero.strip():
        return f"El operador responsable ({responsable.nombre}) no tiene celular cargado."
    return None


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
    email = email.strip().lower()
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


@router.get("/config/usuarios/exportar")
def usuarios_exportar(db: Session = Depends(get_db), user: PanelUser = Depends(require_user)):
    usuarios = db.scalars(select(PanelUser).order_by(PanelUser.id)).all()
    filas = [[u.nombre, u.email, u.rol, "Activo" if u.activo else "Inactivo"] for u in usuarios]
    return _xlsx_response(["Nombre", "Email", "Rol", "Estado"], filas, "usuarios.xlsx")


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
    email = email.strip().lower()
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

    email = email.strip().lower()
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
def list_operadores(
    request: Request, error: str = "", db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    operadores = db.scalars(select(Operator).order_by(Operator.id)).all()
    moviles = db.scalars(select(Movil).where(Movil.activo.is_(True)).order_by(Movil.nombre)).all()
    return templates.TemplateResponse(
        request, "operadores.html", {"user": user, "operadores": operadores, "moviles": moviles, "error": error or None}
    )


@router.get("/config/operadores/exportar")
def operadores_exportar(db: Session = Depends(get_db), user: PanelUser = Depends(require_user)):
    operadores = db.scalars(select(Operator).options(selectinload(Operator.movil)).order_by(Operator.id)).all()
    filas = [
        [
            o.nombre,
            o.whatsapp_numero,
            "Si" if o.telegram_chat_id else "No",
            o.tipo,
            o.movil.numero if o.movil else "",
            "Activo" if o.activo else "Inactivo",
        ]
        for o in operadores
    ]
    return _xlsx_response(
        ["Nombre", "Numero", "Telegram vinculado", "Tipo", "Movil", "Estado"], filas, "operadores.xlsx"
    )


@router.post("/config/operadores")
def create_operador(
    request: Request,
    nombre: str = Form(...),
    whatsapp_numero: str = Form(...),
    tipo: str = Form("Reparto"),
    movil_id: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    if _duplicate_exists(db, Operator.whatsapp_numero, whatsapp_numero):
        operadores = db.scalars(select(Operator).order_by(Operator.id)).all()
        moviles = db.scalars(select(Movil).where(Movil.activo.is_(True)).order_by(Movil.nombre)).all()
        return templates.TemplateResponse(
            request,
            "operadores.html",
            {
                "user": user,
                "operadores": operadores,
                "moviles": moviles,
                "error": f"Ya existe un operador con el numero {whatsapp_numero}.",
            },
            status_code=400,
        )

    db.add(
        Operator(
            nombre=nombre, whatsapp_numero=whatsapp_numero, tipo=tipo, movil_id=int(movil_id) if movil_id else None
        )
    )
    db.commit()
    return RedirectResponse("/config/operadores", status_code=303)


@router.post("/config/operadores/{operador_id}/toggle")
def toggle_operador(
    operador_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    operador = db.get(Operator, operador_id)
    if operador is not None:
        if operador.activo:
            es_responsable = db.scalar(
                select(Movil).where(Movil.responsable_operador_id == operador.id, Movil.activo.is_(True))
            )
            if es_responsable is not None:
                mensaje = f"No se puede desactivar a {operador.nombre}: es responsable del movil {es_responsable.numero}."
                return RedirectResponse(f"/config/operadores?{urlencode({'error': mensaje})}", status_code=303)
        operador.activo = not operador.activo
        db.commit()
    return RedirectResponse("/config/operadores", status_code=303)


@router.get("/config/operadores/{operador_id}/editar")
def editar_operador_form(
    operador_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    operador = _get_or_redirect(db, Operator, operador_id, "/config/operadores")
    moviles = db.scalars(select(Movil).where(Movil.activo.is_(True)).order_by(Movil.nombre)).all()
    return templates.TemplateResponse(
        request, "editar_operador.html", {"user": user, "operador": operador, "moviles": moviles, "error": None}
    )


@router.post("/config/operadores/{operador_id}/editar")
def editar_operador_submit(
    operador_id: int,
    request: Request,
    nombre: str = Form(...),
    whatsapp_numero: str = Form(""),
    tipo: str = Form("Reparto"),
    movil_id: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    operador = _get_or_redirect(db, Operator, operador_id, "/config/operadores")
    moviles = db.scalars(select(Movil).where(Movil.activo.is_(True)).order_by(Movil.nombre)).all()

    if not whatsapp_numero.strip():
        es_responsable = db.scalar(
            select(Movil).where(Movil.responsable_operador_id == operador.id, Movil.activo.is_(True))
        )
        mensaje = (
            f"No se puede dejar sin celular a {operador.nombre}: es responsable del movil {es_responsable.numero}."
            if es_responsable is not None
            else "El numero de celular es obligatorio."
        )
        return templates.TemplateResponse(
            request,
            "editar_operador.html",
            {"user": user, "operador": operador, "moviles": moviles, "error": mensaje},
            status_code=400,
        )

    if _duplicate_exists(db, Operator.whatsapp_numero, whatsapp_numero, exclude_id=operador.id):
        return templates.TemplateResponse(
            request,
            "editar_operador.html",
            {"user": user, "operador": operador, "moviles": moviles, "error": f"Ya existe otro operador con el numero {whatsapp_numero}."},
            status_code=400,
        )

    operador.nombre = nombre
    operador.whatsapp_numero = whatsapp_numero
    operador.tipo = tipo
    operador.movil_id = int(movil_id) if movil_id else None
    db.commit()
    return RedirectResponse("/config/operadores", status_code=303)


@router.get("/config/moviles")
def list_moviles(
    request: Request, error: str = "", db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    moviles = db.scalars(select(Movil).order_by(Movil.id)).all()
    operadores = db.scalars(select(Operator).where(Operator.activo.is_(True)).order_by(Operator.nombre)).all()
    return templates.TemplateResponse(
        request, "moviles.html", {"user": user, "moviles": moviles, "operadores": operadores, "error": error or None}
    )


@router.get("/config/moviles/exportar")
def moviles_exportar(db: Session = Depends(get_db), user: PanelUser = Depends(require_user)):
    moviles = db.scalars(select(Movil).options(selectinload(Movil.responsable)).order_by(Movil.id)).all()
    filas = [
        [
            m.numero,
            m.nombre,
            m.descripcion or "",
            m.responsable.nombre if m.responsable else "",
            "Activo" if m.activo else "Inactivo",
        ]
        for m in moviles
    ]
    return _xlsx_response(["Numero", "Nombre", "Descripcion", "Responsable", "Estado"], filas, "moviles.xlsx")


@router.post("/config/moviles")
def create_movil(
    request: Request,
    numero: str = Form(...),
    nombre: str = Form(...),
    descripcion: str = Form(""),
    responsable_operador_id: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    error = None
    if _duplicate_exists(db, Movil.numero, numero):
        error = f"Ya existe un movil con el numero {numero}."
    else:
        error = _validar_responsable_movil(db, int(responsable_operador_id) if responsable_operador_id else None)

    if error:
        moviles = db.scalars(select(Movil).order_by(Movil.id)).all()
        operadores = db.scalars(select(Operator).where(Operator.activo.is_(True)).order_by(Operator.nombre)).all()
        return templates.TemplateResponse(
            request,
            "moviles.html",
            {"user": user, "moviles": moviles, "operadores": operadores, "error": error},
            status_code=400,
        )

    db.add(
        Movil(
            numero=numero,
            nombre=nombre,
            descripcion=descripcion or None,
            responsable_operador_id=int(responsable_operador_id),
        )
    )
    db.commit()
    return RedirectResponse("/config/moviles", status_code=303)


@router.post("/config/moviles/{movil_id}/toggle")
def toggle_movil(
    movil_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    movil = db.get(Movil, movil_id)
    if movil is not None:
        movil.activo = not movil.activo
        db.commit()
    return RedirectResponse("/config/moviles", status_code=303)


@router.get("/config/moviles/{movil_id}/editar")
def editar_movil_form(
    movil_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    movil = _get_or_redirect(db, Movil, movil_id, "/config/moviles")
    operadores = db.scalars(select(Operator).where(Operator.activo.is_(True)).order_by(Operator.nombre)).all()
    return templates.TemplateResponse(
        request, "editar_movil.html", {"user": user, "movil": movil, "operadores": operadores, "error": None}
    )


@router.post("/config/moviles/{movil_id}/editar")
def editar_movil_submit(
    movil_id: int,
    request: Request,
    numero: str = Form(...),
    nombre: str = Form(...),
    descripcion: str = Form(""),
    responsable_operador_id: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    movil = _get_or_redirect(db, Movil, movil_id, "/config/moviles")
    operadores = db.scalars(select(Operator).where(Operator.activo.is_(True)).order_by(Operator.nombre)).all()

    error = None
    if _duplicate_exists(db, Movil.numero, numero, exclude_id=movil.id):
        error = f"Ya existe otro movil con el numero {numero}."
    else:
        error = _validar_responsable_movil(db, int(responsable_operador_id) if responsable_operador_id else None)

    if error:
        return templates.TemplateResponse(
            request,
            "editar_movil.html",
            {"user": user, "movil": movil, "operadores": operadores, "error": error},
            status_code=400,
        )

    movil.numero = numero
    movil.nombre = nombre
    movil.descripcion = descripcion or None
    movil.responsable_operador_id = int(responsable_operador_id)
    db.commit()
    return RedirectResponse("/config/moviles", status_code=303)


@router.get("/config/cuentas")
def list_cuentas(request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)):
    cuentas = db.scalars(select(BankAccount).order_by(BankAccount.id)).all()
    return templates.TemplateResponse(request, "cuentas.html", {"user": user, "cuentas": cuentas, "error": None})


@router.get("/config/cuentas/exportar")
def cuentas_exportar(db: Session = Depends(get_db), user: PanelUser = Depends(require_user)):
    cuentas = db.scalars(select(BankAccount).order_by(BankAccount.id)).all()
    filas = [[c.banco, c.numero_cuenta, c.alias, c.moneda] for c in cuentas]
    return _xlsx_response(["Banco", "Numero de cuenta", "Alias", "Moneda"], filas, "cuentas.xlsx")


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
    """Un panel por banco (mismo shape que _panel_banco, de /conciliaciones) para
    cada banco con al menos un comprobante hoy -- usado en el home."""
    hoy = hoy_argentina()
    resultado = [
        panel
        for banco in _bancos_disponibles(db)
        if (panel := _panel_banco(db, hoy, banco))["cantidad_comprobantes"] > 0
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
    return _xlsx_response(
        ["Vendedor", "Telefono", "Tipo", "Total", "Comprobantes", "Conciliados con el banco", "Pendientes de conciliar", "Con diferencia"],
        filas,
        "resumen.xlsx",
    )


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
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    movimientos = db.scalars(
        _comprobantes_query(
            banco, estado_conciliacion, operador_id,
            fecha_transaccion_desde, fecha_transaccion_hasta, fecha_subida_desde, fecha_subida_hasta, q,
            sort, order, movil_id,
        )
    ).all()
    cuentas = db.scalars(select(BankAccount).order_by(BankAccount.id)).all()
    operadores = db.scalars(select(Operator).order_by(Operator.nombre)).all()
    moviles = db.scalars(select(Movil).order_by(Movil.nombre)).all()

    return templates.TemplateResponse(
        request,
        "movimientos.html",
        {
            "user": user,
            "movimientos": movimientos,
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
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    movimientos = db.scalars(
        _comprobantes_query(
            banco, estado_conciliacion, operador_id,
            fecha_transaccion_desde, fecha_transaccion_hasta, fecha_subida_desde, fecha_subida_hasta, q,
            sort, order, movil_id,
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
    return _xlsx_response(
        [
            "Fecha transaccion", "Fecha subida", "Tipo", "Nro. factura/cuenta", "Cuenta banco", "Banco emisor",
            "Titular/Emisor", "N. operacion", "Monto", "Vendedor", "Movil", "Nro. Salida", "Conciliacion",
        ],
        filas,
        "comprobantes.xlsx",
    )


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
    return _xlsx_response(
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

    transfer = extract_transfer(archivo.content_type or "application/octet-stream", contenido)
    if transfer is None:
        return {"ok": False, "error": "No pudimos leer el comprobante. Completa los datos a mano."}

    cuenta = _find_cuenta_bancaria(db, transfer.cuenta_receptora)
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
    if numero_operacion and _duplicate_exists(db, Movement.numero_operacion, numero_operacion):
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
    movimiento_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    movimiento = _get_or_redirect(db, Movement, movimiento_id, "/comprobantes")
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
    user: PanelUser = Depends(require_user),
):
    movimiento = _get_or_redirect(db, Movement, movimiento_id, "/comprobantes")
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
    if numero_operacion and _duplicate_exists(db, Movement.numero_operacion, numero_operacion, exclude_id=movimiento.id):
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
    return RedirectResponse("/comprobantes", status_code=303)


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
    movimiento_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    movimiento = db.get(Movement, movimiento_id)
    if movimiento is not None:
        _eliminar_movimiento(db, movimiento)
        db.commit()
    return RedirectResponse("/comprobantes", status_code=303)


@router.post("/comprobantes/eliminar-lote")
def eliminar_movimientos_lote(
    request: Request,
    movimiento_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    for movimiento_id in movimiento_ids:
        movimiento = db.get(Movement, movimiento_id)
        if movimiento is not None:
            _eliminar_movimiento(db, movimiento)
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


def _parse_fecha_o_hoy(fecha: str) -> date:
    if fecha:
        try:
            return datetime.strptime(fecha, "%Y-%m-%d").date()
        except ValueError:
            pass
    return hoy_argentina()


def _bancos_disponibles(db: Session) -> list[str]:
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


def _panel_banco(db: Session, fecha: date, banco: str) -> dict:
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
    return all(_panel_banco(db, fecha, banco)["estado"] not in ("a_revisar", "error") for banco in bancos)


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
        try:
            condiciones.append(Movement.monto == parse_monto_ar(search))
        except ValueError:
            pass
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


def _construir_contexto_conciliaciones(
    db: Session, user: PanelUser, fecha: date, banco: str = "", error: str | None = None, mensaje: str | None = None
) -> dict:
    bancos = _bancos_disponibles(db)
    banco = _banco_o_default(db, banco, bancos)

    panel = _panel_banco(db, fecha, banco) if banco else None
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
    # misma lista (mismo filtro que usa _panel_banco para las lineas pendientes),
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
    user: PanelUser = Depends(require_user),
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
    return templates.TemplateResponse(
        request, "conciliaciones.html", _construir_contexto_conciliaciones(db, user, fecha_obj, banco)
    )


@router.get("/conciliaciones/panel.json")
def conciliaciones_panel_json(
    fecha: str = "", banco: str = "", db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    fecha_obj = _parse_fecha_o_hoy(fecha)
    bancos = _bancos_disponibles(db)
    banco = _banco_o_default(db, banco, bancos)
    panel = _panel_banco(db, fecha_obj, banco) if banco else None
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
    user: PanelUser = Depends(require_user),
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
    user: PanelUser = Depends(require_user),
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
    return _xlsx_response(
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
    user: PanelUser = Depends(require_user),
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
    fecha: str = "", db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    fecha_obj = _parse_fecha_o_hoy(fecha)
    bancos = _bancos_disponibles(db)
    paneles = [_serializar_panel(_panel_banco(db, fecha_obj, banco)) for banco in bancos]
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
    request: Request, fecha: str = Form(...), db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    fecha_obj = _parse_fecha_o_hoy(fecha)
    bancos = _bancos_disponibles(db)
    if not _dia_puede_cerrarse(db, fecha_obj, bancos):
        return templates.TemplateResponse(
            request,
            "conciliaciones.html",
            _construir_contexto_conciliaciones(
                db, user, fecha_obj, error="No se puede cerrar el dia: hay bancos pendientes de revision."
            ),
            status_code=400,
        )
    if _cierre_del_dia(db, fecha_obj) is None:
        db.add(CierreDiario(fecha=fecha_obj, cerrado_por_id=user.id))
        db.commit()
    return RedirectResponse(f"/conciliaciones?fecha={fecha_obj.isoformat()}", status_code=303)


@router.post("/conciliaciones/cierres/{fecha}/reabrir")
def reabrir_dia(
    fecha: str, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
):
    fecha_obj = _parse_fecha_o_hoy(fecha)
    cierre = _cierre_del_dia(db, fecha_obj)
    if cierre is not None:
        db.delete(cierre)
        db.commit()
    return RedirectResponse(f"/conciliaciones?fecha={fecha_obj.isoformat()}", status_code=303)


@router.post("/conciliaciones/reconciliar")
def reconciliar_pendientes(
    request: Request,
    fecha: str = Form(""),
    banco: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
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
    db.commit()

    mensaje = (
        f"Se {'concilio' if conciliadas == 1 else 'conciliaron'} {conciliadas} "
        f"linea{'s' if conciliadas != 1 else ''} nueva{'s' if conciliadas != 1 else ''}."
        if conciliadas
        else "No se encontraron coincidencias nuevas."
    )
    return templates.TemplateResponse(
        request, "conciliaciones.html", _construir_contexto_conciliaciones(db, user, fecha_obj, banco, mensaje=mensaje)
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
    fecha_obj = datetime.strptime(fecha, "%Y-%m-%d").date()
    cuenta = db.get(BankAccount, cuenta_bancaria_id)
    banco = cuenta.banco if cuenta is not None else ""

    error_cierre = _dia_cerrado_error(db, fecha_obj)
    if error_cierre:
        return templates.TemplateResponse(
            request,
            "conciliaciones.html",
            _construir_contexto_conciliaciones(db, user, fecha_obj, banco, error=error_cierre),
            status_code=400,
        )

    contenido = await archivo.read()
    try:
        filas, formato = parse_statement_file(archivo.filename or "", contenido)
    except StatementParseError as exc:
        return templates.TemplateResponse(
            request,
            "conciliaciones.html",
            _construir_contexto_conciliaciones(db, user, fecha_obj, banco, error=str(exc)),
            status_code=400,
        )

    content_type = archivo.content_type or (
        "text/csv" if formato == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    archivo_id = save_comprobante_archivo(archivo.filename or "resumen", content_type, contenido)

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
    return RedirectResponse(f"/conciliaciones?fecha={fecha}&banco={banco}", status_code=303)


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
    fecha_obj = resumen.fecha.date()
    cuenta = db.get(BankAccount, resumen.cuenta_bancaria_id)
    banco = cuenta.banco if cuenta is not None else ""

    error_cierre = _dia_cerrado_error(db, fecha_obj)
    if error_cierre:
        return templates.TemplateResponse(
            request,
            "conciliaciones.html",
            _construir_contexto_conciliaciones(db, user, fecha_obj, banco, error=error_cierre),
            status_code=400,
        )

    contenido = await archivo.read()
    try:
        filas, _formato = parse_statement_file(archivo.filename or "", contenido)
    except StatementParseError as exc:
        return templates.TemplateResponse(
            request,
            "conciliaciones.html",
            _construir_contexto_conciliaciones(db, user, fecha_obj, banco, error=str(exc)),
            status_code=400,
        )

    # El archivo re-subido reemplaza al guardado (puede ser una version mas nueva
    # y mas completa del mismo resumen) -- queda el ultimo, no se conservan los
    # anteriores.
    content_type = archivo.content_type or (
        "text/csv" if _formato == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    resumen.archivo_id = save_comprobante_archivo(archivo.filename or resumen.archivo_nombre, content_type, contenido)

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
        request, "conciliaciones.html", _construir_contexto_conciliaciones(db, user, fecha_obj, banco, mensaje=mensaje)
    )


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
    user: PanelUser = Depends(require_user),
):
    resumenes = db.scalars(_resumenes_query(banco, fecha_desde, fecha_hasta, q)).all()
    return templates.TemplateResponse(
        request,
        "resumenes.html",
        {
            "user": user,
            "resumenes": resumenes,
            "conteos_por_resumen": _conteos_por_resumen(db),
            "bancos": _bancos_disponibles(db),
            "banco": banco,
            "fecha_desde": fecha_desde,
            "fecha_hasta": fecha_hasta,
            "q": q,
        },
    )


@router.get("/conciliaciones/resumenes/{resumen_id}/descargar")
def descargar_resumen(
    resumen_id: int, db: Session = Depends(get_db), user: PanelUser = Depends(require_user)
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
    user: PanelUser = Depends(require_user),
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
            return templates.TemplateResponse(
                request,
                "conciliaciones.html",
                _construir_contexto_conciliaciones(
                    db, user, fecha_obj, banco, error="Ese movimiento ya esta conciliado con otra linea."
                ),
                status_code=400,
            )
        linea.movimiento_id = movimiento.id
        linea.estado = StatementLineState.CONCILIADA
        movimiento.estado_conciliacion = ReconciliationState.CONCILIADO_MANUALMENTE
        if movimiento.cuenta_bancaria_id is None:
            movimiento.cuenta_bancaria_id = linea.resumen.cuenta_bancaria_id
        db.commit()
    if fecha or banco:
        destino = f"/conciliaciones?fecha={fecha}&banco={banco}"
    else:
        destino = f"/conciliaciones?resumen_id={resumen_id}" if resumen_id else "/conciliaciones"
    return RedirectResponse(destino, status_code=303)


@router.post("/conciliaciones/lineas/{linea_id}/no-corresponde")
def marcar_linea_no_corresponde(
    linea_id: int,
    request: Request,
    resumen_id: str = Form(""),
    fecha: str = Form(""),
    banco: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_user),
):
    linea = db.get(StatementLine, linea_id)
    if linea is not None and _cierre_del_dia(db, linea.fecha.date()) is None:
        linea.estado = StatementLineState.NO_CORRESPONDE
        db.commit()
    if fecha or banco:
        destino = f"/conciliaciones?fecha={fecha}&banco={banco}"
    else:
        destino = f"/conciliaciones?resumen_id={resumen_id}" if resumen_id else "/conciliaciones"
    return RedirectResponse(destino, status_code=303)
