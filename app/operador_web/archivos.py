"""Validacion de los archivos de comprobante que suben los operadores desde la web.

Se valida por contenido (los primeros bytes del archivo), no por la extension ni
por el Content-Type que manda el navegador, que se pueden falsear. El nombre
interno se genera aca: nunca se usa el nombre original del archivo."""

import secrets
from dataclasses import dataclass


@dataclass
class ArchivoValido:
    contenido: bytes
    content_type: str
    nombre: str


class ArchivoInvalido(Exception):
    """El mensaje es para mostrarselo al operador."""


_HEIC_MARCAS = (b"ftypheic", b"ftypheix", b"ftyphevc", b"ftypheim", b"ftypheis", b"ftypmif1", b"ftypmsf1")


def _tipo_por_contenido(contenido: bytes) -> tuple[str, str] | None:
    if contenido.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if contenido.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if contenido[:4] == b"RIFF" and contenido[8:12] == b"WEBP":
        return "image/webp", "webp"
    if contenido.startswith(b"%PDF-"):
        return "application/pdf", "pdf"
    return None


def validar_comprobante(contenido: bytes, max_mb: int) -> ArchivoValido:
    if not contenido:
        raise ArchivoInvalido("No llegó ningún archivo. Sacá la foto o elegí el archivo de nuevo.")
    if len(contenido) > max_mb * 1024 * 1024:
        raise ArchivoInvalido(f"El archivo es demasiado grande (máximo {max_mb} MB).")
    tipo = _tipo_por_contenido(contenido)
    if tipo is None:
        if contenido[4:12] in _HEIC_MARCAS:
            raise ArchivoInvalido(
                "Ese formato de foto (HEIC, del iPhone) no se puede leer. Sacá la foto desde acá con "
                "\"Sacar foto\", o elegí una captura de pantalla."
            )
        raise ArchivoInvalido("Solo se pueden subir fotos (JPG, PNG o WebP) o PDF.")
    content_type, extension = tipo
    return ArchivoValido(contenido, content_type, f"web-{secrets.token_hex(8)}.{extension}")
