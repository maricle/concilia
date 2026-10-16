"""Pantalla de inicio de /m."""

from fastapi import APIRouter, Depends, Request

from ..models import Operator
from .comun import templates
from .sesion import require_operator

router = APIRouter()


@router.get("/m/")
def inicio(request: Request, operador: Operator = Depends(require_operator)):
    return templates.TemplateResponse(request, "m/inicio.html", {"operador": operador})
