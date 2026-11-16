"""Login y logout del panel."""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import verify_password
from ..models import (
    PanelUser,
)
from .comun import get_db, get_logged_in_user, templates

router = APIRouter()
logger = logging.getLogger(__name__)

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


