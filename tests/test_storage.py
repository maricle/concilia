from sqlalchemy import create_engine

from app import storage


def test_turso_engine_url_converts_libsql_scheme():
    assert storage._turso_engine_url("libsql://concilia-test-org.turso.io") == "sqlite+libsql://concilia-test-org.turso.io"


def test_save_and_get_comprobante_prueba_roundtrip(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    monkeypatch.setattr(storage, "get_test_store_engine", lambda: engine)

    archivo_id = storage.save_comprobante_prueba(
        nombre_archivo="comprobante.jpg",
        content_type="image/jpeg",
        contenido=b"fake-image-bytes",
        numero_operacion="OP-1",
    )

    archivo = storage.get_comprobante_prueba(archivo_id)
    assert archivo is not None
    assert archivo.contenido == b"fake-image-bytes"
    assert archivo.numero_operacion == "OP-1"
