"""Panel: acceso web de los operadores (/config/operadores)."""

import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import panel
from app.auth import hash_password
from app.db import Base
from app.main import app
from app.models import Operator, PanelUser, SesionOperador
from app.operador_web import sesion


def _entorno(rol: str = "Administrador"):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    fabrica = sessionmaker(bind=engine)

    def override_get_db():
        with fabrica() as session:
            yield session

    app.dependency_overrides[panel.get_db] = override_get_db
    app.dependency_overrides[sesion.get_db] = override_get_db
    with fabrica() as db:
        db.add(PanelUser(nombre="Admin", email="admin@concilia.test", password_hash=hash_password("secreta123"), rol=rol))
        db.add_all([Operator(nombre="Ana", whatsapp_numero="3794500000"), Operator(nombre="Corto", whatsapp_numero="12345")])
        db.commit()
    admin = TestClient(app)
    admin.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    return admin, fabrica


def teardown_function():
    app.dependency_overrides.clear()


def _pin_mostrado(respuesta) -> str:
    return re.search(r'letter-spacing: \.2em;">(\d{6})<', respuesta.text).group(1)


def test_generar_pin_lo_muestra_una_vez_y_el_operador_entra_y_lo_cambia():
    admin, fabrica = _entorno()
    assert "No habilitado" in admin.get("/config/operadores").text

    respuesta = admin.post("/config/operadores/1/pin")

    assert respuesta.headers["cache-control"] == "no-store"
    assert "una sola vez" in respuesta.text
    pin = _pin_mostrado(respuesta)
    with fabrica() as db:
        ana = db.get(Operator, 1)
        assert ana.web_habilitado and ana.pin_temporal and ana.pin_hash != pin
    assert "PIN temporal pendiente" in admin.get("/config/operadores").text

    operador = TestClient(app)
    login = operador.post("/m/login", data={"celular": "3794500000", "pin": pin}, follow_redirects=False)
    assert login.headers["location"] == "/m/cambiar-pin"
    operador.post("/m/cambiar-pin", data={"pin_nuevo": "583920", "pin_repetido": "583920"})
    assert "Hola, Ana" in operador.get("/m/").text
    assert ">Habilitado<" in admin.get("/config/operadores").text


def test_resetear_pin_quitar_acceso_cerrar_sesiones_y_desactivar_cortan_las_sesiones():
    admin, fabrica = _entorno()
    for accion in ("pin", "acceso-web", "cerrar-sesiones", "toggle"):
        pin = _pin_mostrado(admin.post("/config/operadores/1/pin"))
        with fabrica() as db:
            db.get(Operator, 1).activo = True
            db.commit()
        operador = TestClient(app)
        operador.post("/m/login", data={"celular": "3794500000", "pin": pin})
        operador.post("/m/cambiar-pin", data={"pin_nuevo": "583920", "pin_repetido": "583920"})
        assert operador.get("/m/").status_code == 200, accion

        admin.post(f"/config/operadores/1/{accion}")

        assert operador.get("/m/", follow_redirects=False).headers["location"] == "/m/login", accion
        with fabrica() as db:
            assert all(s.revocada for s in db.query(SesionOperador).all()), accion
            db.get(Operator, 1).web_habilitado = True
            db.commit()


def test_celular_corto_no_puede_tener_acceso_web():
    admin, fabrica = _entorno()

    habilitar = admin.post("/config/operadores/2/acceso-web", follow_redirects=False)
    pin = admin.post("/config/operadores/2/pin", follow_redirects=False)

    assert "10" in habilitar.headers["location"] and "10" in pin.headers["location"]
    with fabrica() as db:
        assert db.get(Operator, 2).web_habilitado is False


def test_solo_administradores_manejan_el_acceso_web():
    admin, _ = _entorno(rol="Recaudador")

    for accion in ("pin", "acceso-web", "cerrar-sesiones"):
        assert admin.post(f"/config/operadores/1/{accion}").status_code == 403, accion
