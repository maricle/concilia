# Spec técnico — App de Conciliación Bancaria vía WhatsApp

**Estado:** borrador para revisión
**Fecha:** 2026-08-20
**Depende de:** `spec-conciliacion-bancaria.md` (spec funcional, mismo proyecto) — este documento no repite las reglas de negocio ya definidas ahí, solo cómo se implementan.

> **Nota sobre el canal (vigente):** WhatsApp Business (Meta, `POST /webhook`) y **Telegram** (`POST /telegram/webhook`) están ambos conectados de punta a punta — bajan el archivo, corren la extracción y responden por el mismo canal. Ver la sección 14 para el detalle de cómo se comparte el estado de la conversación entre ambos.

## 1. Arquitectura general

La app sigue el mismo patrón que BridgeBot (la app de Kleba Dev para bots de WhatsApp/Instagram con Claude), adaptado a este caso de uso:

Un backend único en **Python 3.13 + FastAPI** expone: un webhook que recibe los mensajes de WhatsApp Business (Meta), la lógica de extracción y verificación de comprobantes con Claude, el motor de conciliación, y el panel de administración server-rendered (mismo proceso, sin frontend separado).

Flujo de datos, en alto nivel: WhatsApp Business API (Meta) → webhook FastAPI → máquina de estados de la conversación (sección 4) → extracción de datos (PDF con Python / imagen con Claude vision) → confirmación conversacional con el vendedor → registro en PostgreSQL. Por otro lado, el administrador importa resúmenes bancarios desde el panel → motor de conciliación (sección 6) → actualización de estado de cada movimiento.

## 2. Stack tecnológico

Backend: Python 3.13, FastAPI, httpx (llamadas a la API de WhatsApp y Anthropic).

LLM: Anthropic API (Claude), con prompt caching donde aplique, igual que BridgeBot. Se usa para dos cosas distintas: (a) interpretar el texto/imagen del comprobante y estructurar los datos de la transferencia, y (b) llevar la conversación de confirmación con el vendedor en lenguaje natural.

Base de datos: **PostgreSQL** (a diferencia de BridgeBot, que usa Turso/SQLite) — se eligió por la necesidad de reportes, filtros y agregaciones del panel (resumen por vendedor, conciliaciones, exportaciones) que PostgreSQL resuelve mejor que un motor tipo SQLite.

Panel de administración: server-rendered con **Jinja2** dentro del mismo FastAPI, más JavaScript liviano para filtros e interacciones (sin framework de frontend separado), siguiendo el mismo criterio que el `/dashboard` de BridgeBot.

Mensajería: **WhatsApp Business API de Meta** (Cloud API), el mismo canal que usa BridgeBot — no un wrapper no oficial.

Hosting: **Railway**, con deploy automático desde Git, igual que BridgeBot.

Almacenamiento de archivos (comprobantes e imports de resúmenes): Railway no garantiza filesystem persistente entre deploys (mismo problema que BridgeBot tiene con SQLite local sin Turso — ver sección 8). Los archivos originales de los comprobantes y los resúmenes bancarios importados deben guardarse en un almacenamiento externo persistente tipo **S3-compatible** (por ejemplo Cloudflare R2 o AWS S3), no en el disco del contenedor.

## 3. Integración con WhatsApp Business API

Un único número de WhatsApp Business recibe los mensajes de todos los vendedores (no un número por vendedor) — el sistema identifica al vendedor por el número de **origen** del mensaje, contra la tabla de operadores registrados.

El webhook (`POST /webhook`) recibe los eventos de Meta, valida la firma (`META_APP_SECRET`), y por cada mensaje entrante determina el tipo (imagen, documento/PDF, texto) y lo enruta según el estado de conversación de ese número (sección 4). Igual que BridgeBot, se deduplica por `message_id` para que los reintentos de Meta (si no se responde 200 a tiempo) no generen procesamiento duplicado, y los mensajes de un mismo número se procesan en orden (no en paralelo), para evitar que una ráfaga de mensajes cruce las respuestas del bot.

Envío de mensajes salientes (confirmaciones, pedidos de datos, resultado del registro) vía la API de mensajes de Meta, con `WA_ACCESS_TOKEN` y `WA_PHONE_ID`.

## 4. Máquina de estados de la conversación

A diferencia de BridgeBot (donde Claude maneja la conversación de forma más libre), acá el flujo es más estructurado porque tiene pasos obligatorios (extraer → confirmar datos → pedir cuenta/factura → confirmar registro). Se modela como una máquina de estados por conversación (número de WhatsApp), persistida en una tabla `conversaciones_whatsapp`:

- `esperando_comprobante` — estado inicial/de reposo, esperando que el vendedor mande una imagen o PDF.
- `esperando_confirmacion_datos` — el sistema mostró los datos extraídos y espera que el vendedor confirme o corrija.
- `esperando_cuenta_factura` — el sistema pidió el número de cuenta/factura.
- `esperando_confirmacion_final` — el sistema mostró el resumen completo y espera el OK final.

En cada estado, Claude se usa para interpretar la respuesta libre del vendedor (confirmación, corrección, cancelación) y decidir la transición, pero las transiciones y qué se persiste en cada una las controla el código, no el LLM — el LLM interpreta lenguaje natural, el código decide el estado. Esto evita que una respuesta ambigua del vendedor derive en un registro incompleto o inconsistente.

Cada conversación referencia el movimiento en construcción (borrador) mientras no se confirma; recién al llegar a `Confirmado` (sección 5) el movimiento queda como registro definitivo y la conversación vuelve a `esperando_comprobante`.

## 5. Extracción de datos del comprobante

**PDF:** extracción de texto nativo con una librería Python (`pdfplumber` o `pypdf`), y el texto resultante se pasa a Claude para estructurar los campos.

**Imagen:** en vez de un motor de OCR tradicional (Tesseract u otro) como paso separado, se recomienda pasar la imagen directamente a Claude usando su capacidad de visión — Claude puede leer texto de una imagen y estructurarlo en un mismo paso, lo cual simplifica el pipeline (un componente menos que mantener) y suele dar mejor resultado que OCR clásico + LLM en dos pasos, especialmente con fotos de comprobantes de distinta calidad. *(Nota: el spec funcional habla de "OCR" — esta es la forma concreta de resolverlo técnicamente; si se prefiere mantener un motor de OCR tradicional separado de Claude, avisar para ajustar esta sección.)*

**Qué modelo de Claude usar.** Se recomienda **Claude Haiku 4.5** como modelo por defecto para la extracción: es el mismo modelo que ya usa BridgeBot (consistencia de infraestructura y costos), tiene visión nativa, y es el más rápido y económico de la familia actual — importante porque esto corre dentro de una conversación de WhatsApp donde la latencia importa y el volumen diario de comprobantes puede ser alto. Como resguardo, si la extracción con Haiku no logra completar los campos mínimos (monto, fecha, identificador de la operación) o devuelve baja confianza, conviene reintentar una sola vez con **Claude Sonnet 5** antes de pedirle al vendedor que reenvíe la foto — Sonnet tiene mejor capacidad de lectura en imágenes difíciles (baja calidad, reflejos, comprobantes cortados) a un costo todavía razonable para ese caso puntual. No se recomienda usar el modelo más grande (Opus/Fable) para este paso: es mucho más caro y la tarea (leer un comprobante y estructurar campos) no requiere ese nivel de razonamiento.

En ambos casos (PDF e imagen), la interpretación usa **structured output / tool use** de Claude (se le pide una función/schema JSON con los campos: monto, fecha de la operación, hora, banco emisor, banco/cuenta receptora, número de operación o referencia, CVU/alias si figura, titular si figura), en vez de parsear texto libre — esto da un resultado predecible para guardar en la base de datos y evita errores de parseo.

Si la extracción no logra obtener al menos monto, fecha y algún identificador de la operación (ni siquiera después del reintento con Sonnet), se considera comprobante ilegible (caso alternativo ya definido en el spec funcional, sección 4.2).

## 6. Motor de conciliación

Proceso disparado por el administrador desde el panel (no automático/programado en esta fase), después de importar el resumen de cuenta del día de una cuenta receptora.

Pasos: (1) parsear el archivo importado (CSV, XLS o PDF) según el formato/banco — cada banco puede tener un layout de columnas distinto, por lo que se necesita un parser configurable o adaptador por banco (al menos uno inicial, ampliable); (2) para cada línea del resumen, buscar candidatos entre los movimientos **Confirmados** y aún no conciliados de esa cuenta, con matching por monto exacto + fecha dentro de una tolerancia configurable (por defecto sugerida: mismo día o ±1 día) + número de operación/referencia si está disponible en ambos lados; (3) si hay un único candidato que matchea monto y fecha, se marca **Conciliado** automáticamente; si hay más de un candidato posible (por ejemplo mismo monto, misma fecha, sin referencia), queda **Pendiente** para resolución manual en vez de adivinar; (4) las líneas del resumen sin ningún candidato, y los movimientos sin ninguna línea candidata, quedan visibles en la pantalla de Conciliaciones para revisión manual.

La resolución manual (emparejar a mano, marcar como no corresponde, o cargar el comprobante faltante y conciliarlo después) se hace desde el panel, un movimiento/línea a la vez.

## 7. Modelo de datos (PostgreSQL)

Tablas principales, en base al modelo conceptual del spec funcional (sección 7 de ese documento):

- `operadores` (id, nombre, whatsapp_numero, tipo, activo, fecha_alta)
- `cuentas_bancarias` (id, banco, numero_cuenta, moneda, alias)
- `movimientos` (id, operador_id, fecha_transaccion, fecha_subida, archivo_url, monto, banco_emisor, cuenta_receptora_extraida, numero_operacion, titular, factura_o_cuenta, cuenta_bancaria_id nullable, origen [whatsapp/manual], estado_registro [pendiente_confirmacion/confirmado], estado_conciliacion [pendiente/conciliado/conciliado_manual/con_diferencia], creado_por_usuario_id nullable para carga manual)
- `resumenes_importados` (id, cuenta_bancaria_id, fecha, archivo_url, formato, usuario_id, fecha_importacion)
- `lineas_resumen` (id, resumen_id, fecha, monto, descripcion, referencia, movimiento_id nullable, estado)
- `usuarios_panel` (id, nombre, email, password_hash, rol, activo)
- `conversaciones_whatsapp` (numero, estado, movimiento_borrador_id nullable, actualizado_en)

Índices relevantes: `movimientos.numero_operacion` (para detectar duplicados), `movimientos(operador_id, fecha_transaccion)` (para rendición diaria), `movimientos(cuenta_bancaria_id, estado_conciliacion)` (para la pantalla de conciliaciones).

## 8. Persistencia y consistencia de datos en Railway

Igual que el "gotcha" conocido de BridgeBot con SQLite/Turso: Railway no garantiza que el filesystem del contenedor sobreviva a un redeploy. PostgreSQL en Railway (como servicio administrado) resuelve la persistencia de datos estructurados, pero los **archivos** (comprobantes, resúmenes importados) necesitan el storage externo mencionado en la sección 2 — nunca guardarlos solo en el disco del contenedor.

## 9. Panel de administración

Rutas principales (server-rendered, Jinja2), siguiendo la estructura ya definida en el spec funcional (sección 6.1):

`GET /resumen` — dashboard principal con filtros y tabla por vendedor.
`GET /comprobantes` — listado "Mis Comprobantes" con filtros y export.
`POST /comprobantes/manual` — carga manual de un comprobante.
`GET /comprobantes/{id}/editar` — corrección de un movimiento ya confirmado (solo administrador).
`GET /conciliaciones` — pantalla de resolución manual de pendientes/diferencias.
`POST /conciliaciones/importar` — carga de un resumen bancario.
`GET/POST /config/usuarios`, `/config/operadores`, `/config/bancos`, `/config/telefonos` — CRUD de configuración.

Autenticación: login simple de usuarios de panel (email + contraseña, hash con bcrypt/argon2), con sesión por cookie. No se contempla en esta fase login social ni SSO.

## 10. Seguridad

Webhook de WhatsApp: validación de firma con `META_APP_SECRET`, igual que BridgeBot.

Todo comprobante entrante se valida contra la lista de operadores registrados por número de WhatsApp antes de procesar nada (regla ya definida en el spec funcional).

Panel de administración detrás de autenticación; los archivos de comprobantes en el storage externo no deben ser de acceso público directo (URLs firmadas/privadas), dado que contienen datos bancarios.

## 11. Variables de entorno (borrador)

WhatsApp/Meta: `META_VERIFY_TOKEN`, `META_APP_SECRET`, `WA_ACCESS_TOKEN`, `WA_PHONE_ID`.
Claude: `ANTHROPIC_API_KEY`.
Base de datos: `DATABASE_URL` (PostgreSQL).
Storage: `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET` (o equivalentes de R2).
Panel: `SESSION_SECRET`.
Modo/debug: `MODO_DEV` (para probar sin mandar WhatsApp real, igual que BridgeBot).

## 12. Roadmap sugerido

**Fase 1 — flujo WhatsApp end-to-end.** Webhook, máquina de estados, extracción PDF/imagen con Claude, confirmación conversacional, registro en PostgreSQL. Sin panel todavía (se puede validar consultando la base directamente).

**Fase 2 — panel de administración.** Login, Resumen, Mis Comprobantes, Configuración (operadores, bancos, usuarios, teléfonos), carga manual, corrección de movimientos confirmados.

**Fase 3 — conciliación.** Importación de resúmenes (CSV/XLS primero, PDF después si hace falta — es el formato más difícil de parsear de forma confiable), motor de matching automático, pantalla de resolución manual.

**Fase 4 — pulido.** Exportaciones, rendición diaria como reporte formal, ajustes de tolerancias de matching según los primeros casos reales.

## 13. Riesgos y puntos a validar

El formato de los resúmenes bancarios varía por banco — el parser de importación probablemente necesite un adaptador por banco en vez de un parser genérico único; conviene tener 1-2 ejemplos reales de resumen por cada banco que se vaya a soportar antes de implementar el parser.

La extracción con Claude vision de comprobantes fotografiados (no escaneados) puede fallar con fotos de mala calidad, reflejos o comprobantes parcialmente cortados — el flujo ya contempla pedir reenvío, pero conviene medir la tasa de error real con comprobantes de ejemplo antes de dar por cerrado el pipeline de extracción.

Los límites de tasa y de ventana de mensajes de la API de WhatsApp Business (Meta) aplican igual que en BridgeBot — no debería ser un problema al volumen esperado, pero conviene tenerlo presente si el número de operadores/comprobantes diarios crece mucho.

## 14. Actualizaciones posteriores a este spec

Este documento quedó como borrador desde su fecha original; varias decisiones cambiaron con la implementación real. Registro de los cambios técnicos relevantes, no exhaustivo:

**Dos canales, un mismo hilo por operador.** El webhook de WhatsApp Business (Meta, sección 3, `POST /webhook`) pasó de ser solo un stub (recibía texto pero nunca bajaba archivos ni respondía) a estar conectado de punta a punta, igual que Telegram (`POST /telegram/webhook`): baja la imagen/PDF via la Graph API de Meta, corre la extracción con Claude, y responde por el mismo canal (texto plano o, cuando corresponde, botones interactivos — el equivalente de WhatsApp al `reply_markup`/inline keyboard de Telegram, limitado a 3 botones por mensaje, suficiente para los pasos Sí/No y Factura/Cuenta que ya existían). `ConversationService` identifica al operador por su `whatsapp_numero` en ambos casos, así que un comprobante empezado por un canal se puede terminar por el otro sin perder el estado. La diferencia entre canales: en Telegram el `chat_id` es opaco (no es el teléfono), así que hace falta un paso de "compartir contacto" para vincularlo la primera vez; en WhatsApp el `from` de cada mensaje ya es el teléfono real, así que no hace falta ningún paso de vinculación — se matchea directo contra `Operator.whatsapp_numero` comparando los últimos 10 dígitos (mismo criterio de tolerancia a formato que ya usaba Telegram).

**Webhook de WhatsApp: deduplicación y procesamiento en segundo plano (sección 3).** `POST /webhook` valida la firma y devuelve 200 de inmediato; cada mensaje se procesa después con `BackgroundTasks`, para que Meta no reintente la entrega mientras corre la extracción con Claude (varios segundos, más con el fallback a Sonnet). Los mensajes se deduplican por su `id` (`_mensajes_procesados`, en memoria, últimos 2000) y se procesan en orden por número de origen con un `asyncio.Lock` por número; operadores distintos siguen en paralelo. Antes de este cambio un reintento de Meta podía crear un segundo borrador huérfano y una respuesta cobrada de más.

**Máquina de estados (sección 4).** La interpretación de las respuestas del operador no usa Claude: es por palabras clave y expresiones regulares en `ConversationService` (`si`/`no`/`descartar`, `factura`/`cuenta`, `F 12345`, comandos de salida), y Claude se usa solo para extraer los datos del comprobante (sección 2, punto a). Estados vigentes de `conversaciones_whatsapp.estado`: `esperando_comprobante`; para un comprobante, `esperando_tipo_factura_cuenta`, `esperando_numero_factura_cuenta`, `esperando_confirmacion_final` (solo cuando la factura/cuenta vino en el comentario de la foto), `esperando_numero_reparto_nuevo` y `esperando_movil` (sin salida abierta), `esperando_confirmacion_crear_reparto` (legado de la versión anterior del flujo, alcanzable solo desde una conversación que ya estaba en ese paso); para las salidas, `esperando_datos_inicio_reparto`, `esperando_confirmacion_inicio_reparto` y `esperando_decision_reparto_abierto`. `esperando_confirmacion_datos` quedó sin uso (el resumen se confirma junto con la elección de factura/cuenta) y se conserva solo por compatibilidad con conversaciones guardadas.

**Seguridad del panel.** Defensa CSRF por verificación de origen (middleware en `main.py`): un POST cuyo `Origin` (o `Referer`, si no hay `Origin`) no coincide con el `Host`/`X-Forwarded-Host` del request se rechaza con 403 y queda en el log; `/webhook` y `/telegram/webhook` están exceptuados. Al arrancar se loguea una advertencia si `META_APP_SECRET` o `TELEGRAM_WEBHOOK_SECRET` están vacíos, porque en ese caso los webhooks no verifican el origen.

**Tests y CI.** `tests/conftest.py` fuerza una base SQLite temporal y credenciales de prueba antes de importar la app, así que la suite nunca usa el `DATABASE_URL` ni los tokens del `.env`. `.github/workflows/ci.yml` corre `ruff check` y `pytest` con cobertura en cada push y pull request; las reglas de lint están en `pyproject.toml` y el extra `dev` instala ruff y coverage.

**Estructura del código.** El panel es un paquete, `app/panel/`, con un módulo por área (`auth`, `configuracion`, `resumen`, `comprobantes`, `salidas`, `conciliaciones`), cada uno con su `APIRouter`, más `comun.py` (templates, dependencias de sesión y permisos, helpers compartidos como `respuesta_xlsx`, `del_dia` y `page_url`); `app/panel/__init__.py` arma el router que monta `main.py`. La lógica que WhatsApp y Telegram comparten (operador por teléfono, respuesta a un texto, recordatorio de pendiente, descarga + extracción + guardado + inicio de la carga) vive en `app/canales.py`, que devuelve una `Respuesta` con un `Teclado` abstracto; `webhook.py` y `telegram.py` solo reciben, traducen el teclado a su formato y envían.

**Rendimiento en pedidos recurrentes.** Los filtros por día usan un rango (`del_dia`: `>= 00:00` y `< 00:00 del día siguiente`) en vez de `func.date(columna) == dia`, que no puede usar índices. Índices nuevos en `movimientos` (`estado_registro + fecha_transaccion`, `cuenta_bancaria_id + fecha_transaccion`, `operador_id`, `fecha_subida`), `lineas_resumen` (`resumen_id`, `movimiento_id`) y `resumenes_importados` (`cuenta_bancaria_id + fecha`); como `create_all()` no agrega índices a tablas existentes, `_ensure_indexes()` los crea en el arranque (`checkfirst`). `/comprobantes` pagina de a 50 (antes traía todo en cada visita; la exportación sigue bajando todo lo filtrado). El conteo por estado del historial de resúmenes se agrega en SQL. El motor de Postgres usa `pool_pre_ping` (Railway corta conexiones inactivas) y la extracción reutiliza un único cliente de Anthropic por proceso.

**Storage de archivos: PostgreSQL, no S3/R2.** Contra lo definido en la sección 2, los archivos de comprobantes recibidos no se guardan en un storage externo S3-compatible — se guardan directo en PostgreSQL (tabla `comprobantes_archivo`), dado que son archivos chicos (fotos/PDFs de comprobantes) y esto evita la dependencia de un servicio externo adicional. Lo mismo vale para los resúmenes bancarios importados. Por eso las variables `S3_*` de la sección 11 no se usan.

**Variables de entorno (sección 11).** Las vigentes (ver `.env.example`) son `DATABASE_URL`, `META_VERIFY_TOKEN`, `META_APP_SECRET`, `WA_ACCESS_TOKEN`, `WA_PHONE_ID`, `ANTHROPIC_API_KEY`, `SESSION_SECRET`, `MODO_DEV`, `TELEGRAM_BOT_TOKEN` y `TELEGRAM_WEBHOOK_SECRET`.

**Sin Alembic.** Los cambios de esquema (columnas nuevas, valores de enum) se aplican con `ALTER TABLE`/`ALTER TYPE` manuales en vez de una herramienta de migraciones. `_ensure_enum_values()` corre en cada arranque y agrega automáticamente los valores de enum de Postgres que falten (`ADD VALUE IF NOT EXISTS`), para que agregar un valor a un enum de `models.py` no rompa producción hasta que alguien corra el `ALTER TYPE` a mano.

**Extracción (sección 5).** Además del reintento con Sonnet cuando falta el monto, el mismo reintento se dispara ahora si `cuenta_receptora` es una cadena numérica pero no tiene exactamente 22 dígitos (largo fijo de un CBU/CVU argentino) — señal de que Haiku transcribió el número incompleto. El prompt de extracción también resuelve explícitamente el caso de comprobantes que listan cuenta de origen y de destino bajo un único título sin etiquetas ("Origen y destino"): la primera entidad listada es siempre el origen, la segunda el destino.

**Motor de conciliación (sección 6).** Formatos de resumen bancario soportados hasta ahora: **CSV y XLSX**; PDF sigue sin implementarse (es, como se anticipaba en la sección 13, el formato más difícil de parsear de forma confiable). El estado de una línea de resumen incluye además **no_corresponde** (el administrador la marca así cuando no corresponde a ningún movimiento, por ejemplo un depósito ajeno a la operación), sin agregar un estado equivalente a nivel `movimiento`. La pantalla de resolución manual (`/conciliaciones`) tiene además: filtro propio por fecha/monto sobre las líneas sin conciliar (una tabla ya renderizada completa server-side, sin volver a pegarle al backend); reintentar el emparejamiento automático sin resubir archivo; aceptar un movimiento **con diferencia** (`POST /conciliaciones/movimientos/{id}/aceptar`, lo pasa a `conciliado_manual`; sin esta acción no había forma de sacarlo de ese estado, porque editar el comprobante no vuelve a correr el matching, y el banco quedaba A REVISAR sin poder cerrar el día); cierre diario de la recaudación (`POST /conciliaciones/cierres`, tabla `cierres_diarios`; solo se habilita si ningún banco del día está en `a_revisar` o `error`, y mientras exista bloquea importar, actualizar o eliminar resúmenes, emparejar, marcar "no corresponde" y aceptar diferencias de esa fecha; se reabre con `POST /conciliaciones/cierres/{fecha}/reabrir`); eliminar un resumen importado (sus movimientos conciliados vuelven a `pendiente`); y un fix reciente de sincronización — cambiar de pestaña de banco es navegación normal (no una actualización parcial vía AJAX), porque varias secciones de esa pantalla (líneas pendientes, historial de resúmenes, modal de subir resumen) se renderizan server-side y quedaban mostrando el banco de la carga anterior si solo se refrescaban los KPIs y la tabla de movimientos.

**Móviles y salidas de reparto.** Funcionalidad agregada fuera del alcance original (spec funcional, sección 2): tablas `moviles` (vehículo asignado a un operador) y `repartos` (turno de reparto o "salida", con hora de inicio/fin y número). Por WhatsApp o Telegram, comandos de texto libre ("inicio movil M-01 salida nro 5" — `reparto` y `salida` son sinónimos —, "cerrar" o "fin") arrancan/cierran una salida; si falta un dato el bot lo pide en un paso aparte (número de salida primero, luego móvil). `Movement.movil_id` y `Movement.reparto_id` quedan fijos al momento de confirmarse el comprobante (no siguen al operador si después cambia de móvil). Al cerrar una salida se genera un PDF con el detalle de comprobantes y el desglose por cuenta bancaria.

**Flujo de confirmación más corto (costo por mensaje de WhatsApp).** Como Meta cobra los mensajes de servicio desde el 1/10/2026, `ConversationService` registra el comprobante apenas tiene tipo y número de factura/cuenta (helper `_registrar`), sin pasar por `ESPERANDO_CONFIRMACION_FINAL`. `_parse_caption_factura_cuenta` reconoce "F 12345"/"C 987" (prefijos `f`/`fc`/`fac`/`fact`/`factura` o `c`/`cta`/`cuenta`, pegados o no al número; un número suelto no alcanza) tanto en el texto escrito en `ESPERANDO_TIPO_FACTURA_CUENTA` como en el `caption` de la imagen/PDF (WhatsApp y Telegram) — en este último caso `start_transfer(..., caption=...)` salta directo al Sí/No final, que es el único uso que le queda a `ESPERANDO_CONFIRMACION_FINAL`. Sin salida abierta, `_registrar` pasa a `ESPERANDO_NUMERO_REPARTO_NUEVO` con `movil_pendiente_numero` en null, y `_resolver_salida_por_numero` deduce el móvil: única salida abierta con ese número → se asocia; ninguna → se crea en `Operator.movil_id`; si no alcanza, guarda `numero_reparto_pendiente` y pide el móvil (`ESPERANDO_MOVIL`), que entonces crea la salida sin pedir confirmación. `pending_prompt` (comprobante nuevo con otro a medio cargar) responde "Tenes movimientos pendientes." + el dato que falta, sin repetir el resumen. Botones: **Descartar** suma un tercero al paso factura/cuenta (WhatsApp permite hasta 3) y aparece solo (`needs_descartar_keyboard`) en los pasos de texto libre de un borrador. En `webhook.py`, el aviso a números no registrados se manda como mucho una vez cada 24 h por número (`_avisos_no_habilitado`, en memoria del proceso — un reinicio a lo sumo repite un aviso).

**Panel de administración (sección 9).** El filtro estándar (buscador + avanzados colapsables + Buscar/Limpiar/Exportar) vive en `partials/_filtros.html`, reutilizado por `/comprobantes` y `/repartos`. La exportación es a Excel (`.xlsx`) en toda la app, no CSV. `GET /comprobantes/nuevo` implementa la carga manual (antes solo prevista; la sección 9 la nombraba `POST /comprobantes/manual`), con extracción previa vía `POST /comprobantes/nuevo/extraer`. Las rutas de configuración vigentes son `/config/usuarios`, `/config/operadores`, `/config/moviles` y `/config/cuentas` (no existen `/config/bancos` ni `/config/telefonos`); además están `/repartos` (salidas, con PDF de cierre) y `/conciliaciones/resumenes` (historial de resúmenes importados). La edición de un comprobante (`GET/POST /comprobantes/{id}/editar`) resuelve `cuenta_bancaria_id` con un `<select>` de las cuentas registradas (mostrando el ícono del banco) en vez de aceptar el CBU/CVU/alias como texto libre — el texto crudo extraído del comprobante se conserva como referencia de solo lectura.

## 15. Mejoras solicitadas para la próxima iteración (diseño técnico)

Contraparte técnica de la sección 12 del spec funcional. Ninguno de estos puntos está construido todavía salvo que se aclare lo contrario (hoy: la extracción en carga manual y el rol Recaudador); son lineamientos de diseño, no una implementación cerrada.

**Historial de cambios (auditoría general).** Nueva tabla, por ejemplo `historial_cambios` (`id`, `entidad` [ej. `"movimiento"`, `"reparto"`, `"cuenta_bancaria"`, `"usuario_panel"`...], `entidad_id`, `accion` [`"crear"`/`"editar"`/`"eliminar"`/`"reabrir"`...], `usuario_id` nullable (`PanelUser`, null si lo dispara un operador por WhatsApp o Telegram), `detalle` (JSON o texto con campo/valor_anterior/valor_nuevo), `creado_en`). Alcance general, no solo `Movement`/`Reparto`. Implementación sugerida: un helper central (ej. `registrar_cambio(db, entidad, entidad_id, accion, usuario, detalle)`) llamado explícitamente desde cada endpoint que modifica algo, en vez de un mecanismo automático a nivel ORM/evento — más simple de razonar y de no perder contexto de qué cambió puntualmente en cada caso. `updated_at`/`updated_by` de una entidad se resuelven contra su último registro en esta tabla, sin duplicar el dato en cada modelo.

**Bloqueo de edición de comprobantes conciliados.** En `POST /comprobantes/{id}/editar`: rechazar con 400 (mismo patrón que el resto de las validaciones de ese endpoint) si `movimiento.estado_conciliacion` está en `(CONCILIADO, CONCILIADO_MANUALMENTE)`. `PENDIENTE` y `CON_DIFERENCIA` siguen editables. Un `CON_DIFERENCIA` por monto se corrige antes de aceptarlo; después de aceptado queda `CONCILIADO_MANUALMENTE` y bloqueado como el resto.

**Extracción en carga manual — implementado** (`POST /comprobantes/nuevo/extraer`, que el formulario llama al adjuntar el archivo). Diseño original: `POST /comprobantes/nuevo` pasa a llamar `extract_transfer()` (mismo `app/extraction.py` que usa Telegram) sobre el archivo adjunto antes de guardar el movimiento, pre-completando los campos del formulario. Implica partir el alta en dos pasos (subir archivo → revisar/corregir los datos extraídos → confirmar) en vez del alta directa de un solo paso que tiene hoy.

**Reporte de Mercado Pago por correo — diferido.** Requeriría una cuenta de correo dedicada, lectura vía IMAP (o un proveedor de email parsing) y un parser del formato de ese reporte. **Fuera de esta iteración**, queda solo como nota para más adelante.

**Reabrir una salida cerrada.** No existe hoy la ruta. Agregar algo como `POST /repartos/{id}/reabrir` (`Reparto.hora_fin = None`), registrando el cambio en `historial_cambios`.

**Móvil 0 / recaudador de sala fija.** Se crea un `Movil` especial (por ejemplo `numero="0"`, `nombre="Entrega y retiro"`) sin vehículo real asociado; el recaudador de esa sala se asocia a él igual que cualquier operador (`Operator.movil_id`), y el flujo de iniciar/cerrar salida (por Telegram o por la carga manual) funciona sin cambios de código más allá de dar de alta ese móvil especial.

**Rol Recaudador — implementado.** `PanelUser.rol` sigue siendo `String(50)` (sin migración), pero el alta y la edición de usuarios solo aceptan `Administrador`, `Contador` o `Recaudador` (`ROLES_PANEL`, `_rol_valido`, un `<select>` en el formulario). `es_admin(user)` es verdadero para Administrador y Contador (comparación sin mayúsculas); cualquier otro valor, incluido texto viejo cargado a mano, tiene permisos de Recaudador. La dependency `require_admin` (sobre `require_user`) protege todas las rutas de `/config/*` y `/conciliaciones*`, y `GET/POST /comprobantes/{id}/editar`, `POST /comprobantes/{id}/eliminar` y `POST /comprobantes/eliminar-lote`; si falla levanta `SinPermiso`, que `main.py` responde con `sin_permiso.html` y 403. `es_admin` también está como global de Jinja para ocultar el menú de Conciliaciones/Configuración, el botón de eliminar en lote, el link de edición de cada comprobante y los accesos a Conciliaciones del resumen.
