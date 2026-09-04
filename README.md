# Concilia

Backend de conciliacion bancaria via WhatsApp.

## Estado actual

Fase 1: webhook de Meta, validacion de operadores, maquina de estados conversacional, extraccion de comprobantes con Claude (vision para imagenes, lectura nativa de PDF) y persistencia de movimientos. El envio de respuestas reales por WhatsApp (API de Meta) todavia no esta conectado; el canal en uso real es Telegram (ver mas abajo).

Fase 2 (panel de administracion): completa. Login de usuarios de panel con gestion CRUD propia (`/config/usuarios` — alta, edicion, desactivacion, no se puede auto-desactivar), alta/edicion de operadores (`/config/operadores`), alta/edicion de cuentas bancarias (`/config/cuentas`), listado de comprobantes confirmados con filtros, edicion, eliminacion, orden por columna (clickeando el encabezado) y exportacion a CSV respetando filtros y orden (`/comprobantes`), y dashboard "Resumen" con KPIs (comprobantes, monto total, conciliados, pendientes, con diferencia), grafico de actividad de los ultimos 14 dias, tabla por operador y panel de "Ultimos movimientos", con filtros por fecha/operador/cuenta y export a CSV (`/resumen`). Rediseño visual completo sobre la base de SB Admin 2 (paleta, tipografia, sidebar fijo con toggle) en `app/static/css/custom.css`.

Fase 3 (conciliacion bancaria): desde `/conciliaciones` el administrador importa el resumen de cuenta del dia (CSV o XLSX) de una cuenta bancaria. El motor de matching (`app/reconciliation.py`) empareja automaticamente cada linea del resumen contra los movimientos confirmados y no conciliados, usando monto exacto, fecha (tolerancia de 1 dia) y numero de operacion/referencia cuando esta disponible en ambos lados: un unico candidato por monto+fecha (o por referencia) queda **Conciliado**; un candidato con la misma referencia pero monto distinto queda **Con diferencia**; sin candidato o con varios candidatos ambiguos, la linea y el movimiento quedan **Pendientes** para revision manual. Desde la misma pantalla el administrador resuelve a mano lo que no concilio automaticamente: emparejar una linea contra un movimiento pendiente, marcar una linea del resumen como "no corresponde" (por ejemplo, un deposito ajeno a la operacion), volver a cargar un resumen actualizado sin duplicar lineas ya importadas ("Volver a revisar"), reintentar el emparejamiento automatico sin resubir archivo ("Reintentar conciliacion"), o editar un movimiento sin conciliar. Parsers de resumen bancario soportados por ahora: CSV y XLSX, con columnas identificadas por nombre (Fecha/Importe/Descripcion/Referencia u equivalentes); PDF queda pendiente. La carga manual de comprobantes desde el panel (seccion 4.1 del spec funcional) tambien queda pendiente.

El canal real en uso es **Telegram** (`POST /telegram/webhook`), que reutiliza la misma maquina de estados y tiene la extraccion con Claude conectada (modelo Haiku 4.5 con fallback a Sonnet 5 si no logra leer el monto). Los archivos de comprobante recibidos por ese canal se guardan directo en Postgres (`app/storage.py`, tabla `comprobantes_archivo`) — no es el storage S3/R2 definido en el spec tecnico, pero al ser archivos chicos (fotos/PDFs de comprobantes) alcanza sin depender de un servicio externo.

El panel esta publicado en produccion (Railway) bajo el dominio propio `conciliadm.klebadev.com`, con certificado HTTPS renovado automaticamente (Let's Encrypt via Railway).

**Moviles y repartos**: cada operador puede tener un movil (vehiculo) asignado (`/config/moviles`, con responsable obligatorio y celular cargado). Por Telegram, el operador arranca/cierra un turno de reparto con comandos de texto libre (`inicio movil M-01 reparto nro 5`, `cerrar reparto nro 5`); si ya tenia un reparto abierto en otro movil, el bot le pregunta si cerrarlo o seguir con el actual antes de abrir el nuevo. Todo comprobante confirmado queda asociado al movil vigente del operador en ese momento (`app/repartos.py`, `app/conversation.py`), y el listado de comprobantes se puede filtrar/exportar por movil.

### Notas tecnicas relevantes

- **Sin Alembic**: los cambios de modelo (columnas, tipos enum de Postgres) se aplican con ALTER TABLE/ALTER TYPE manuales. `app/db.py::_ensure_enum_values()` corre en cada arranque y agrega automaticamente valores de enum faltantes (`ALTER TYPE ... ADD VALUE IF NOT EXISTS`); `scripts/check_schema.py` compara los modelos contra la base real (columnas + enums) y sirve como chequeo pre-deploy.
- **Montos en formato argentino**: `app/numeros.py::parse_monto_ar()` centraliza la logica para desambiguar "." (miles) vs "," (decimales), usada tanto en la extraccion con Claude como en el parseo de resumenes bancarios.
- **Emails de usuarios de panel**: se normalizan a minuscula en login, alta y edicion (`app/panel.py`) para evitar duplicados o logins fallidos por diferencia de capitalizacion.

### Panel de administracion

El panel vive en el mismo backend (`/login`, `/config/operadores`, `/config/usuarios`, `/config/cuentas`, `/resumen`, `/comprobantes`, `/conciliaciones`), server-rendered con Jinja2, protegido con sesion por cookie. No hay una pantalla de registro: el primer usuario se crea con un script.

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
2. Completa `ANTHROPIC_API_KEY` en `.env` para que la extraccion funcione.
3. Expone tu servidor local (por ejemplo con `ngrok http 8000`) y registra el webhook de Telegram:

   ```powershell
   curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<tu-url-publica>/telegram/webhook&secret_token=<TELEGRAM_WEBHOOK_SECRET>"
   ```

   `TELEGRAM_WEBHOOK_SECRET` es opcional pero recomendado; si lo configuras en `.env`, usa el mismo valor en el `setWebhook`.
4. Da de alta un operador desde `/config/operadores` con su numero de telefono real (sin codigo de pais, ej. `3794579133`). No hace falta el `chat_id` de Telegram: la primera vez que el operador le escribe al bot desde un chat sin vincular, el bot le pide que comparta su numero con el boton nativo de Telegram, y lo vincula solo si coincide con el numero cargado.
5. Mandale al bot una foto o PDF de un comprobante: Claude extrae los datos y el bot te pide que confirmes, indiques la cuenta/factura, y confirmes el registro final — igual que el flujo funcional descripto en el spec.

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
