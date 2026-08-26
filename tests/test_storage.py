from app import storage
from app.db import Base, SessionLocal, engine
from app.models import ComprobanteArchivo


def setup_function():
    Base.metadata.create_all(engine)


def test_save_and_get_comprobante_archivo_roundtrip():
    archivo_id = storage.save_comprobante_archivo(
        nombre_archivo="comprobante.jpg",
        content_type="image/jpeg",
        contenido=b"fake-image-bytes",
    )

    try:
        archivo = storage.get_comprobante_archivo(archivo_id)
        assert archivo is not None
        assert archivo.contenido == b"fake-image-bytes"
        assert archivo.content_type == "image/jpeg"
        assert archivo.nombre_archivo == "comprobante.jpg"
    finally:
        with SessionLocal() as session:
            archivo = session.get(ComprobanteArchivo, archivo_id)
            if archivo is not None:
                session.delete(archivo)
                session.commit()


def test_get_comprobante_archivo_returns_none_when_missing():
    assert storage.get_comprobante_archivo(999999999) is None
