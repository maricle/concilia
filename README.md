# Concilia

Backend de conciliacion bancaria via WhatsApp.

## Estado actual

Fase 1: webhook de Meta, validacion de operadores, maquina de estados conversacional, extraccion de comprobantes con Claude (vision para imagenes, lectura nativa de PDF) y persistencia de movimientos. El envio de respuestas reales por WhatsApp (API de Meta) todavia no esta conectado.

Fase 2 (panel de administracion): login simple de usuarios de panel, alta/baja de operadores (`/config/operadores`), alta de cuentas bancarias (`/config/cuentas`) y listado de comprobantes confirmados (`/comprobantes`). La pantalla "Resumen" (dashboard) no esta implementada todavia.

Fase 3 (conciliacion bancaria): desde `/conciliaciones` el administrador importa el resumen de cuenta del dia (CSV o XLSX) de una cuenta bancaria. El motor de matching (`app/reconciliation.py`) empareja automaticamente cada linea del resumen contra los movimientos confirmados y no conciliados, usando monto exacto, fecha (tolerancia de 1 dia) y numero de operacion/referencia cuando esta disponible en ambos lados: un unico candidato por monto+fecha (o por referencia) queda **Conciliado**; un candidato con la misma referencia pero monto distinto queda **Con diferencia**; sin candidato o con varios candidatos ambiguos, la linea y el movimiento quedan **Pendientes** para revision manual. Desde la misma pantalla el administrador resuelve a mano lo que no concilio automaticamente: emparejar una linea contra un movimiento pendiente, o marcar una linea del resumen como "no corresponde" (por ejemplo, un deposito ajeno a la operacion). Parsers de resumen bancario soportados por ahora: CSV y XLSX, con columnas identificadas por nombre (Fecha/Importe/Descripcion/Referencia u equivalentes); PDF queda pendiente. La carga manual de comprobantes desde el panel (seccion 4.1 del spec funcional) tambien queda pendiente.

Para pruebas rapidas mientras se define la integracion final con WhatsApp, hay un canal alternativo por **Telegram** (`POST /telegram/webhook`) que reutiliza la misma maquina de estados y ya tiene la extraccion con Claude conectada. Los archivos de comprobante recibidos por ese canal se guardan ademas en una base de **Turso** (`app/storage.py`), solo para pruebas — no reemplaza el storage S3/R2 definido en el spec tecnico.

### Panel de administracion

El panel vive en el mismo backend (`/login`, `/config/operadores`), server-rendered con Jinja2, protegido con sesion por cookie. No hay una pantalla de registro: el primer usuario se crea con un script.

Local:

```powershell
python scripts/create_panel_user.py --nombre "Tu Nombre" --email "tu@email.com" --password "una-contrasena-segura"
```

En un deploy de Railway (corre el script dentro del contenedor, usando la misma base de datos que la app):

```powershell
railway ssh --service <nombre-del-servicio> python scripts/create_panel_user.py --nombre "Tu Nombre" --email "tu@email.com" --password "una-contrasena-segura"
```

Volver a correrlo con el mismo email actualiza el nombre y la contrasena de ese usuario (sirve para resetear una contrasena).

### Probar con Telegram

1. Crea un bot con [@BotFather](https://t.me/BotFather) y copia el token a `TELEGRAM_BOT_TOKEN` en `.env`.
2. Instala el driver de Turso y crea/copia las credenciales de tu base de prueba (el dashboard de [turso.tech](https://turso.tech) es mas simple que el CLI, que no tiene build para Windows):

   ```powershell
   python -m pip install -e ".[test,turso]"
   ```

   Completa `TURSO_DATABASE_URL` (formato `libsql://...`) y `TURSO_AUTH_TOKEN` en `.env`.
3. Completa `ANTHROPIC_API_KEY` en `.env` para que la extraccion funcione.
4. Expone tu servidor local (por ejemplo con `ngrok http 8000`) y registra el webhook de Telegram:

   ```powershell
   curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<tu-url-publica>/telegram/webhook&secret_token=<TELEGRAM_WEBHOOK_SECRET>"
   ```

   `TELEGRAM_WEBHOOK_SECRET` es opcional pero recomendado; si lo configuras en `.env`, usa el mismo valor en el `setWebhook`.
5. Da de alta un operador desde `/config/operadores` con tu `chat_id` de Telegram como si fuera el numero de WhatsApp (mismo campo `whatsapp_numero` — se obtiene hablandole a [@userinfobot](https://t.me/userinfobot)).
6. Mandale al bot una foto o PDF de un comprobante: Claude extrae los datos y el bot te pide que confirmes, indiques la cuenta/factura, y confirmes el registro final — igual que el flujo funcional descripto en el spec.

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
