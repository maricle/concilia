from sqlalchemy import select

from .db import SessionLocal
from .models import ComprobanteArchivo


def save_comprobante_archivo(nombre_archivo: str, content_type: str, contenido: bytes) -> int:
    with SessionLocal() as session:
        archivo = ComprobanteArchivo(nombre_archivo=nombre_archivo, content_type=content_type, contenido=contenido)
        session.add(archivo)
        session.commit()
        session.refresh(archivo)
        return archivo.id


def get_comprobante_archivo(archivo_id: int) -> ComprobanteArchivo | None:
    with SessionLocal() as session:
        return session.scalar(select(ComprobanteArchivo).where(ComprobanteArchivo.id == archivo_id))
