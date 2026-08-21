from sqlalchemy import create_engine
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


def create_tables() -> None:
    Base.metadata.create_all(engine)
