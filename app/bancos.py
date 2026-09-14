"""Icono y color para mostrar cada banco en /conciliaciones y el home.
BankAccount.banco es texto libre (sin enum), asi que esto es solo un mapeo
cosmetico de nombres conocidos a un icono de Font Awesome (o una imagen del
logo real, si existe en app/static/img/) y un color hex -- cualquier banco no
listado usa el default generico, asi que cargar una cuenta nueva en
/config/cuentas nunca rompe la pantalla."""

_DEFAULT = {"icono": "fa-university", "color": "#6c757d", "imagen": None}

_ICONOS_BANCO: dict[str, dict[str, str]] = {
    "galicia": {"icono": "fa-university", "color": "#EE3124", "imagen": "galicia.png"},
    "mercado pago": {"icono": "fa-handshake", "color": "#00A9E0", "imagen": "mercado-pago-icon.png"},
    "naranja x": {"icono": "fa-credit-card", "color": "#FF7A00"},
    "uala": {"icono": "fa-bolt", "color": "#5A3EF5"},
    "ualá": {"icono": "fa-bolt", "color": "#5A3EF5"},
    "santander": {"icono": "fa-university", "color": "#EC0000"},
    "bbva": {"icono": "fa-university", "color": "#004481"},
    "nacion": {"icono": "fa-landmark", "color": "#6BAED6", "imagen": "banconacion.png"},
    "nación": {"icono": "fa-landmark", "color": "#6BAED6", "imagen": "banconacion.png"},
    "banco nacion": {"icono": "fa-landmark", "color": "#6BAED6", "imagen": "banconacion.png"},
    "banco nación": {"icono": "fa-landmark", "color": "#6BAED6", "imagen": "banconacion.png"},
    "provincia": {"icono": "fa-landmark", "color": "#00954A"},
    "brubank": {"icono": "fa-mobile-alt", "color": "#7B2FF7"},
    "icbc": {"icono": "fa-university", "color": "#C8102E", "imagen": "icbc.png"},
}


def icono_banco(banco: str) -> dict[str, str | None]:
    datos = _ICONOS_BANCO.get(banco.strip().lower(), {})
    return {**_DEFAULT, **datos}
