from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Movil, Operator


def test_create_all_handles_circular_movil_operador_fk():
    """operadores.movil_id -> moviles.id y moviles.responsable_operador_id ->
    operadores.id son un FK circular entre dos tablas nuevas en una base
    limpia (como en los tests) -- use_alter en operadores.movil_id evita que
    create_all() explote con un CircularDependencyError."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)


def test_operador_y_movil_se_referencian_mutuamente():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = Session(engine)

    operador = Operator(nombre="Ana", whatsapp_numero="5491112345678")
    db.add(operador)
    db.flush()

    movil = Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=operador.id)
    db.add(movil)
    db.flush()

    operador.movil_id = movil.id
    db.commit()

    assert db.get(Operator, operador.id).movil.numero == "M-01"
    assert db.get(Movil, movil.id).responsable.nombre == "Ana"


def test_ensure_indexes_agrega_indices_nuevos_a_tablas_existentes(monkeypatch):
    # create_all no agrega indices a una tabla que ya existe: sin esto, los
    # indices nuevos de models.py nunca llegarian a produccion.
    from sqlalchemy import create_engine, inspect, text

    from app import db

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE lineas_resumen (id INTEGER PRIMARY KEY, resumen_id INTEGER, movimiento_id INTEGER)"))
    monkeypatch.setattr(db, "engine", engine)

    db._ensure_indexes()
    db._ensure_indexes()  # idempotente

    indices = {i["name"] for i in inspect(engine).get_indexes("lineas_resumen")}
    assert {"ix_lineas_resumen_resumen_id", "ix_lineas_resumen_movimiento_id"} <= indices
