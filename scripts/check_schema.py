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

from sqlalchemy import Enum, inspect

from app.db import Base, engine
from app import models  # noqa: F401  (importa todos los modelos para poblar Base.metadata)


def _enums_reales(inspector) -> dict[str, set[str]]:
    """Valores de cada tipo ENUM nativo que existen hoy en la base. Solo aplica a
    Postgres -- en SQLite los enums de los modelos se guardan como VARCHAR simple,
    sin un tipo separado que pueda quedar desincronizado."""
    if engine.dialect.name != "postgresql":
        return {}
    return {e["name"]: set(e["labels"]) for e in inspector.get_enums()}


def check_schema() -> bool:
    inspector = inspect(engine)
    tablas_existentes = set(inspector.get_table_names())
    enums_reales = _enums_reales(inspector)

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

        for columna in tabla.columns:
            if engine.dialect.name != "postgresql":
                break  # el chequeo de valores de enum solo aplica a tipos ENUM nativos de Postgres
            if columna.name not in columnas_esperadas - faltantes:
                continue  # ya reportada como columna faltante, no hace falta chequear su enum
            if not isinstance(columna.type, Enum) or not columna.type.native_enum:
                continue
            tipo_nombre = columna.type.name
            valores_esperados = set(columna.type.enums)
            valores_reales = enums_reales.get(tipo_nombre)
            if valores_reales is None:
                ok = False
                print(f"[DRIFT] Tipo enum '{tipo_nombre}' (columna {nombre_tabla}.{columna.name}) "
                      f"no existe en la base.")
                continue
            faltantes_enum = valores_esperados - valores_reales
            if faltantes_enum:
                ok = False
                print(f"[DRIFT] Tipo enum '{tipo_nombre}' (columna {nombre_tabla}.{columna.name}): "
                      f"faltan valores {sorted(faltantes_enum)}")

    if ok:
        print("Esquema OK: no falta ninguna columna ni valor de enum que el codigo actual necesite.")
    else:
        print("\nHay columnas o valores de enum nuevos en app/models.py que todavia no existen en la base.")
        print("Generar el ALTER TABLE / ALTER TYPE ... ADD VALUE correspondiente y aplicarlo antes de deployar.")

    return ok


if __name__ == "__main__":
    sys.exit(0 if check_schema() else 1)
