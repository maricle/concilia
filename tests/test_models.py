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
