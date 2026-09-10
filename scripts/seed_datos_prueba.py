"""Carga datos ficticios en la base local (SQLite) para poder probar el panel y
el flujo de conversacion sin depender de datos reales. Idempotente: correrlo
varias veces no duplica nada, solo asegura que los datos de prueba existan.

Uso: python scripts/seed_datos_prueba.py
"""

import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.auth import hash_password
from app.db import SessionLocal, create_tables
from app.models import (
    BankAccount,
    Movement,
    Movil,
    Operator,
    PanelUser,
    RecordState,
    ReconciliationState,
    Reparto,
    RepartoOperador,
)

ADMIN_EMAIL = "admin@concilia.test"
ADMIN_PASSWORD = "test1234"


def _get_or_create(session, modelo, filtro: dict, **extra):
    instancia = session.scalar(select(modelo).filter_by(**filtro))
    if instancia is None:
        instancia = modelo(**filtro, **extra)
        session.add(instancia)
        session.flush()
    return instancia


def main() -> None:
    create_tables()
    with SessionLocal() as session:
        admin = session.scalar(select(PanelUser).where(PanelUser.email == ADMIN_EMAIL))
        if admin is None:
            admin = PanelUser(nombre="Admin de prueba", email=ADMIN_EMAIL, rol="Administrador")
            session.add(admin)
        admin.password_hash = hash_password(ADMIN_PASSWORD)
        admin.activo = True
        session.flush()

        ana = _get_or_create(
            session, Operator, {"whatsapp_numero": "5493794000001"}, nombre="Ana Operadora", tipo="Reparto"
        )
        beto = _get_or_create(
            session, Operator, {"whatsapp_numero": "5493794000002"}, nombre="Beto Operador", tipo="Reparto"
        )

        movil_1 = _get_or_create(
            session, Movil, {"numero": "M-01"}, nombre="Camion 1", responsable_operador_id=ana.id
        )
        movil_2 = _get_or_create(
            session, Movil, {"numero": "M-02"}, nombre="Camion 2", responsable_operador_id=beto.id
        )

        cuenta_mp = _get_or_create(
            session,
            BankAccount,
            {"alias": "empresa.mp.prueba"},
            banco="Mercado Pago",
            numero_cuenta="0000003100045397444527",
        )
        cuenta_galicia = _get_or_create(
            session,
            BankAccount,
            {"alias": "empresa.galicia.prueba"},
            banco="Galicia",
            numero_cuenta="0070077120000014194391",
        )

        session.commit()

        hoy = date.today()
        reparto_abierto = _get_or_create(
            session,
            Reparto,
            {"movil_id": movil_1.id, "fecha": hoy, "numero_reparto": 1},
            hora_inicio=datetime.now() - timedelta(hours=2),
        )
        session.flush()
        _get_or_create(session, RepartoOperador, {"reparto_id": reparto_abierto.id, "operador_id": ana.id})

        reparto_cerrado = _get_or_create(
            session,
            Reparto,
            {"movil_id": movil_2.id, "fecha": hoy - timedelta(days=1), "numero_reparto": 1},
            hora_inicio=datetime.now() - timedelta(days=1, hours=8),
            hora_fin=datetime.now() - timedelta(days=1, hours=2),
        )
        session.flush()
        _get_or_create(session, RepartoOperador, {"reparto_id": reparto_cerrado.id, "operador_id": beto.id})
        session.commit()

        _get_or_create(
            session,
            Movement,
            {"numero_operacion": "OP-PRUEBA-1"},
            operador_id=ana.id,
            monto=Decimal("15000.50"),
            fecha_transaccion=datetime.now() - timedelta(hours=1),
            banco_emisor="Mercado Pago",
            titular="Cliente de Prueba SA",
            cuenta_bancaria_id=cuenta_mp.id,
            movil_id=movil_1.id,
            reparto_id=reparto_abierto.id,
            estado_registro=RecordState.CONFIRMADO,
            estado_conciliacion=ReconciliationState.PENDIENTE,
        )
        _get_or_create(
            session,
            Movement,
            {"numero_operacion": "OP-PRUEBA-2"},
            operador_id=beto.id,
            monto=Decimal("8320.00"),
            fecha_transaccion=datetime.now() - timedelta(days=1, hours=3),
            banco_emisor="Galicia",
            titular="Otro Cliente SRL",
            cuenta_bancaria_id=cuenta_galicia.id,
            movil_id=movil_2.id,
            reparto_id=reparto_cerrado.id,
            estado_registro=RecordState.CONFIRMADO,
            estado_conciliacion=ReconciliationState.PENDIENTE,
        )
        session.commit()

    print("Datos de prueba listos.")
    print(f"Panel: http://localhost:8000/login  (usuario: {ADMIN_EMAIL} / clave: {ADMIN_PASSWORD})")
    print("Operadores de prueba: Ana (5493794000001), Beto (5493794000002)")


if __name__ == "__main__":
    main()
