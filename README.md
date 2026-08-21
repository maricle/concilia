# Concilia

Backend de conciliacion bancaria via WhatsApp.

## Estado actual

Fase 1 inicial: webhook de Meta, validacion de operadores, maquina de estados conversacional y persistencia de movimientos. La extraccion de comprobantes y el envio de respuestas a WhatsApp quedan preparados para conectarse mediante servicios externos.

## Ejecutar

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
uvicorn app.main:app --reload
```

La aplicacion queda disponible en `http://127.0.0.1:8000`. Verifica `http://127.0.0.1:8000/health`.

Copia `.env.example` a `.env` y completa las credenciales antes de conectar Meta o Anthropic. En desarrollo se usa SQLite; en Railway configura `DATABASE_URL` para PostgreSQL.

Para trabajar con PostgreSQL instala el driver y cambia únicamente la URL de conexión:

```powershell
python -m pip install -e ".[test,postgres]"
$env:DATABASE_URL = "postgresql://usuario:password@localhost:5432/concilia"
uvicorn app.main:app --reload
```

También se aceptan URLs con los esquemas `postgres://` y `postgresql://`; la aplicación las adapta al dialecto `postgresql+psycopg` de SQLAlchemy.

## Tests

```powershell
pytest
```
