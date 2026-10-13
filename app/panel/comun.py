"""Piezas compartidas por todas las pantallas del panel: templates y sus
filtros, dependencias de sesion y permisos, excepciones y helpers."""

import io
import logging
from datetime import date, datetime, time, timedelta
from typing import TypeVar
from urllib.parse import urlencode

from fastapi import Depends, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook
from sqlalchemy import and_, select
from sqlalchemy.orm import DeclarativeBase, InstrumentedAttribute, Session

from ..bancos import icono_banco
from ..db import SessionLocal
from ..models import (
    PanelUser,
)

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="app/templates")


def _fecha_transaccion_display(fecha: datetime | None) -> str:
    """La hora solo se muestra si se detecto (fecha.hour/minute != 0): si el
    comprobante no traia hora, fecha_transaccion queda en medianoche y mostrarla
    daria a entender que la hora se leyo del comprobante cuando en realidad no."""
    if fecha is None:
        return "-"
    if fecha.hour or fecha.minute:
        return fecha.strftime("%Y-%m-%d %H:%M")
    return fecha.strftime("%Y-%m-%d")


templates.env.filters["fecha_transaccion"] = _fecha_transaccion_display


def _sort_url(request: Request, campo: str) -> str:
    """URL para el link de un encabezado de columna ordenable: conserva todos los
    filtros/parametros actuales de la pagina, pisando solo sort/order. Si ya se
    esta ordenando por esa misma columna invierte la direccion; si no, arranca
    ascendente."""
    parametros = dict(request.query_params)
    ya_activo = parametros.get("sort") == campo
    nuevo_orden = "desc" if ya_activo and parametros.get("order", "asc") == "asc" else "asc"
    parametros["sort"] = campo
    parametros["order"] = nuevo_orden
    return f"{request.url.path}?{urlencode(parametros)}"


templates.env.globals["sort_url"] = _sort_url
templates.env.globals["icono_banco"] = icono_banco


def del_dia(columna, dia: date):
    """Condicion "columna cae en `dia`" como rango [00:00, 00:00 del dia siguiente).
    Es equivalente a func.date(columna) == dia, pero esa forma obliga a la base a
    calcular la fecha de cada fila y no puede usar el indice de la columna; con
    el rango, las consultas por dia (conciliaciones, resumen) usan el indice."""
    inicio = datetime.combine(dia, time.min)
    return and_(columna >= inicio, columna < inicio + timedelta(days=1))


def url_con(request: Request, **cambios: object) -> str:
    """La URL actual con algunos parametros cambiados (None los saca),
    conservando el resto: filtros, orden, pagina."""
    parametros = dict(request.query_params)
    for clave, valor in cambios.items():
        if valor is None:
            parametros.pop(clave, None)
        else:
            parametros[clave] = str(valor)
    return f"{request.url.path}?{urlencode(parametros)}"


templates.env.globals["url_con"] = url_con


def respuesta_xlsx(headers: list[str], filas: list[list], filename: str) -> Response:
    """Arma un .xlsx descargable a partir de un encabezado y filas de valores --
    usado por todos los botones "Exportar" del panel, en vez de CSV."""
    workbook = Workbook()
    hoja = workbook.active
    hoja.append(headers)
    for fila in filas:
        hoja.append(fila)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return Response(
        content=buffer.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_Model = TypeVar("_Model", bound=DeclarativeBase)


class NotAuthenticated(Exception):
    """Se levanta cuando una ruta protegida no tiene sesion de panel activa."""


class RedirectOnMissing(Exception):
    """Se levanta cuando un registro buscado por id no existe."""

    def __init__(self, redirect_to: str):
        self.redirect_to = redirect_to


def get_db():
    with SessionLocal() as session:
        yield session


def get_logged_in_user(request: Request, db: Session) -> PanelUser | None:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    user = db.get(PanelUser, user_id)
    return user if user is not None and user.activo else None


def require_user(request: Request, db: Session = Depends(get_db)) -> PanelUser:
    user = get_logged_in_user(request, db)
    if user is None:
        raise NotAuthenticated()
    return user


ROLES_PANEL = ("Administrador", "Contador", "Recaudador")
_ROLES_CON_ACCESO_TOTAL = {"administrador", "contador"}


class SinPermiso(Exception):
    """Se levanta cuando un usuario logueado entra a una ruta que su rol no permite."""

    def __init__(self, user: PanelUser):
        self.user = user


def es_admin(user: PanelUser | None) -> bool:
    """Administrador y Contador tienen acceso total. Cualquier otro valor de rol
    (Recaudador, o un texto viejo cargado a mano antes de que el rol fuera una
    lista cerrada) queda con los permisos de Recaudador: puede ver el resumen,
    los comprobantes y las salidas, y cargar comprobantes, pero no corregirlos
    ni eliminarlos, ni entrar a Conciliaciones ni a Configuracion."""
    return user is not None and (user.rol or "").strip().lower() in _ROLES_CON_ACCESO_TOTAL


templates.env.globals["es_admin"] = es_admin


def require_admin(user: PanelUser = Depends(require_user)) -> PanelUser:
    if not es_admin(user):
        raise SinPermiso(user)
    return user


def rol_valido(rol: str) -> str | None:
    """Devuelve el rol con su nombre canonico, o None si no es uno de ROLES_PANEL."""
    for nombre in ROLES_PANEL:
        if rol.strip().lower() == nombre.lower():
            return nombre
    return None


def obtener_o_redirigir(db: Session, model: type[_Model], id_: int, redirect_to: str) -> _Model:
    obj = db.get(model, id_)
    if obj is None:
        raise RedirectOnMissing(redirect_to)
    return obj


def existe_duplicado(db: Session, field: InstrumentedAttribute, value: str, *, exclude_id: int | None = None) -> bool:
    query = select(field.class_).where(field == value)
    if exclude_id is not None:
        query = query.where(field.class_.id != exclude_id)
    return db.scalar(query) is not None


