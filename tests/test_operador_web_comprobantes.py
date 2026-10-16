"""Carga de comprobantes desde la interfaz web (/m): mismas reglas y mismo
borrador que el chat."""

from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import servicios
from app.auth import hash_password
from app.conversation import ConversationService
from app.db import Base
from app.main import app
from app.models import (
    BankAccount,
    ConversationState,
    Movement,
    Movil,
    Operator,
    RecordState,
    Reparto,
    WhatsAppConversation,
)
from app.operador_web import comprobantes, sesion
from app.operador_web.archivos import ArchivoInvalido, validar_comprobante

PIN = "482915"
ANA = "3794500000"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200
PDF = b"%PDF-1.7\n" + b"0" * 200


def _transfer(numero_operacion: str = "OP-1") -> servicios.ExtractedTransfer:
    return servicios.ExtractedTransfer(
        Decimal("15000.00"), datetime(2026, 10, 16, 11, 30), numero_operacion, banco_emisor="Galicia",
        cuenta_receptora="empresa.mp", titular="Juan Perez",
    )


@pytest.fixture
def entorno(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    fabrica = sessionmaker(bind=engine)

    def override_get_db():
        with fabrica() as session:
            yield session

    app.dependency_overrides[sesion.get_db] = override_get_db
    with fabrica() as db:
        db.add_all(
            [
                Operator(nombre="Ana", whatsapp_numero=ANA, web_habilitado=True, pin_hash=hash_password(PIN)),
                Operator(nombre="Beto", whatsapp_numero="3794511111", web_habilitado=True, pin_hash=hash_password(PIN)),
            ]
        )
        db.add(BankAccount(banco="Mercado Pago", numero_cuenta="1", alias="empresa.mp"))
        db.commit()
        db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
        db.commit()

    lecturas: list[bytes] = []

    def extraccion(content_type, contenido):
        lecturas.append(contenido)
        return entorno_estado["transfer"]

    entorno_estado = {"transfer": _transfer(), "lecturas": lecturas, "fabrica": fabrica}
    monkeypatch.setattr(comprobantes, "extract_transfer", extraccion)
    monkeypatch.setattr(comprobantes, "save_comprobante_archivo", lambda **kwargs: None)
    yield entorno_estado
    app.dependency_overrides.clear()


def _logueado(celular: str = ANA) -> TestClient:
    client = TestClient(app)
    client.post("/m/login", data={"celular": celular, "pin": PIN})
    return client


def _abrir_salida(fabrica, numero: int = 5) -> None:
    with fabrica() as db:
        servicios.crear_salida(db, db.get(Operator, 1), db.get(Movil, 1), numero)
        db.commit()


def _subir(client, contenido: bytes = JPEG, nombre: str = "foto.jpg"):
    # Llegan los dos campos del formulario: camara (vacio) y archivo elegido.
    return client.post(
        "/m/comprobante",
        files=[("archivo", ("", b"", "application/octet-stream")), ("archivo", (nombre, contenido, "image/jpeg"))],
        follow_redirects=False,
    )


def test_flujo_completo_con_salida_abierta(entorno):
    fabrica = entorno["fabrica"]
    _abrir_salida(fabrica)
    client = _logueado()

    subida = _subir(client)
    assert subida.status_code == 303
    revisar = client.get(subida.headers["location"]).text
    assert "$15.000,00" in revisar and "empresa.mp" in revisar and "Salida Nº <strong>5</strong>" in revisar
    assert 'name="monto"' not in revisar  # datos extraidos en solo lectura

    registrado = client.post(subida.headers["location"].replace("revisar", "registrar"), data={"tipo": "factura", "numero": "12345"})

    assert "Comprobante registrado" in registrado.text
    assert "Factura: <strong>12345</strong>" in registrado.text
    with fabrica() as db:
        movimiento = db.query(Movement).one()
        assert (movimiento.estado_registro, movimiento.origen, movimiento.movil_id) == (RecordState.CONFIRMADO, "web", 1)
        assert movimiento.reparto_id == db.query(Reparto).one().id
        assert db.get(WhatsAppConversation, ANA).estado == ConversationState.ESPERANDO_COMPROBANTE


def test_numero_de_factura_invalido_vuelve_a_pedir(entorno):
    _abrir_salida(entorno["fabrica"])
    client = _logueado()
    revisar = _subir(client).headers["location"]

    respuesta = client.post(revisar.replace("revisar", "registrar"), data={"tipo": "factura", "numero": "12 34"})

    assert respuesta.status_code == 400
    assert "Elegí Factura o Cuenta" in respuesta.text


def test_sin_salida_pide_el_numero_y_la_crea_en_el_movil_asignado(entorno):
    fabrica = entorno["fabrica"]
    with fabrica() as db:
        db.get(Operator, 1).movil_id = 1
        db.commit()
    client = _logueado()
    revisar = _subir(client).headers["location"]
    pantalla = client.get(revisar).text
    assert 'name="numero_salida"' in pantalla and 'name="movil_id"' not in pantalla

    sin_numero = client.post(revisar.replace("revisar", "registrar"), data={"tipo": "cuenta", "numero": "987"})
    assert sin_numero.status_code == 400
    with fabrica() as db:
        # La web nunca deja la conversacion en un paso del chat que ella no usa.
        assert db.get(WhatsAppConversation, ANA).estado == ConversationState.ESPERANDO_TIPO_FACTURA_CUENTA

    registrado = client.post(revisar.replace("revisar", "registrar"), data={"tipo": "cuenta", "numero": "987", "numero_salida": "7"})
    assert "Salida Nº 7" in registrado.text and "salida nueva" in registrado.text


def test_sin_salida_ni_movil_pide_tambien_el_movil(entorno):
    client = _logueado()
    revisar = _subir(client).headers["location"]
    assert 'name="movil_id"' in client.get(revisar).text

    registrado = client.post(
        revisar.replace("revisar", "registrar"), data={"tipo": "factura", "numero": "1", "numero_salida": "8", "movil_id": "1"}
    )

    assert "Salida Nº 8" in registrado.text
    with entorno["fabrica"]() as db:
        assert db.get(Operator, 1).movil_id == 1


def test_movil_asignado_con_otra_salida_avisa_y_conserva_lo_cargado(entorno):
    fabrica = entorno["fabrica"]
    with fabrica() as db:
        servicios.crear_salida(db, db.get(Operator, 2), db.get(Movil, 1), 4)
        db.get(Operator, 1).movil_id = 1
        db.commit()
    client = _logueado()
    revisar = _subir(client).headers["location"]

    respuesta = client.post(revisar.replace("revisar", "registrar"), data={"tipo": "factura", "numero": "55", "numero_salida": "9"})

    assert "ya tiene abierta la Salida Nº 4" in respuesta.text
    assert 'value="55"' in respuesta.text
    assert "Salida Nº 4" in client.post(
        revisar.replace("revisar", "registrar"), data={"tipo": "factura", "numero": "55", "numero_salida": "4"}
    ).text


def test_comprobante_ilegible_ofrece_otra_foto(entorno):
    entorno["transfer"] = None
    client = _logueado()

    respuesta = _subir(client)

    assert respuesta.status_code == 422
    assert "Sacar otra foto" in respuesta.text and "administrador" in respuesta.text
    with entorno["fabrica"]() as db:
        assert db.query(Movement).count() == 0


def test_error_de_la_api_al_leer(entorno, monkeypatch):
    def caida(content_type, contenido):
        raise RuntimeError("API caida")

    monkeypatch.setattr(comprobantes, "extract_transfer", caida)
    respuesta = _subir(_logueado())

    assert respuesta.status_code == 502
    assert "Probá de nuevo" in respuesta.text


def test_duplicado_avisa_antes_de_registrar(entorno):
    with entorno["fabrica"]() as db:
        db.add(Movement(operador_id=2, monto=Decimal("1"), fecha_transaccion=datetime(2026, 10, 1), numero_operacion="OP-1"))
        db.commit()

    respuesta = _subir(_logueado())

    assert respuesta.status_code == 409
    assert "Ya existe un comprobante con ese número de operación" in respuesta.text


def test_borrador_pendiente_no_acepta_otra_subida_ni_gasta_otra_lectura(entorno):
    client = _logueado()
    primera = _subir(client).headers["location"]

    assert client.get("/m/comprobante/nuevo", follow_redirects=False).headers["location"] == primera
    segunda = _subir(client)

    assert segunda.headers["location"] == primera
    assert len(entorno["lecturas"]) == 1
    inicio = client.get("/m/").text
    assert "Tenés un movimiento pendiente" in inicio and "Continuar" in inicio


def test_descartar_borra_el_borrador(entorno):
    client = _logueado()
    revisar = _subir(client).headers["location"]

    respuesta = client.post(revisar.replace("revisar", "descartar"), follow_redirects=False)

    assert respuesta.headers["location"] == "/m/"
    with entorno["fabrica"]() as db:
        assert db.query(Movement).count() == 0
        assert db.get(WhatsAppConversation, ANA).estado == ConversationState.ESPERANDO_COMPROBANTE


def test_borrador_empezado_por_chat_con_factura_precargada_se_termina_en_la_web(entorno):
    fabrica = entorno["fabrica"]
    _abrir_salida(fabrica)
    with fabrica() as db:
        ConversationService(db).start_transfer(ANA, _transfer(), None, caption="F 777")
        borrador_id = db.query(Movement).one().id
    client = _logueado()

    pantalla = client.get(f"/m/comprobante/{borrador_id}/revisar").text

    assert 'value="factura" required checked' in pantalla and 'value="777"' in pantalla
    registrado = client.post(f"/m/comprobante/{borrador_id}/registrar", data={"tipo": "factura", "numero": "777"})
    assert "Comprobante registrado" in registrado.text


def test_borrador_empezado_en_la_web_se_termina_por_chat(entorno):
    _abrir_salida(entorno["fabrica"])
    _subir(_logueado())

    with entorno["fabrica"]() as db:
        respuesta = ConversationService(db).handle_text(ANA, "C 4455")
        assert respuesta.startswith("Comprobante registrado correctamente.")
        assert db.query(Movement).one().origen == "web"


def test_borrador_de_otro_operador_da_404(entorno):
    revisar = _subir(_logueado()).headers["location"]
    beto = _logueado("3794511111")

    assert beto.get(revisar).status_code == 404
    assert beto.post(revisar.replace("revisar", "registrar"), data={"tipo": "factura", "numero": "1"}).status_code == 404
    assert beto.post(revisar.replace("revisar", "descartar")).status_code == 404
    assert beto.get("/m/comprobante/999/revisar").status_code == 404


def test_archivo_que_no_es_comprobante_se_rechaza(entorno):
    client = _logueado()

    texto = _subir(client, b"hola, esto no es una foto", "foto.jpg")
    heic = _subir(client, b"\x00\x00\x00\x18ftypheic" + b"0" * 100, "IMG.HEIC")

    assert texto.status_code == 400 and "Solo se pueden subir fotos" in texto.text
    assert heic.status_code == 400 and "HEIC" in heic.text
    assert entorno["lecturas"] == []


def test_validacion_de_archivos_por_contenido():
    assert validar_comprobante(JPEG, 10).content_type == "image/jpeg"
    assert validar_comprobante(PDF, 10).content_type == "application/pdf"
    assert validar_comprobante(b"\x89PNG\r\n\x1a\n" + b"0" * 10, 10).content_type == "image/png"
    assert validar_comprobante(b"RIFF\x00\x00\x00\x00WEBPVP8 ", 10).content_type == "image/webp"
    assert validar_comprobante(PDF, 10).nombre.startswith("web-")
    with pytest.raises(ArchivoInvalido, match="demasiado grande"):
        validar_comprobante(b"%PDF-" + b"0" * (1024 * 1024 + 1), 1)
    with pytest.raises(ArchivoInvalido):
        validar_comprobante(b"", 10)


def test_mis_comprobantes_solo_propios_confirmados_y_del_dia(entorno):
    with entorno["fabrica"]() as db:
        for operador_id, numero, monto, fecha, estado in [
            (1, "OP-A", "1000", datetime(2026, 10, 16, 9, 15), RecordState.CONFIRMADO),
            (1, "OP-B", "2500.50", datetime(2026, 10, 16, 12, 0), RecordState.CONFIRMADO),
            (1, "OP-C", "999", datetime(2026, 10, 16, 13, 0), RecordState.PENDIENTE_CONFIRMACION),
            (1, "OP-D", "50", datetime(2026, 10, 15, 13, 0), RecordState.CONFIRMADO),
            (2, "OP-E", "70", datetime(2026, 10, 16, 13, 0), RecordState.CONFIRMADO),
        ]:
            db.add(
                Movement(
                    operador_id=operador_id, numero_operacion=numero, monto=Decimal(monto), fecha_transaccion=fecha,
                    estado_registro=estado, factura_o_cuenta_tipo="factura", factura_o_cuenta_numero=numero,
                )
            )
        db.commit()
    client = _logueado()

    pagina = client.get("/m/comprobantes", params={"fecha": "2026-10-16"}).text

    assert "OP-A" in pagina and "OP-B" in pagina
    assert "OP-C" not in pagina and "OP-D" not in pagina and "OP-E" not in pagina
    assert "Total del día: <strong>$3.500,50</strong> (2 comprobantes)" in pagina
    assert "OP-D" in client.get("/m/comprobantes", params={"fecha": "2026-10-15"}).text
