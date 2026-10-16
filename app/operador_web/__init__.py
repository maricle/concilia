"""Interfaz web movil (PWA) para operadores, bajo /m: un tercer canal de carga
de comprobantes, ademas de WhatsApp y Telegram, que usa las mismas reglas
(app/servicios.py) y el mismo borrador por operador."""

from fastapi import APIRouter

from .acceso import router as _router_acceso
from .inicio import router as _router_inicio
from .sesion import CambioDePinPendiente, OperadorNoAutenticado

router = APIRouter()
router.include_router(_router_acceso)
router.include_router(_router_inicio)

__all__ = ["CambioDePinPendiente", "OperadorNoAutenticado", "router"]
