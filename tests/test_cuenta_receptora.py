"""Reconocer a que cuenta de la empresa fue la transferencia: por alias, CBU/CVU
o numero de cuenta, completos o parcialmente ocultos en el comprobante."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import BankAccount
from app.servicios import buscar_cuenta_bancaria


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        session.add_all(
            [
                BankAccount(banco="Galicia", numero_cuenta="4019441-7 077-2", cbu="0070077120000014194391", alias="empresa.galicia"),
                BankAccount(banco="Mercado Pago", numero_cuenta="", cbu="0000003100045397444527", alias="empresa.mp"),
                # Cuenta cargada antes de existir el campo CBU: el CBU quedo en numero_cuenta.
                BankAccount(banco="Nacion", numero_cuenta="0110599520000012345678", alias="empresa.nacion"),
            ]
        )
        session.commit()
        yield session


def _banco(db, leido):
    cuenta = buscar_cuenta_bancaria(db, leido)
    return cuenta.banco if cuenta else None


@pytest.mark.parametrize(
    "leido,banco",
    [
        ("EMPRESA.GALICIA", "Galicia"),  # alias
        ("0070 0771 2000 0014 1943 91", "Galicia"),  # CBU completo
        ("0000003100045397444527", "Mercado Pago"),  # CVU completo
        ("CC $ 4019441-7 077-2", "Galicia"),  # numero de cuenta del banco
        ("40194417 0772", "Galicia"),  # mismo numero de cuenta con otro formato
        ("0110599520000012345678", "Nacion"),  # CBU cargado en numero_cuenta (dato viejo)
        ("***4391", "Galicia"),  # CBU oculto, quedan los ultimos digitos
        ("CBU ...14194391", "Galicia"),
        ("0070****4391", "Galicia"),  # principio y final visibles
        ("****444527", "Mercado Pago"),
        ("14194391", "Galicia"),  # final del CBU sin mascara
        ("***391", None),  # muy pocos digitos visibles para decidir
        ("9999****4391", None),  # el principio no coincide
        ("otra.cuenta", None),
        (None, None),
    ],
)
def test_reconoce_la_cuenta_de_destino(db, leido, banco):
    assert _banco(db, leido) == banco


def test_si_el_final_coincide_con_dos_cuentas_no_adivina(db):
    db.add(BankAccount(banco="Santander", numero_cuenta="", cbu="0720000000000000004391", alias="empresa.santander"))
    db.commit()
    assert _banco(db, "***4391") is None
    assert _banco(db, "***14194391") == "Galicia"
