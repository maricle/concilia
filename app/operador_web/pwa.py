"""Capa PWA de /m: manifest y service worker.

Los dos se sirven bajo /m/ (no desde /m/static/) para que el alcance del
service worker cubra toda la interfaz de operadores."""

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter()

# Iconos: mascota de Demonte (app/static/img/demonte-box.png) sobre fondo blanco.
# Colores: los del panel (sidebar #14141f).
_MANIFEST = {
    "name": "Concilia - Operadores",
    "short_name": "Concilia",
    "description": "Cargá los comprobantes de tus cobros y manejá tu salida.",
    "lang": "es",
    "start_url": "/m/",
    "scope": "/m/",
    "display": "standalone",
    "orientation": "portrait",
    "theme_color": "#14141f",
    "background_color": "#ffffff",
    "icons": [
        {"src": "/m/static/iconos/icono-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/m/static/iconos/icono-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/m/static/iconos/icono-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}


@router.get("/m/manifest.webmanifest")
def manifest() -> JSONResponse:
    return JSONResponse(_MANIFEST, media_type="application/manifest+json")


@router.get("/m/sw.js")
def service_worker() -> FileResponse:
    return FileResponse("app/static/m/sw.js", media_type="application/javascript")
