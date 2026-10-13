import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import create_tables
from .panel import NotAuthenticated, RedirectOnMissing
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
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(webhook_router)
app.include_router(telegram_router)
app.include_router(panel_router)


@app.exception_handler(NotAuthenticated)
def _redirect_to_login(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(RedirectOnMissing)
def _redirect_on_missing(request: Request, exc: RedirectOnMissing) -> RedirectResponse:
    return RedirectResponse(exc.redirect_to, status_code=303)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
