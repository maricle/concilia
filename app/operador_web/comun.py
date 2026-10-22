"""Piezas compartidas por las pantallas de /m."""

from datetime import timedelta

from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import servicios
from ..estaticos import version_estatico
from ..models import Movil
from ..panel.comun import url_actual

# Instancia propia: las pantallas de /m usan su plantilla base (m/base.html) y no
# comparten globals de Jinja con el panel.
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["url_actual"] = url_actual
templates.env.globals["version_estatico"] = version_estatico
templates.env.globals["timedelta"] = timedelta
templates.env.globals["ESTADOS"] = {
    "pendiente": "Pendiente de conciliar",
    "conciliado": "Conciliado",
    "conciliado_manualmente": "Conciliado",
    "con_diferencia": "Con diferencia",
}


def moviles_activos(db: Session) -> list[Movil]:
    return list(db.scalars(select(Movil).where(Movil.activo.is_(True)).order_by(Movil.numero)))


def texto_pedir_movil(numero_salida: int, varias: bool = False) -> str:
    if varias:
        return f"Hay más de una Salida Nº {numero_salida} abierta. Elegí en qué móvil estás."
    return f"La Salida Nº {numero_salida} no está abierta. Elegí el móvil para iniciarla."


def texto_salida_ocupada(salida: servicios.SalidaEnOtroMovil | servicios.MovilConOtraSalida) -> str:
    if isinstance(salida, servicios.MovilConOtraSalida):
        return (
            f"El móvil {salida.movil.numero} ya tiene abierta la Salida Nº {salida.reparto.numero_reparto}. "
            "Si vas en ese móvil, indicá ese número de salida."
        )
    return f"La Salida Nº {salida.reparto.numero_reparto} está abierta en el móvil {salida.reparto.movil.numero}. Revisá el número."
