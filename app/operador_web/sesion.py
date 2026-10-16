"""Autenticacion de operadores en la interfaz web (/m): celular + PIN de 6
digitos, bloqueo por intentos fallidos y sesiones en la base.

Es independiente de la sesion del panel (require_user): una sesion de panel no
da acceso a /m ni al reves. Nunca se loguean PIN ni tokens.
"""

import hashlib
import re
import secrets
from datetime import timedelta

import bcrypt
from fastapi import Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..auth import hash_password, verify_password
from ..canales import buscar_operador_por_telefono
from ..config import get_settings
from ..db import SessionLocal
from ..models import Operator, SesionOperador
from ..zona_horaria import ahora_argentina

COOKIE_SESION = "concilia_operador"
RUTA_COOKIE = "/m"
LARGO_PIN = 6
# Hash de un PIN cualquiera, para gastar el mismo tiempo cuando el celular no
# existe y no revelar por la demora si hay un operador con ese numero.
_HASH_DE_RELLENO = bcrypt.hashpw(b"000000", bcrypt.gensalt()).decode()


class OperadorNoAutenticado(Exception):
    """No hay sesion valida de operador: redirigir al login de /m."""


class CambioDePinPendiente(Exception):
    """El operador entro con un PIN temporal: tiene que cambiarlo antes de seguir."""


def get_db():
    with SessionLocal() as session:
        yield session


# ------------------------------------------------------------------------ PIN


def pin_valido(pin: str) -> str | None:
    """Mensaje de error si el PIN nuevo no sirve, o None si esta bien."""
    if not re.fullmatch(rf"\d{{{LARGO_PIN}}}", pin):
        return f"El PIN tiene que tener {LARGO_PIN} numeros."
    if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
        return "Elegí un PIN menos obvio (no todos iguales ni seguidos)."
    return None


def generar_pin_temporal(db: Session, operador: Operator) -> str:
    """Genera un PIN nuevo para que el administrador se lo pase al operador
    (se muestra una sola vez), obliga a cambiarlo en el primer ingreso y revoca
    todas las sesiones abiertas. Sin commit."""
    while True:
        pin = f"{secrets.randbelow(10**LARGO_PIN):0{LARGO_PIN}d}"
        if pin_valido(pin) is None:
            break
    operador.pin_hash = hash_password(pin)
    operador.pin_temporal = True
    operador.intentos_fallidos = 0
    operador.bloqueado_hasta = None
    revocar_sesiones(db, operador.id)
    return pin


def cambiar_pin(operador: Operator, pin_nuevo: str) -> None:
    operador.pin_hash = hash_password(pin_nuevo)
    operador.pin_temporal = False


# ---------------------------------------------------------------------- login


def autenticar(db: Session, celular: str, pin: str) -> Operator | None:
    """El operador si celular y PIN son correctos, esta activo, tiene acceso web
    y no esta bloqueado; si no, None (sin decir por que: el mensaje al usuario es
    siempre el mismo). Cuenta los intentos fallidos y bloquea al llegar al
    maximo. Hace commit."""
    settings = get_settings()
    ahora = ahora_argentina()
    operador = buscar_operador_por_telefono(db, celular)
    if operador is None or not operador.web_habilitado or operador.pin_hash is None:
        verify_password(pin, _HASH_DE_RELLENO)
        return None
    if operador.bloqueado_hasta is not None and operador.bloqueado_hasta > ahora:
        verify_password(pin, _HASH_DE_RELLENO)
        return None
    if not verify_password(pin, operador.pin_hash):
        operador.intentos_fallidos = (operador.intentos_fallidos or 0) + 1
        if operador.intentos_fallidos >= settings.web_login_max_attempts:
            operador.bloqueado_hasta = ahora + timedelta(minutes=settings.web_login_lock_minutes)
            operador.intentos_fallidos = 0
        db.commit()
        return None
    operador.intentos_fallidos = 0
    operador.bloqueado_hasta = None
    db.commit()
    return operador


# ------------------------------------------------------------------- sesiones


def _hash_token(token: str) -> str:
    # El token es aleatorio de 256 bits: un hash rapido alcanza (no hay
    # diccionario posible), y permite buscarlo por indice.
    return hashlib.sha256(token.encode()).hexdigest()


def abrir_sesion(db: Session, operador: Operator, user_agent: str | None) -> str:
    """Crea la sesion y devuelve el token en claro, para la cookie. Hace commit."""
    token = secrets.token_urlsafe(32)
    ahora = ahora_argentina()
    db.add(
        SesionOperador(
            operador_id=operador.id,
            token_hash=_hash_token(token),
            creada_en=ahora,
            ultimo_uso=ahora,
            expira_en=ahora + timedelta(days=get_settings().web_session_days),
            user_agent=(user_agent or "")[:255] or None,
        )
    )
    db.commit()
    return token


def revocar_sesion(db: Session, token: str | None) -> None:
    if token:
        db.execute(update(SesionOperador).where(SesionOperador.token_hash == _hash_token(token)).values(revocada=True))
        db.commit()


def revocar_sesiones(db: Session, operador_id: int) -> None:
    """Cierra todas las sesiones web del operador (sin commit)."""
    db.execute(
        update(SesionOperador)
        .where(SesionOperador.operador_id == operador_id, SesionOperador.revocada.is_(False))
        .values(revocada=True)
    )


def _es_https(request: Request) -> bool:
    # Railway termina TLS en su proxy y le pasa http a la app: el protocolo real
    # viene en X-Forwarded-Proto.
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def poner_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        COOKIE_SESION,
        token,
        max_age=get_settings().web_session_days * 24 * 3600,
        path=RUTA_COOKIE,
        httponly=True,
        secure=_es_https(request),
        samesite="lax",
    )


def borrar_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_SESION, path=RUTA_COOKIE)


def operador_de_la_sesion(request: Request, db: Session) -> Operator | None:
    """El operador de la cookie si la sesion sigue valida (y la extiende:
    vencimiento deslizante). Una sesion de un operador desactivado o sin acceso
    web deja de valer aunque no se haya revocado."""
    token = request.cookies.get(COOKIE_SESION)
    if not token:
        return None
    sesion = db.scalar(select(SesionOperador).where(SesionOperador.token_hash == _hash_token(token)))
    ahora = ahora_argentina()
    if sesion is None or sesion.revocada or sesion.expira_en <= ahora:
        return None
    operador = sesion.operador
    if not operador.activo or not operador.web_habilitado:
        return None
    sesion.ultimo_uso = ahora
    sesion.expira_en = ahora + timedelta(days=get_settings().web_session_days)
    db.commit()
    return operador


def operador_logueado(request: Request, db: Session = Depends(get_db)) -> Operator:
    """Sesion valida, aunque el PIN siga siendo temporal (para /m/cambiar-pin)."""
    operador = operador_de_la_sesion(request, db)
    if operador is None:
        raise OperadorNoAutenticado()
    return operador


def require_operator(operador: Operator = Depends(operador_logueado)) -> Operator:
    """Para todas las pantallas de /m: sesion valida y PIN ya cambiado."""
    if operador.pin_temporal:
        raise CambioDePinPendiente()
    return operador
