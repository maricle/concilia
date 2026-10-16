"""Piezas compartidas por las pantallas de /m."""

from fastapi.templating import Jinja2Templates

from ..panel.comun import url_actual

# Instancia propia: las pantallas de /m usan su plantilla base (m/base.html) y no
# comparten globals de Jinja con el panel.
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["url_actual"] = url_actual
