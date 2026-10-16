"""Ingreso de operadores a la interfaz web (/m): login con celular + PIN, PIN
temporal, bloqueo por intentos, sesiones y su separacion del panel."""

from datetime import timedelta

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
from app.zona_horaria import ahora_argentina

CELULAR = "+54 9 379 450-0000"
PIN = "482915"


def _cliente(*, web_habilitado: bool = True, pin_temporal: bool = False, activo: bool = True):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    fabrica = sessionmaker(bind=engine)

    def override_get_db():
        with fabrica() as session:
            yield session

    app.dependency_overrides[sesion.get_db] = override_get_db
    app.dependency_overrides[panel.get_db] = override_get_db
    with fabrica() as db:
        db.add(
            Operator(
                nombre="Ana",
                whatsapp_numero=CELULAR,
                activo=activo,
                web_habilitado=web_habilitado,
                pin_hash=hash_password(PIN),
                pin_temporal=pin_temporal,
            )
        )
        db.add(PanelUser(nombre="Admin", email="admin@concilia.test", password_hash=hash_password("secreta123")))
        db.commit()
    return TestClient(app), fabrica


def teardown_function():
    app.dependency_overrides.clear()


def _login(client, celular: str = "3794500000", pin: str = PIN):
    return client.post("/m/login", data={"celular": celular, "pin": pin}, follow_redirects=False)


def test_login_con_celular_en_cualquier_formato_y_pin():
    client, fabrica = _cliente()

    respuesta = _login(client, celular="379 4500000")

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/m/"
    cookie = respuesta.headers["set-cookie"]
    assert "concilia_operador=" in cookie
    assert "HttpOnly" in cookie and "Path=/m" in cookie and "SameSite=lax" in cookie
    assert "Secure" not in cookie  # http local
    inicio = client.get("/m/")
    assert inicio.status_code == 200
    assert "Hola, Ana" in inicio.text
    with fabrica() as db:
        guardada = db.query(SesionOperador).one()
        assert guardada.token_hash not in cookie  # en la base, solo el hash


def test_cookie_secure_detras_del_proxy_https():
    client, _ = _cliente()

    respuesta = client.post(
        "/m/login",
        data={"celular": "3794500000", "pin": PIN},
        headers={"x-forwarded-proto": "https"},
        follow_redirects=False,
    )

    assert "Secure" in respuesta.headers["set-cookie"]


def test_errores_de_login_son_genericos():
    client, _ = _cliente()

    pin_incorrecto = _login(client, pin="111222")
    celular_inexistente = _login(client, celular="3794999999")

    assert pin_incorrecto.status_code == celular_inexistente.status_code == 401
    assert "No pudimos ingresar con esos datos" in pin_incorrecto.text
    assert "No pudimos ingresar con esos datos" in celular_inexistente.text


def test_sin_acceso_web_o_inactivo_no_entra():
    for opciones in ({"web_habilitado": False}, {"activo": False}):
        client, _ = _cliente(**opciones)
        assert _login(client).status_code == 401, opciones
        teardown_function()


def test_bloqueo_despues_de_varios_intentos_fallidos():
    client, fabrica = _cliente()

    for _ in range(5):
        assert _login(client, pin="111222").status_code == 401
    bloqueado = _login(client)  # PIN correcto, pero bloqueado

    assert bloqueado.status_code == 401
    with fabrica() as db:
        ana = db.query(Operator).one()
        assert ana.bloqueado_hasta is not None
        ana.bloqueado_hasta = ahora_argentina() - timedelta(minutes=1)  # paso el bloqueo
        db.commit()
    assert _login(client).status_code == 303


def test_pin_temporal_obliga_a_cambiarlo_antes_de_seguir():
    client, fabrica = _cliente(pin_temporal=True)

    login = _login(client)
    assert login.headers["location"] == "/m/cambiar-pin"
    assert client.get("/m/", follow_redirects=False).headers["location"] == "/m/cambiar-pin"

    obvio = client.post("/m/cambiar-pin", data={"pin_nuevo": "111111", "pin_repetido": "111111"})
    assert obvio.status_code == 400
    distintos = client.post("/m/cambiar-pin", data={"pin_nuevo": "583920", "pin_repetido": "583921"})
    assert distintos.status_code == 400

    ok = client.post("/m/cambiar-pin", data={"pin_nuevo": "583920", "pin_repetido": "583920"}, follow_redirects=False)
    assert ok.headers["location"] == "/m/"
    assert client.get("/m/").status_code == 200
    with fabrica() as db:
        assert db.query(Operator).one().pin_temporal is False


def test_cambiar_pin_voluntario_pide_el_actual():
    client, _ = _cliente()
    _login(client)

    sin_actual = client.post("/m/cambiar-pin", data={"pin_actual": "000001", "pin_nuevo": "583920", "pin_repetido": "583920"})
    assert sin_actual.status_code == 400
    con_actual = client.post(
        "/m/cambiar-pin", data={"pin_actual": PIN, "pin_nuevo": "583920", "pin_repetido": "583920"}, follow_redirects=False
    )
    assert con_actual.status_code == 303


def test_logout_revoca_la_sesion():
    client, fabrica = _cliente()
    _login(client)
    cookie = client.cookies.get("concilia_operador")

    client.post("/m/logout")

    with fabrica() as db:
        assert db.query(SesionOperador).one().revocada is True
    client.cookies.set("concilia_operador", cookie, path="/m")
    assert client.get("/m/", follow_redirects=False).headers["location"] == "/m/login"


def test_resetear_pin_o_quitar_acceso_corta_las_sesiones():
    client, fabrica = _cliente()
    _login(client)
    assert client.get("/m/").status_code == 200

    with fabrica() as db:
        ana = db.query(Operator).one()
        pin = sesion.generar_pin_temporal(db, ana)
        db.commit()
        assert len(pin) == 6 and ana.pin_temporal
    assert client.get("/m/", follow_redirects=False).headers["location"] == "/m/login"

    _login(client, pin=pin)
    with fabrica() as db:
        db.query(Operator).one().web_habilitado = False
        db.commit()
    assert client.get("/m/cambiar-pin", follow_redirects=False).headers["location"] == "/m/login"


def test_sesion_vencida_y_vencimiento_deslizante():
    client, fabrica = _cliente()
    _login(client)
    with fabrica() as db:
        guardada = db.query(SesionOperador).one()
        guardada.expira_en = ahora_argentina() + timedelta(minutes=5)
        db.commit()

    client.get("/m/")  # usarla la extiende
    with fabrica() as db:
        assert db.query(SesionOperador).one().expira_en > ahora_argentina() + timedelta(days=29)
        db.query(SesionOperador).one().expira_en = ahora_argentina() - timedelta(seconds=1)
        db.commit()
    assert client.get("/m/", follow_redirects=False).headers["location"] == "/m/login"


def test_sesion_de_panel_y_de_operador_no_se_mezclan():
    client, _ = _cliente()
    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    assert client.get("/m/", follow_redirects=False).headers["location"] == "/m/login"

    otro, _ = TestClient(app), None
    _login(otro)
    assert otro.get("/config/operadores", follow_redirects=False).headers["location"] == "/login"


def test_paginas_de_operador_con_cabeceras_de_seguridad():
    client, _ = _cliente()

    pagina = client.get("/m/login")

    assert pagina.headers["cache-control"] == "no-store"
    assert pagina.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in pagina.headers["content-security-policy"]
    assert client.get("/m/static/m.css").headers.get("cache-control") != "no-store"


def test_login_desde_otro_sitio_es_rechazado():
    client, _ = _cliente()

    respuesta = client.post(
        "/m/login", data={"celular": "3794500000", "pin": PIN}, headers={"origin": "https://otro-sitio.test"}
    )

    assert respuesta.status_code == 403
