"""Copia todos los datos de la base de produccion (Postgres) a un archivo SQLite
local, para poder probar el panel contra datos reales sin arriesgar nada en
produccion (solo hace SELECT del lado de origen, nunca escribe ahi).

No usa pg_dump/psql -- esta maquina no los tiene instalados -- sino que lee cada
tabla por SQLAlchemy Core y la vuelca directo a un SQLite nuevo con el mismo
esquema (Base.metadata), respetando el orden de dependencias entre tablas.

Uso (necesita las credenciales de produccion inyectadas por Railway, nunca las
pega este script ni las imprime):

    railway run python scripts/backup_a_sqlite.py [archivo_salida.db]

El archivo de salida por defecto es concilia_backup.db (se sobreescribe si ya
existe). Point later local runs at it with:

    $env:DATABASE_URL = "sqlite:///./concilia_backup.db"
    uvicorn app.main:app --reload
"""

import sys
from pathlib import Path

from sqlalchemy import create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: F401 -- registra todas las tablas en Base.metadata
from app.config import get_settings
from app.db import Base, _engine_url

_LOTE = 1000


def main() -> None:
    origen_url = get_settings().database_url
    if origen_url.startswith("sqlite"):
        print(
            "DATABASE_URL apunta a SQLite, no a produccion -- corre esto con "
            "'railway run python scripts/backup_a_sqlite.py' para que use las "
            "credenciales de produccion."
        )
        raise SystemExit(1)

    destino_path = Path(sys.argv[1] if len(sys.argv) > 1 else "concilia_backup.db")
    destino_path.unlink(missing_ok=True)

    origen = create_engine(_engine_url(origen_url))
    destino = create_engine(f"sqlite:///{destino_path}", connect_args={"check_same_thread": False})

    Base.metadata.create_all(destino)

    with origen.connect() as conn_origen, destino.begin() as conn_destino:
        for tabla in Base.metadata.sorted_tables:
            total = 0
            resultado = conn_origen.execution_options(stream_results=True).execute(select(tabla))
            while True:
                filas = resultado.fetchmany(_LOTE)
                if not filas:
                    break
                conn_destino.execute(tabla.insert(), [dict(fila._mapping) for fila in filas])
                total += len(filas)
            print(f"{tabla.name}: {total} filas")

    print(f"\nListo -- {destino_path.resolve()}")


if __name__ == "__main__":
    main()
