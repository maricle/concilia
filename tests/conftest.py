"""Aisla la suite del entorno real.

Varios tests (webhook, telegram, storage) usan el engine/SessionLocal de la app,
que se crea al importar app.db con el DATABASE_URL de ese momento. Sin esto, correr
pytest con un .env apuntando a otra base (ej. la de produccion) escribiria ahi los
operadores y comprobantes de prueba. Las variables de entorno tienen prioridad
sobre el .env, y este modulo se importa antes que cualquier test -- por eso se
setean aca arriba, a nivel de modulo, y no en un fixture.

Las credenciales externas se pisan con valores inocuos por el mismo motivo: un test
que se olvide de mockear un envio no tiene que poder llegar a Meta, Telegram ni
Anthropic con un token real.
"""

import os
import tempfile
from pathlib import Path

import pytest

_DIRECTORIO_TEMPORAL = tempfile.TemporaryDirectory(prefix="concilia-tests-", ignore_cleanup_errors=True)

os.environ.update(
    {
        "DATABASE_URL": f"sqlite:///{Path(_DIRECTORIO_TEMPORAL.name, 'concilia_test.db').as_posix()}",
        "META_APP_SECRET": "",
        "META_VERIFY_TOKEN": "token-de-test",
        "WA_ACCESS_TOKEN": "token-de-test",
        "WA_PHONE_ID": "0",
        "ANTHROPIC_API_KEY": "clave-de-test",
        "TELEGRAM_BOT_TOKEN": "token-de-test",
        "TELEGRAM_WEBHOOK_SECRET": "",
        "SESSION_SECRET": "secreto-de-test",
    }
)


@pytest.fixture(scope="session", autouse=True)
def _esquema_de_la_base_temporal():
    # Recien aca se importa la app, ya con el entorno de arriba aplicado.
    from app.db import Base, engine

    Base.metadata.create_all(engine)
    yield
    engine.dispose()
