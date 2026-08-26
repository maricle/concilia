from sqlalchemy import Enum as SqlEnum
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _engine_url(database_url: str) -> str:
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


engine = create_engine(
    _engine_url(get_settings().database_url),
    connect_args={"check_same_thread": False} if get_settings().database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _ensure_enum_values() -> None:
    """create_all() crea un tipo ENUM de Postgres si es nuevo, pero nunca lo altera
    si ya existe -- y este proyecto no tiene Alembic. Sin esto, agregarle un valor
    a un StrEnum de models.py (ej. un ConversationState nuevo) rompe en produccion
    con "invalid input value for enum" hasta que alguien corra el ALTER TYPE a mano.
    ADD VALUE IF NOT EXISTS (Postgres 12+) lo autorepara en cada arranque, sin
    tocar los valores que ya estaban."""
    if engine.dialect.name != "postgresql":
        return
    inspector = inspect(engine)
    tablas_existentes = set(inspector.get_table_names())
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        for tabla in Base.metadata.tables.values():
            if tabla.name not in tablas_existentes:
                continue
            for columna in tabla.columns:
                if not isinstance(columna.type, SqlEnum) or not columna.type.native_enum:
                    continue
                for valor in columna.type.enums:
                    # ADD VALUE no admite bind params (es DDL); tipo_nombre y valor
                    # salen siempre de las clases StrEnum del propio codigo, nunca
                    # de un dato externo.
                    conn.execute(text(f"ALTER TYPE {columna.type.name} ADD VALUE IF NOT EXISTS '{valor}'"))


def create_tables() -> None:
    Base.metadata.create_all(engine)
    _ensure_enum_values()
