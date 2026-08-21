# Spec técnico — App de Conciliación Bancaria vía WhatsApp

**Estado:** borrador para revisión
**Fecha:** 2026-08-20
**Depende de:** `spec-funcional.md` (mismo proyecto) — este documento no repite las reglas de negocio ya definidas ahí, solo cómo se implementan.

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
