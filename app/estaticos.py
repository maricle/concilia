"""Version de los archivos estaticos para las URLs de las plantillas.

Los CSS y JS se enlazan como "/static/css/custom.css?v=<hash del contenido>":
cuando el archivo cambia, cambia la URL y el navegador baja el nuevo en vez de
seguir usando el que tenia en cache. El hash se calcula una vez por proceso
(cada deploy arranca un proceso nuevo con los archivos nuevos)."""

import hashlib
from functools import lru_cache
from pathlib import Path

_RAIZ = Path("app/static")


@lru_cache
def version_estatico(ruta: str) -> str:
    """`ruta` relativa a app/static (ej. "css/custom.css" o "m/m.css")."""
    try:
        return hashlib.sha256((_RAIZ / ruta).read_bytes()).hexdigest()[:10]
    except OSError:
        return "0"
