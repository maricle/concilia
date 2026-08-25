from datetime import datetime
from functools import lru_cache

from sqlalchemy import DateTime, Engine, LargeBinary, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import get_settings


class TestStoreBase(DeclarativeBase):
    pass


class ComprobanteArchivoPrueba(TestStoreBase):
    """Archivo original de un comprobante, guardado en Turso solo para pruebas.

    Reemplaza temporalmente el storage S3/R2 del spec tecnico mientras no esta
    implementado; no usar esta tabla en produccion.
    """

    __tablename__ = "comprobantes_archivo_prueba"

    id: Mapped[int] = mapped_column(primary_key=True)
    numero_operacion: Mapped[str | None] = mapped_column(String(120), index=True)
    nombre_archivo: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    contenido: Mapped[bytes] = mapped_column(LargeBinary)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


def _turso_engine_url(database_url: str) -> str:
    if database_url.startswith("libsql://"):
        return "sqlite+libsql://" + database_url.removeprefix("libsql://")
    return database_url


@lru_cache
def get_test_store_engine() -> Engine:
    settings = get_settings()
    if not settings.turso_database_url:
        raise RuntimeError(
            "TURSO_DATABASE_URL no esta configurado. "
            "Copia la URL con `turso db show <db>` y el token con `turso db tokens create <db>` a tu .env."
        )
    connect_args = {"auth_token": settings.turso_auth_token, "secure": True} if settings.turso_auth_token else {}
    return create_engine(_turso_engine_url(settings.turso_database_url), connect_args=connect_args)


def create_test_store_tables() -> None:
    TestStoreBase.metadata.create_all(get_test_store_engine())


def _test_store_session() -> Session:
    return sessionmaker(bind=get_test_store_engine())()


def save_comprobante_prueba(
    nombre_archivo: str,
    content_type: str,
    contenido: bytes,
    numero_operacion: str | None = None,
) -> int:
    create_test_store_tables()
    with _test_store_session() as session:
        archivo = ComprobanteArchivoPrueba(
            nombre_archivo=nombre_archivo,
            content_type=content_type,
            contenido=contenido,
            numero_operacion=numero_operacion,
        )
        session.add(archivo)
        session.commit()
        session.refresh(archivo)
        return archivo.id


def get_comprobante_prueba(archivo_id: int) -> ComprobanteArchivoPrueba | None:
    with _test_store_session() as session:
        return session.scalar(select(ComprobanteArchivoPrueba).where(ComprobanteArchivoPrueba.id == archivo_id))
