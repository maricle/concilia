"""Configuracion: usuarios del panel, operadores, moviles y cuentas bancarias."""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..auth import hash_password
from ..canales import ultimos_digitos
from ..models import (
    BankAccount,
    Movil,
    Operator,
    PanelUser,
)
from .comun import (
    con_parametros,
    existe_duplicado,
    get_db,
    obtener_o_redirigir,
    require_admin,
    respuesta_xlsx,
    rol_valido,
    templates,
    volver_a,
)

router = APIRouter()
logger = logging.getLogger(__name__)

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


@router.get("/config/usuarios")
def list_usuarios(request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)):
    usuarios = db.scalars(select(PanelUser).order_by(PanelUser.id)).all()
    return templates.TemplateResponse(request, "usuarios.html", {"user": user, "usuarios": usuarios, "error": None})


@router.get("/config/usuarios/exportar")
def usuarios_exportar(db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)):
    usuarios = db.scalars(select(PanelUser).order_by(PanelUser.id)).all()
    filas = [[u.nombre, u.email, u.rol, "Activo" if u.activo else "Inactivo"] for u in usuarios]
    return respuesta_xlsx(["Nombre", "Email", "Rol", "Estado"], filas, "usuarios.xlsx")


@router.post("/config/usuarios")
def create_usuario(
    request: Request,
    nombre: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    rol: str = Form("Administrador"),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    email = email.strip().lower()
    error = None
    if existe_duplicado(db, PanelUser.email, email):
        error = f"Ya existe un usuario con el email {email}."
    elif len(password) < 8:
        error = "La contrasena tiene que tener al menos 8 caracteres."
    elif rol_valido(rol) is None:
        error = "El rol tiene que ser Administrador, Contador o Recaudador."

    if error:
        usuarios = db.scalars(select(PanelUser).order_by(PanelUser.id)).all()
        return templates.TemplateResponse(
            request, "usuarios.html", {"user": user, "usuarios": usuarios, "error": error}, status_code=400
        )

    db.add(PanelUser(nombre=nombre, email=email, password_hash=hash_password(password), rol=rol_valido(rol)))
    db.commit()
    return RedirectResponse(volver_a(request, "/config/usuarios"), status_code=303)


@router.post("/config/usuarios/{usuario_id}/toggle")
def toggle_usuario(
    usuario_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    usuario = db.get(PanelUser, usuario_id)
    if usuario is not None and usuario.id != user.id:
        # No se permite que un usuario se desactive a si mismo, para evitar que
        # el panel quede sin nadie que pueda volver a activar cuentas.
        usuario.activo = not usuario.activo
        db.commit()
    return RedirectResponse(volver_a(request, "/config/usuarios"), status_code=303)


@router.get("/config/usuarios/{usuario_id}/editar")
def editar_usuario_form(
    usuario_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    usuario = obtener_o_redirigir(db, PanelUser, usuario_id, "/config/usuarios")
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
    user: PanelUser = Depends(require_admin),
):
    usuario = obtener_o_redirigir(db, PanelUser, usuario_id, "/config/usuarios")

    email = email.strip().lower()
    error = None
    if existe_duplicado(db, PanelUser.email, email, exclude_id=usuario.id):
        error = f"Ya existe otro usuario con el email {email}."
    elif password and len(password) < 8:
        error = "La contrasena tiene que tener al menos 8 caracteres."
    elif rol_valido(rol) is None:
        error = "El rol tiene que ser Administrador, Contador o Recaudador."

    if error:
        return templates.TemplateResponse(
            request, "editar_usuario.html", {"user": user, "usuario": usuario, "error": error}, status_code=400
        )

    usuario.nombre = nombre
    usuario.email = email
    usuario.rol = rol_valido(rol)
    if password:
        usuario.password_hash = hash_password(password)
    db.commit()
    return RedirectResponse(volver_a(request, "/config/usuarios"), status_code=303)


@router.get("/config/operadores")
def list_operadores(
    request: Request, error: str = "", db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    operadores = db.scalars(select(Operator).order_by(Operator.id)).all()
    moviles = db.scalars(select(Movil).where(Movil.activo.is_(True)).order_by(Movil.nombre)).all()
    return templates.TemplateResponse(
        request, "operadores.html", {"user": user, "operadores": operadores, "moviles": moviles, "error": error or None}
    )


@router.get("/config/operadores/exportar")
def operadores_exportar(db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)):
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
    return respuesta_xlsx(
        ["Nombre", "Numero", "Telegram vinculado", "Tipo", "Movil", "Estado"], filas, "operadores.xlsx"
    )


def _operador_con_el_mismo_celular(db: Session, celular: str, excluir_id: int | None = None) -> Operator | None:
    """Otro operador (activo o no) con el mismo celular. Se compara con el mismo
    criterio que usan WhatsApp, Telegram y el login web para identificar al
    operador -- los ultimos 10 digitos --, asi "3794500000" y "+54 9 379 450-0000"
    cuentan como el mismo numero y nunca quedan dos operadores que se confundan."""
    clave = ultimos_digitos(celular)
    consulta = select(Operator)
    if excluir_id is not None:
        consulta = consulta.where(Operator.id != excluir_id)
    for operador in db.scalars(consulta):
        if operador.whatsapp_numero.strip() == celular.strip():
            return operador
        if clave is not None and ultimos_digitos(operador.whatsapp_numero) == clave:
            return operador
    return None


@router.post("/config/operadores")
def create_operador(
    request: Request,
    nombre: str = Form(...),
    whatsapp_numero: str = Form(...),
    tipo: str = Form("Reparto"),
    movil_id: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    duplicado = _operador_con_el_mismo_celular(db, whatsapp_numero)
    if duplicado is not None:
        operadores = db.scalars(select(Operator).order_by(Operator.id)).all()
        moviles = db.scalars(select(Movil).where(Movil.activo.is_(True)).order_by(Movil.nombre)).all()
        return templates.TemplateResponse(
            request,
            "operadores.html",
            {
                "user": user,
                "operadores": operadores,
                "moviles": moviles,
                "error": f"Ya existe un operador con el numero {whatsapp_numero} ({duplicado.nombre}).",
            },
            status_code=400,
        )

    db.add(
        Operator(
            nombre=nombre, whatsapp_numero=whatsapp_numero, tipo=tipo, movil_id=int(movil_id) if movil_id else None
        )
    )
    db.commit()
    return RedirectResponse(volver_a(request, "/config/operadores"), status_code=303)


@router.post("/config/operadores/{operador_id}/toggle")
def toggle_operador(
    operador_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    operador = db.get(Operator, operador_id)
    if operador is not None:
        if operador.activo:
            es_responsable = db.scalar(
                select(Movil).where(Movil.responsable_operador_id == operador.id, Movil.activo.is_(True))
            )
            if es_responsable is not None:
                mensaje = f"No se puede desactivar a {operador.nombre}: es responsable del movil {es_responsable.numero}."
                return RedirectResponse(
                    con_parametros(volver_a(request, "/config/operadores"), error=mensaje), status_code=303
                )
        operador.activo = not operador.activo
        db.commit()
    return RedirectResponse(volver_a(request, "/config/operadores"), status_code=303)


@router.get("/config/operadores/{operador_id}/editar")
def editar_operador_form(
    operador_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    operador = obtener_o_redirigir(db, Operator, operador_id, "/config/operadores")
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
    user: PanelUser = Depends(require_admin),
):
    operador = obtener_o_redirigir(db, Operator, operador_id, "/config/operadores")
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

    duplicado = _operador_con_el_mismo_celular(db, whatsapp_numero, excluir_id=operador.id)
    if duplicado is not None:
        return templates.TemplateResponse(
            request,
            "editar_operador.html",
            {
                "user": user,
                "operador": operador,
                "moviles": moviles,
                "error": f"Ya existe otro operador con el numero {whatsapp_numero} ({duplicado.nombre}).",
            },
            status_code=400,
        )

    operador.nombre = nombre
    operador.whatsapp_numero = whatsapp_numero
    operador.tipo = tipo
    operador.movil_id = int(movil_id) if movil_id else None
    db.commit()
    return RedirectResponse(volver_a(request, "/config/operadores"), status_code=303)


@router.get("/config/moviles")
def list_moviles(
    request: Request, error: str = "", db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    moviles = db.scalars(select(Movil).order_by(Movil.id)).all()
    operadores = db.scalars(select(Operator).where(Operator.activo.is_(True)).order_by(Operator.nombre)).all()
    return templates.TemplateResponse(
        request, "moviles.html", {"user": user, "moviles": moviles, "operadores": operadores, "error": error or None}
    )


@router.get("/config/moviles/exportar")
def moviles_exportar(db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)):
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
    return respuesta_xlsx(["Numero", "Nombre", "Descripcion", "Responsable", "Estado"], filas, "moviles.xlsx")


@router.post("/config/moviles")
def create_movil(
    request: Request,
    numero: str = Form(...),
    nombre: str = Form(...),
    descripcion: str = Form(""),
    responsable_operador_id: str = Form(""),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    error = None
    if existe_duplicado(db, Movil.numero, numero):
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
    return RedirectResponse(volver_a(request, "/config/moviles"), status_code=303)


@router.post("/config/moviles/{movil_id}/toggle")
def toggle_movil(
    movil_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    movil = db.get(Movil, movil_id)
    if movil is not None:
        movil.activo = not movil.activo
        db.commit()
    return RedirectResponse(volver_a(request, "/config/moviles"), status_code=303)


@router.get("/config/moviles/{movil_id}/editar")
def editar_movil_form(
    movil_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    movil = obtener_o_redirigir(db, Movil, movil_id, "/config/moviles")
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
    user: PanelUser = Depends(require_admin),
):
    movil = obtener_o_redirigir(db, Movil, movil_id, "/config/moviles")
    operadores = db.scalars(select(Operator).where(Operator.activo.is_(True)).order_by(Operator.nombre)).all()

    error = None
    if existe_duplicado(db, Movil.numero, numero, exclude_id=movil.id):
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
    return RedirectResponse(volver_a(request, "/config/moviles"), status_code=303)


@router.get("/config/cuentas")
def list_cuentas(request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)):
    cuentas = db.scalars(select(BankAccount).order_by(BankAccount.id)).all()
    return templates.TemplateResponse(request, "cuentas.html", {"user": user, "cuentas": cuentas, "error": None})


@router.get("/config/cuentas/exportar")
def cuentas_exportar(db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)):
    cuentas = db.scalars(select(BankAccount).order_by(BankAccount.id)).all()
    filas = [[c.banco, c.numero_cuenta, c.alias, c.moneda] for c in cuentas]
    return respuesta_xlsx(["Banco", "Numero de cuenta", "Alias", "Moneda"], filas, "cuentas.xlsx")


@router.post("/config/cuentas")
def create_cuenta(
    request: Request,
    banco: str = Form(...),
    numero_cuenta: str = Form(...),
    alias: str = Form(...),
    moneda: str = Form("ARS"),
    db: Session = Depends(get_db),
    user: PanelUser = Depends(require_admin),
):
    db.add(BankAccount(banco=banco, numero_cuenta=numero_cuenta, alias=alias, moneda=moneda))
    db.commit()
    return RedirectResponse(volver_a(request, "/config/cuentas"), status_code=303)


@router.get("/config/cuentas/{cuenta_id}/editar")
def editar_cuenta_form(
    cuenta_id: int, request: Request, db: Session = Depends(get_db), user: PanelUser = Depends(require_admin)
):
    cuenta = obtener_o_redirigir(db, BankAccount, cuenta_id, "/config/cuentas")
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
    user: PanelUser = Depends(require_admin),
):
    cuenta = obtener_o_redirigir(db, BankAccount, cuenta_id, "/config/cuentas")

    cuenta.banco = banco
    cuenta.numero_cuenta = numero_cuenta
    cuenta.alias = alias
    cuenta.moneda = moneda
    db.commit()
    return RedirectResponse(volver_a(request, "/config/cuentas"), status_code=303)


