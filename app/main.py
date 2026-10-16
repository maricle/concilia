import logging
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import create_tables
from .operador_web import CambioDePinPendiente, OperadorNoAutenticado
from .operador_web import router as operador_web_router
from .panel import NotAuthenticated, RedirectOnMissing, SinPermiso, templates
from .panel import router as panel_router
from .telegram import router as telegram_router
from .webhook import router as webhook_router

logger = logging.getLogger(__name__)


def _avisar_webhooks_sin_verificar() -> None:
    """Sin estos secretos los webhooks aceptan cualquier request (comodo para
    desarrollo local), asi que cualquiera que conozca la URL podria simular
    mensajes de un operador. No se bloquea el arranque para no tirar produccion
    por una variable faltante, pero queda avisado en el log de cada deploy."""
    settings = get_settings()
    if not settings.meta_app_secret:
        logger.warning("META_APP_SECRET vacio: POST /webhook no verifica la firma de Meta.")
    if not settings.telegram_webhook_secret:
        logger.warning("TELEGRAM_WEBHOOK_SECRET vacio: POST /telegram/webhook no verifica el origen.")


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_tables()
    _avisar_webhooks_sin_verificar()
    yield


app = FastAPI(title="Concilia", version="0.1.0", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=get_settings().session_secret)
# Rutas que reciben POST de servidores (Meta, Telegram), no de un navegador.
_RUTAS_SIN_CONTROL_DE_ORIGEN = {"/webhook", "/telegram/webhook"}


def _origen_del_request(request: Request) -> str | None:
    origen = request.headers.get("origin") or request.headers.get("referer")
    if not origen or origen == "null":
        return origen
    return urlsplit(origen).netloc


@app.middleware("http")
async def _bloquear_post_de_otro_sitio(request: Request, call_next):
    """Defensa CSRF por verificacion de origen (OWASP): todo navegador manda
    Origin en un POST, asi que un formulario armado en otro sitio para usar la
    sesion de un administrador llega con un Origin que no es el del panel y se
    rechaza aca. Un POST sin Origin ni Referer no viene de un formulario de otro
    sitio (clientes que no son navegadores, tests) y se deja pasar; la cookie de
    sesion es SameSite=Lax, que ademas ya no viaja en POST cruzados."""
    if request.method == "POST" and request.url.path not in _RUTAS_SIN_CONTROL_DE_ORIGEN:
        origen = _origen_del_request(request)
        hosts_validos = {request.headers.get("host"), request.headers.get("x-forwarded-host")} - {None}
        if origen is not None and origen not in hosts_validos:
            logger.warning(
                "POST %s rechazado por origen: origen=%r hosts=%r", request.url.path, origen, hosts_validos
            )
            return PlainTextResponse("Origen no permitido", status_code=403)
    return await call_next(request)


app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/m/static", StaticFiles(directory="app/static/m"), name="static_m")
app.include_router(webhook_router)
app.include_router(telegram_router)
app.include_router(panel_router)
app.include_router(operador_web_router)

_CSP_OPERADOR = (
    "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; "
    "form-action 'self'; base-uri 'none'"
)


@app.middleware("http")
async def _cabeceras_operador(request: Request, call_next):
    """Cabeceras de seguridad de la interfaz de operadores. Las paginas de /m
    tienen datos del operador: no se guardan en ningun cache (ni el del service
    worker); los estaticos de /m/static si."""
    response = await call_next(request)
    if request.url.path.startswith("/m/"):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = _CSP_OPERADOR
        if not request.url.path.startswith("/m/static/"):
            response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(NotAuthenticated)
def _redirect_to_login(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(OperadorNoAutenticado)
def _redirect_al_login_de_operador(request: Request, exc: OperadorNoAutenticado) -> RedirectResponse:
    return RedirectResponse("/m/login", status_code=303)


@app.exception_handler(CambioDePinPendiente)
def _redirect_a_cambiar_pin(request: Request, exc: CambioDePinPendiente) -> RedirectResponse:
    return RedirectResponse("/m/cambiar-pin", status_code=303)


@app.exception_handler(SinPermiso)
def _sin_permiso(request: Request, exc: SinPermiso):
    return templates.TemplateResponse(request, "sin_permiso.html", {"user": exc.user}, status_code=403)


@app.exception_handler(RedirectOnMissing)
def _redirect_on_missing(request: Request, exc: RedirectOnMissing) -> RedirectResponse:
    return RedirectResponse(exc.redirect_to, status_code=303)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    # Los navegadores lo piden en la raiz aunque la pagina declare otro.
    return FileResponse("app/static/iconos/favicon.ico", media_type="image/x-icon")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
