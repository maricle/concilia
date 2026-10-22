"""Ingreso de operadores a /m: login, logout y cambio de PIN."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import verify_password
from ..models import Operator
from .comun import templates
from .sesion import (
    COOKIE_SESION,
    abrir_sesion,
    autenticar,
    borrar_cookie,
    cambiar_pin,
    get_db,
    operador_de_la_sesion,
    operador_logueado,
    pin_valido,
    poner_cookie,
    revocar_sesion,
)

router = APIRouter()

ERROR_LOGIN = (
    "No pudimos ingresar con esos datos. Revisá el celular y el PIN. "
    "Después de varios intentos fallidos el acceso se bloquea por unos minutos."
)


@router.get("/m/login")
def login_form(request: Request, db: Session = Depends(get_db)):
    if operador_de_la_sesion(request, db) is not None:
        return RedirectResponse("/m/", status_code=303)
    return templates.TemplateResponse(request, "m/login.html", {"error": None, "celular": ""})


@router.post("/m/login")
def login_submit(
    request: Request,
    celular: str = Form(...),
    pin: str = Form(...),
    db: Session = Depends(get_db),
):
    operador = autenticar(db, celular, pin.strip())
    if operador is None:
        return templates.TemplateResponse(
            request, "m/login.html", {"error": ERROR_LOGIN, "celular": celular}, status_code=401
        )
    token = abrir_sesion(db, operador, request.headers.get("user-agent"))
    destino = "/m/cambiar-pin" if operador.pin_temporal else "/m/"
    response = RedirectResponse(destino, status_code=303)
    poner_cookie(response, request, token)
    return response


@router.post("/m/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    revocar_sesion(db, request.cookies.get(COOKIE_SESION))
    response = RedirectResponse("/m/login", status_code=303)
    borrar_cookie(response)
    return response


@router.get("/m/cambiar-pin")
def cambiar_pin_form(request: Request, operador: Operator = Depends(operador_logueado)):
    return templates.TemplateResponse(request, "m/cambiar_pin.html", {"operador": operador, "error": None})


@router.post("/m/cambiar-pin")
def cambiar_pin_submit(
    request: Request,
    pin_actual: str = Form(""),
    pin_nuevo: str = Form(...),
    pin_repetido: str = Form(...),
    operador: Operator = Depends(operador_logueado),
    db: Session = Depends(get_db),
):
    error = None
    # Con PIN temporal recien se entro con el: no se vuelve a pedir.
    if not operador.pin_temporal and not verify_password(pin_actual.strip(), operador.pin_hash or ""):
        error = "El PIN actual no es correcto."
    elif pin_nuevo != pin_repetido:
        error = "Los dos PIN nuevos no coinciden."
    else:
        error = pin_valido(pin_nuevo)
    if error:
        return templates.TemplateResponse(
            request, "m/cambiar_pin.html", {"operador": operador, "error": error}, status_code=400
        )
    cambiar_pin(operador, pin_nuevo)
    db.commit()
    return RedirectResponse("/m/", status_code=303)
