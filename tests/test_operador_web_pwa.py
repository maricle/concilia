"""Capa PWA de /m: manifest, service worker y compatibilidad con la CSP."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
PLANTILLAS_M = Path("app/templates/m")


def test_manifest_instalable_con_alcance_m():
    respuesta = client.get("/m/manifest.webmanifest")

    assert respuesta.headers["content-type"].startswith("application/manifest+json")
    manifest = respuesta.json()
    assert (manifest["start_url"], manifest["scope"], manifest["display"]) == ("/m/", "/m/", "standalone")
    tamanos = {(i["sizes"], i["purpose"]) for i in manifest["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= tamanos
    for icono in manifest["icons"]:
        assert client.get(icono["src"]).status_code == 200


def test_service_worker_en_m_y_sin_cachear_paginas():
    respuesta = client.get("/m/sw.js")

    assert respuesta.status_code == 200
    assert "javascript" in respuesta.headers["content-type"]
    codigo = respuesta.text
    assert "cache.put" not in codigo  # solo el app shell que se precarga al instalar
    assert "/m/static/sin-conexion.html" in codigo
    assert client.get("/m/static/sin-conexion.html").status_code == 200


def test_las_paginas_enlazan_el_manifest_y_el_script():
    pagina = client.get("/m/login").text

    assert '<link rel="manifest" href="/m/manifest.webmanifest">' in pagina
    assert '<script src="/m/static/m.js" defer></script>' in pagina


def test_plantillas_de_m_sin_javascript_en_linea():
    # La CSP de /m (script-src 'self') bloquearia scripts en linea y atributos
    # on*: el JS va solo en /m/static/m.js.
    for plantilla in PLANTILLAS_M.glob("*.html"):
        texto = plantilla.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", texto), plantilla.name
        assert not re.search(r"\son[a-z]+\s*=", texto), plantilla.name
        assert "style=" not in texto, plantilla.name
