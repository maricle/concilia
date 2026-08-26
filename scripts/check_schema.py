"""Compara las tablas/columnas que esperan los modelos de SQLAlchemy contra el
esquema real de la base conectada (DATABASE_URL), y avisa si hay diferencias.

No hay Alembic en este proyecto: Base.metadata.create_all() (ver app/db.py)
crea tablas nuevas al arrancar la app, pero nunca altera una tabla existente
para agregarle/renombrarle columnas. Cada vez que se cambia app/models.py,
correr este script ANTES de deployar para detectar columnas que van a faltar
en produccion (evita el error UndefinedColumn en runtime).

Uso:
    railway run python scripts/check_schema.py   # contra la base de produccion
    python scripts/check_schema.py                # contra la base de DATABASE_URL local

Sale con status 1 si encuentra drift, 0 si el esquema esta al dia.
"""

import sys

from sqlalchemy import inspect

from app.db import Base, engine
from app import models  # noqa: F401  (importa todos los modelos para poblar Base.metadata)


def check_schema() -> bool:
    inspector = inspect(engine)
    tablas_existentes = set(inspector.get_table_names())

    ok = True
    for nombre_tabla, tabla in Base.metadata.tables.items():
        if nombre_tabla not in tablas_existentes:
            print(f"[FALTA TABLA] '{nombre_tabla}' no existe en la base todavia "
                  f"(create_tables() la va a crear sola en el proximo arranque).")
            continue

        columnas_reales = {c["name"] for c in inspector.get_columns(nombre_tabla)}
        columnas_esperadas = {c.name for c in tabla.columns}

        faltantes = columnas_esperadas - columnas_reales
        if faltantes:
            ok = False
            print(f"[DRIFT] Tabla '{nombre_tabla}': faltan columnas {sorted(faltantes)}")

        sobrantes = columnas_reales - columnas_esperadas
        if sobrantes:
            print(f"[INFO] Tabla '{nombre_tabla}': columnas en la base que ya no usa el modelo "
                  f"{sorted(sobrantes)} (no bloquea el deploy, dato historico probablemente).")

    if ok:
        print("Esquema OK: no falta ninguna columna que el codigo actual necesite.")
    else:
        print("\nHay columnas nuevas en app/models.py que todavia no existen en la base.")
        print("Generar el ALTER TABLE correspondiente y aplicarlo antes de deployar.")

    return ok


if __name__ == "__main__":
    sys.exit(0 if check_schema() else 1)
