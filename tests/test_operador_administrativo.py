"""Operadores de tipo Administrativo: no reparten, asi que cargan comprobantes
sin salida ni movil y no inician ni cierran salidas, ni por el chat (WhatsApp y
Telegram usan el mismo servicio) ni desde el celular (/m)."""

from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import servicios
from app.auth import hash_password
from app.conversation import ConversationService, ExtractedTransfer
from app.db import Base
from app.main import app
from app.models import BankAccount, Movement, Movil, Operator, RecordState, Reparto
from app.operador_web import comprobantes, sesion

PIN = "482915"
CELULAR = "3794500000"


def _transfer() -> ExtractedTransfer:
    return ExtractedTransfer(Decimal("1250.50"), datetime(2026, 11, 10, 9, 30), "OP-1", cuenta_receptora="empresa.mp")


def _base(db: Session) -> None:
    db.add_all(
        [
            Operator(
                nombre="Laura", whatsapp_numero=CELULAR, tipo="Administrativo",
                web_habilitado=True, pin_hash=hash_password(PIN),
            ),
            BankAccount(banco="Mercado Pago", numero_cuenta="1", alias="empresa.mp"),
        ]
    )
    db.commit()
    db.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
    db.commit()


def test_es_administrativo():
    assert servicios.es_administrativo(Operator(nombre="a", whatsapp_numero="1", tipo="Administrativo"))
    assert servicios.es_administrativo(Operator(nombre="a", whatsapp_numero="1", tipo=" administrativo "))
    assert not servicios.es_administrativo(Operator(nombre="a", whatsapp_numero="1", tipo="Reparto"))


# ----------------------------------------------------------------------- chat


def test_chat_registra_sin_pedir_salida_ni_movil():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = Session(engine)
    _base(db)
    service = ConversationService(db)

    service.start_transfer(CELULAR, _transfer())
    service.handle_text(CELULAR, "factura")
    final = service.handle_text(CELULAR, "FAC-9")

    assert final == "Comprobante registrado correctamente.\nFactura: FAC-9 · $1250.50"
    movimiento = db.query(Movement).one()
    assert movimiento.estado_registro == RecordState.CONFIRMADO
    assert (movimiento.reparto_id, movimiento.movil_id) == (None, None)
    assert db.query(Reparto).count() == 0


def test_chat_no_deja_iniciar_ni_cerrar_salidas():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = Session(engine)
    _base(db)
    service = ConversationService(db)

    assert service.handle_text(CELULAR, "inicio salida 5 movil M-01") == servicios.ADMINISTRATIVO_SIN_SALIDAS_TEXTO
    assert service.handle_text(CELULAR, "cerrar salida") == servicios.ADMINISTRATIVO_SIN_SALIDAS_TEXTO
    assert db.query(Reparto).count() == 0


# --------------------------------------------------------------------- celular


@pytest.fixture
def web(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    fabrica = sessionmaker(bind=engine)

    def override_get_db():
        with fabrica() as session:
            yield session

    app.dependency_overrides[sesion.get_db] = override_get_db
    with fabrica() as db:
        _base(db)
    monkeypatch.setattr(comprobantes, "extract_transfer", lambda content_type, contenido: _transfer())
    monkeypatch.setattr(comprobantes, "save_comprobante_archivo", lambda **kwargs: None)
    client = TestClient(app)
    client.post("/m/login", data={"usuario": CELULAR, "pin": PIN})
    yield client, fabrica
    app.dependency_overrides.clear()


def test_celular_sin_botones_de_salida(web):
    client, _ = web
    inicio = client.get("/m/").text
    assert "Cargar comprobante" in inicio
    assert "Iniciar salida" not in inicio and "Sin salida abierta" not in inicio

    # Aunque entre por la direccion, no puede iniciar una salida.
    assert servicios.ADMINISTRATIVO_SIN_SALIDAS_TEXTO in client.get("/m/salida/iniciar").text
    respuesta = client.post("/m/salida/iniciar", data={"numero": "5", "movil_id": "1"})
    assert servicios.ADMINISTRATIVO_SIN_SALIDAS_TEXTO in respuesta.text


def test_celular_carga_sin_numero_de_salida_ni_movil(web):
    client, fabrica = web
    subida = client.post(
        "/m/comprobante",
        files=[("archivo", ("foto.jpg", b"\xff\xd8\xff\xe0" + b"0" * 200, "image/jpeg"))],
        follow_redirects=False,
    )
    revisar = client.get(subida.headers["location"]).text
    assert 'name="numero_salida"' not in revisar and 'name="movil_id"' not in revisar

    registrado = client.post(
        subida.headers["location"].replace("revisar", "registrar"), data={"tipo": "cuenta", "numero": "987"}
    )

    assert "Comprobante registrado" in registrado.text
    assert "Salida Nº" not in registrado.text
    with fabrica() as db:
        movimiento = db.query(Movement).one()
        assert movimiento.estado_registro == RecordState.CONFIRMADO
        assert (movimiento.reparto_id, movimiento.movil_id, movimiento.origen) == (None, None, "web")
