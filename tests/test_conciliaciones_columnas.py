"""Columnas del extracto por banco: la primera vez se eligen en una pantalla y
quedan guardadas; el titular y el CUIT/CUIL del extracto se usan para conciliar."""

import io
from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import panel
from app.auth import hash_password
from app.db import Base
from app.main import app
from app.models import (
    BankAccount,
    FormatoExtracto,
    ImportedStatement,
    Movement,
    Operator,
    PanelUser,
    ReconciliationState,
    RecordState,
    StatementLine,
)

ENCABEZADOS_GALICIA = [
    "Fecha", "Descripción", "Origen", "Débitos", "Créditos", "Grupo de Conceptos", "Concepto",
    "Número de Comprobante", "Leyendas Adicionales1", "Leyendas Adicionales2", "Saldo",
]


def _extracto_galicia(filas: list[list]) -> bytes:
    """XLSX con un par de filas de titulo antes del encabezado, como exportan
    algunos bancos."""
    libro = Workbook()
    hoja = libro.active
    hoja.append(["Movimientos de cuenta"])
    hoja.append(["Cuenta corriente en pesos"])
    hoja.append(ENCABEZADOS_GALICIA)
    for fila in filas:
        hoja.append(fila)
    salida = io.BytesIO()
    libro.save(salida)
    return salida.getvalue()


FILAS = [
    [datetime(2026, 11, 4), "TRANSF. RECIBIDA", "", None, 1500.5, "Transf", "", "000123", "PEREZ JUAN", "20-12345678-6", 1],
    [datetime(2026, 11, 4), "COMISION", "", 300, None, "Gastos", "", "", "", "", 1],
    [datetime(2026, 11, 4), "TRANSF. RECIBIDA", "", 0, 2000, "Transf", "", "000124", "GOMEZ ANA", "27334455667", 1],
]


def _cliente():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    fabrica = sessionmaker(bind=engine)

    def override_get_db():
        with fabrica() as session:
            yield session

    app.dependency_overrides[panel.get_db] = override_get_db
    with fabrica() as db:
        db.add(PanelUser(nombre="Admin", email="admin@concilia.test", password_hash=hash_password("secreta123")))
        db.add(BankAccount(banco="Galicia", numero_cuenta="1", alias="galicia.empresa"))
        db.add(Operator(nombre="Ana", whatsapp_numero="111"))
        db.commit()
    client = TestClient(app)
    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})
    return client, fabrica


def teardown_function():
    app.dependency_overrides.clear()


def _subir(client, contenido: bytes, **extra):
    return client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-11-04", **extra},
        files={"archivo": ("galicia.xlsx", contenido, "application/octet-stream")},
    )


def _archivo_id(pantalla: str) -> str:
    inicio = pantalla.index('name="archivo_id" value="') + len('name="archivo_id" value="')
    return pantalla[inicio : pantalla.index('"', inicio)]


COLUMNAS_GALICIA = {
    "col_fecha": "Fecha",
    "col_monto": "Créditos",
    "col_referencia": "Número de Comprobante",
    "col_titular": "Leyendas Adicionales1",
    "col_cuit": "Leyendas Adicionales2",
    "col_descripcion": "Descripción",
}


def test_la_primera_vez_pide_las_columnas_con_sugerencias_y_las_guarda():
    client, fabrica = _cliente()

    pantalla = _subir(client, _extracto_galicia(FILAS)).text

    assert "Es la primera vez que se importa un extracto de <strong>Galicia</strong>" in pantalla
    assert '<option value="Créditos" selected>' in pantalla
    assert '<option value="Leyendas Adicionales1" selected>' in pantalla
    assert '<option value="Leyendas Adicionales2" selected>' in pantalla
    assert "PEREZ JUAN" in pantalla  # vista previa de las primeras filas
    with fabrica() as db:
        assert db.query(ImportedStatement).count() == 0

    respuesta = client.post(
        "/conciliaciones/importar/columnas",
        data={"archivo_id": _archivo_id(pantalla), "cuenta_bancaria_id": "1", "fecha": "2026-11-04", **COLUMNAS_GALICIA},
    )

    assert "Se importaron 2 transacciones nuevas." in respuesta.text  # el debito no entra
    with fabrica() as db:
        formato = db.query(FormatoExtracto).one()
        assert (formato.banco, formato.col_monto, formato.col_cuit) == ("Galicia", "Créditos", "Leyendas Adicionales2")
        lineas = db.query(StatementLine).order_by(StatementLine.id).all()
        assert [(linea.monto, linea.titular, linea.cuit, linea.referencia) for linea in lineas] == [
            (Decimal("1500.50"), "PEREZ JUAN", "20123456786", "000123"),
            (Decimal("2000.00"), "GOMEZ ANA", "27334455667", "000124"),
        ]

    # La segunda vez importa directo con las columnas guardadas.
    otra = FILAS + [[datetime(2026, 11, 4), "TRANSF. RECIBIDA", "", None, 50, "", "", "000125", "LOPEZ", "", 1]]
    segunda = _subir(client, _extracto_galicia(otra))
    assert "Se importaron 1 transaccion nueva." in segunda.text
    assert 'name="col_fecha"' not in segunda.text


def test_revisar_columnas_muestra_las_guardadas():
    client, fabrica = _cliente()
    with fabrica() as db:
        db.add(FormatoExtracto(banco="Galicia", col_fecha="Fecha", col_monto="Créditos", col_cuit="Leyendas Adicionales2"))
        db.commit()

    pantalla = _subir(client, _extracto_galicia(FILAS), elegir_columnas="1").text

    assert "Es la primera vez" not in pantalla
    assert '<option value="Leyendas Adicionales2" selected>' in pantalla
    # Las guardadas mandan sobre las sugeridas: el titular no estaba elegido.
    assert '<option value="Leyendas Adicionales1" selected>' not in pantalla


def test_columnas_que_no_se_pueden_leer_vuelven_a_la_pantalla_con_el_error():
    client, fabrica = _cliente()
    pantalla = _subir(client, _extracto_galicia(FILAS)).text

    respuesta = client.post(
        "/conciliaciones/importar/columnas",
        data={
            "archivo_id": _archivo_id(pantalla),
            "cuenta_bancaria_id": "1",
            "fecha": "2026-11-04",
            **COLUMNAS_GALICIA,
            "col_fecha": "Descripción",
        },
    )

    assert respuesta.status_code == 400
    assert "No se pudo interpretar la fecha" in respuesta.text
    with fabrica() as db:
        assert db.query(FormatoExtracto).count() == 0
        assert db.query(StatementLine).count() == 0


def _movimiento(db, **datos) -> Movement:
    movimiento = Movement(
        operador_id=1,
        cuenta_bancaria_id=1,
        fecha_transaccion=datetime(2026, 11, 4, 10, 0),
        estado_registro=RecordState.CONFIRMADO,
        **datos,
    )
    db.add(movimiento)
    db.commit()
    return movimiento


def test_el_cuit_concilia_aunque_el_nombre_no_coincida_y_lo_marca_si_es_otro():
    client, fabrica = _cliente()
    with fabrica() as db:
        db.add(
            FormatoExtracto(
                banco="Galicia",
                col_fecha="Fecha",
                col_monto="Créditos",
                col_titular="Leyendas Adicionales1",
                col_cuit="Leyendas Adicionales2",
            )
        )
        db.commit()
        # Sin numero de operacion: solo fecha y monto. El nombre del comprobante
        # no se parece al del banco, pero el CUIT es el mismo.
        mismo_cuit = _movimiento(db, monto=Decimal("1500.50"), titular="Juan Ignacio", cuit="20123456786").id
        otro_cuit = _movimiento(db, monto=Decimal("2000"), titular="Gomez Ana", cuit="20123456786").id

    _subir(client, _extracto_galicia(FILAS))

    with fabrica() as db:
        assert db.get(Movement, mismo_cuit).estado_conciliacion == ReconciliationState.CONCILIADO
        # Mismo nombre, pero el CUIT es de otra persona: queda para revisar.
        assert db.get(Movement, otro_cuit).estado_conciliacion == ReconciliationState.CON_DIFERENCIA
