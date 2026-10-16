"""Piezas compartidas por las pantallas de /m."""

from datetime import timedelta

from fastapi.templating import Jinja2Templates

from ..estaticos import version_estatico
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
