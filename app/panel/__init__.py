"""Panel de administracion. Cada area vive en su modulo y aporta su propio
router; aca se juntan en el `router` que monta main.py."""

from fastapi import APIRouter

from .auth import router as _router_auth
from .comprobantes import router as _router_comprobantes
from .comun import (
    NotAuthenticated,
    RedirectOnMissing,
    SinPermiso,
    es_admin,
    get_db,
    require_admin,
    require_user,
    templates,
)
from .conciliaciones import router as _router_conciliaciones
from .configuracion import router as _router_configuracion
from .resumen import router as _router_resumen
from .salidas import router as _router_salidas

router = APIRouter()
router.include_router(_router_auth)
router.include_router(_router_configuracion)
router.include_router(_router_resumen)
router.include_router(_router_comprobantes)
router.include_router(_router_salidas)
router.include_router(_router_conciliaciones)

__all__ = [
    "router",
    "NotAuthenticated",
    "RedirectOnMissing",
    "SinPermiso",
    "es_admin",
    "get_db",
    "require_admin",
    "require_user",
    "templates",
]
