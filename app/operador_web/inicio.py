"""Pantalla de inicio de /m: estado de la salida y accesos a cada accion."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from .. import servicios
from ..models import Operator
from .comun import templates
from .sesion import get_db, require_operator

router = APIRouter()


@router.get("/m/")
def inicio(request: Request, operador: Operator = Depends(require_operator), db: Session = Depends(get_db)):
    salida = servicios.reparto_abierto_del_operador(db, operador.id)
    return templates.TemplateResponse(request, "m/inicio.html", {"operador": operador, "salida": salida})
