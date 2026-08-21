# Spec funcional — App de Conciliación Bancaria vía WhatsApp

**Estado:** borrador para revisión
**Fecha:** 2026-08-20
**Fase de este documento:** especificación funcional (la especificación técnica/arquitectura se aborda en un documento separado, posterior a este)

## 1. Objetivo

**Contexto del negocio.** La empresa reparte paquetes y cobra en el domicilio del cliente al momento de la entrega. El pago se recibe por transferencia bancaria, y cada repartidor manda por WhatsApp el comprobante de esa transferencia apenas la recibe.

Automatizar la recepción y el registro de esos comprobantes de pago que los repartidores (operadores/vendedores) envían por WhatsApp, verificando sus datos con ayuda de un LLM y asociándolos a la cuenta o factura correspondiente, dejando un registro diario auditable por repartidor para su rendición al terminar el reparto, y conciliando finalmente todos los movimientos registrados contra los resúmenes de las distintas cuentas bancarias receptoras de la empresa.

El problema que resuelve: hoy la carga y verificación de comprobantes de transferencias es manual, dispersa entre conversaciones de WhatsApp, y la conciliación contra el banco se hace por separado sin trazabilidad clara de quién cobró qué y cuándo.

## 2. Alcance

La app es standalone e independiente de la operación de la empresa: no modela repartos, rutas, paquetes ni ningún otro proceso logístico. Lo único que registra son vendedores/repartidores (como usuarios del sistema) y las facturas y pagos de facturas que ellos informan. El contexto de negocio descrito en la sección 1 (reparto de paquetes, cobro en domicilio) explica por qué existe la app, pero la app en sí no necesita saber nada de repartos: solo ve "un vendedor registró el pago de una factura".

La app tampoco depende de un ERP ni de un sistema de facturación externo. El vendedor solo indica un número de cuenta o de factura como referencia del pago; ese dato queda registrado como texto/campo propio de la app, sin validarse contra ningún sistema externo.

La empresa maneja varias cuentas bancarias, todas de una misma empresa (no es una app multi-empresa/multi-cliente en esta fase). No hay una asociación fija entre vendedor y cuenta bancaria receptora: al ser la app independiente de la operación, cualquier vendedor puede registrar un pago que termine acreditado en cualquiera de las cuentas de la empresa: esto se resuelve en la conciliación, no de antemano.

Quedan fuera de alcance por ahora: integración con Odoo u otro ERP, integración con sistemas de facturación electrónica, pagos con otros medios que no sean transferencia bancaria (efectivo, tarjeta, billeteras virtuales quedan como posible fase futura si se confirma que aplica), soporte multi-empresa/multi-tenant (la app es de una sola empresa), y una app o panel para que el operador use algo distinto de WhatsApp.

## 3. Actores

**Operador (repartidor/vendedor).** Persona registrada previamente en el sistema con su número de WhatsApp y un ID propio. Es el repartidor que entrega el paquete, cobra en el domicilio del cliente por transferencia bancaria, envía el comprobante por WhatsApp, y confirma y corrige los datos extraídos — no hay un tercero que valide en ese momento; el propio operador es responsable de la exactitud de lo que confirma. En la interfaz de administración se lo identifica como "Vendedor". Al terminar su recorrido de reparto, es quien rinde sus comprobantes del día (ver sección 5).

**Administrador/Conciliador.** Rol interno que usa un panel web (ver sección 6.1) para: dar de alta operadores y sus números de WhatsApp, dar de alta las cuentas bancarias receptoras de la empresa, cargar comprobantes manualmente cuando no llegaron bien por WhatsApp, corregir un comprobante ya confirmado por un vendedor (única vía posible para corregirlo — el vendedor no puede editarlo una vez confirmado), cargar los resúmenes de cuenta del día de cada cuenta receptora, conciliar los comprobantes rendidos por cada vendedor contra esos resúmenes, resolver los casos que no calzan automáticamente, y ver las rendiciones diarias de cada operador.

## 4. Flujo principal: recepción y registro de un comprobante

1. Un operador registrado envía por WhatsApp una imagen o un PDF de un comprobante de transferencia bancaria.
2. El sistema identifica al operador por su número de WhatsApp de origen contra la lista de operadores registrados. Si el número no está registrado, el sistema no procesa el comprobante y responde indicando que el número no está habilitado.
3. Según el tipo de archivo recibido:
   - Si es PDF, el sistema extrae el texto con Python (extracción de texto nativo del PDF).
   - Si es imagen, el sistema extrae el texto con OCR.
4. El LLM interpreta el texto extraído y estructura los datos relevantes de la transferencia: monto, fecha y hora de la operación, banco emisor, banco/cuenta receptora, número de operación o referencia, y cualquier otro dato identificable (CVU/alias, titular, etc.).
5. El sistema responde al operador por WhatsApp mostrando los datos interpretados y le pide que los confirme o corrija.
6. El operador confirma que los datos son correctos, o indica una corrección; el sistema ajusta el registro según lo que el operador indique.
7. El sistema solicita al operador que indique el número de cuenta o de factura al que corresponde ese pago (dato de texto libre). Este sistema no se sincroniza con el sistema de facturación de la empresa: solo registra el número informado, sin validarlo contra ningún padrón ni sistema externo.
8. El operador responde con ese dato.
9. El sistema muestra un resumen final de todo lo registrado (datos de la transferencia + cuenta/factura asociada) y pide la confirmación final de registro.
10. El operador confirma ("OK"), y el movimiento queda registrado con estado **Confirmado**, asociado al operador, a la fecha/hora de recepción, y disponible para la rendición del día y la conciliación posterior.

### 4.1 Carga manual desde el panel

Además del flujo por WhatsApp, el administrador puede cargar un comprobante manualmente desde el panel web, para los casos en que no llegó bien por WhatsApp o se necesita cargarlo directamente. La carga manual pide los mismos datos que se registran en el flujo automático (datos de la transferencia, operador/vendedor asociado, y la cuenta/cliente de referencia), y el comprobante manual queda marcado como tal para diferenciarlo de los recibidos por WhatsApp, pero sigue el mismo circuito de estados y de conciliación.

### 4.2 Casos alternativos y de error

Comprobante ilegible o del que no se pudieron extraer datos mínimos (monto, fecha, referencia): el sistema informa al operador que no pudo leer el comprobante y le pide que lo reenvíe con mejor calidad, o que ingrese los datos manualmente respondiendo por WhatsApp.

Operador no responde a la confirmación dentro de un tiempo razonable: el comprobante queda en estado **Pendiente de confirmación**, visible para el administrador, sin bloquear el resto del flujo del operador.

Operador cancela o rechaza los datos mostrados: el sistema descarta el registro y permite reenviar el comprobante.

Comprobante duplicado: si el número de operación/referencia ya existe en un movimiento previo, el sistema alerta al operador (y potencialmente al administrador) antes de registrar el duplicado, para evitar contarlo dos veces.

Corrección de un movimiento ya confirmado: el vendedor no puede modificar un comprobante después de confirmado — por ejemplo, si se equivocó al indicar el número de cuenta o factura. Solo el administrador puede corregirlo, desde el panel web.

*(Pendiente de confirmar con el usuario: qué pasa si el operador se equivoca de cuenta/factura después de haber confirmado — si hay una forma de corregir un movimiento ya confirmado, o si eso lo resuelve el administrador desde un panel.)*

## 5. Registro diario y rendición por operador

Cada movimiento confirmado queda asociado a la fecha del día y al operador que lo generó. Al terminar su recorrido de reparto, cada vendedor rinde sus comprobantes del día: el sistema debe poder producir, por cada operador, un detalle de todos los movimientos del día (hora de recepción, monto, factura/cuenta asociada, y estado de cada uno) junto con el total del día, como respaldo de esa rendición.

Por ahora la rendición es puramente administrativa: es un reporte/consulta que muestra lo que el vendedor cobró en el día, pero no es una acción que cambie el estado de los movimientos ni "cierre" el día — no bloquea que el vendedor siga enviando comprobantes, y no incide en los registros. Es simplemente la vista/reporte que el administrador (o el propio vendedor) usa para cotejar lo cobrado en el día.

## 6. Conciliación con los resúmenes bancarios

Este es un proceso posterior y separado del flujo de WhatsApp, que se dispara después de que los vendedores rinden sus comprobantes del día. Toma como entrada todos los movimientos confirmados y rendidos, y los resúmenes de cuenta del día de las distintas cuentas bancarias receptoras de la empresa.

Flujo propuesto:

1. El administrador importa el resumen de cuenta del día de cada cuenta receptora. El sistema acepta el archivo en formato **CSV, PDF o XLS/Excel**, con las líneas de movimientos del banco (fecha, monto, descripción, referencia).
2. El sistema intenta emparejar automáticamente cada movimiento rendido por los vendedores con una línea del resumen bancario correspondiente, usando como criterios el monto, la fecha (con una tolerancia de días a definir), el número de operación/referencia cuando esté disponible, y la cuenta bancaria destino.
3. Cada movimiento queda en uno de estos estados de conciliación: **Conciliado** (match automático encontrado y confirmado), **Conciliado manualmente** (el administrador lo emparejó a mano), **Pendiente** (sin match encontrado), o **Con diferencia** (hay una línea candidata pero el monto u otro dato no coincide exactamente). Este estado queda registrado y visible en el listado de comprobantes, para que el administrativo pueda identificar y revisar lo que no concilió.
4. El administrador dispone de una vista para revisar los movimientos pendientes o con diferencia y resolverlos manualmente: emparejarlos a mano contra una línea del resumen, marcarlos como no corresponde, o —si el motivo es que falta el comprobante— cargarlo manualmente desde el panel (sección 4.1) y conciliarlo en un paso posterior.
5. También deben poder identificarse líneas del resumen bancario que no tienen ningún movimiento asociado (por ejemplo, depósitos no informados por ningún operador), como parte del control de la conciliación.

Al ser resúmenes de cuenta del día, la conciliación es de frecuencia diaria: cada día se cargan los resúmenes de ese día y se concilian contra los comprobantes rendidos ese mismo día. No hay vendedores asociados de antemano a una cuenta receptora en particular (ver sección 2): cualquier vendedor puede registrar un pago que la conciliación termine emparejando contra cualquiera de las cuentas de la empresa.

## 6.1 Panel de administración (referencia de interfaz)

El usuario aportó una captura de una interfaz existente (app "LFT") como referencia visual y de layout para el panel de administración de esta app — no se replican sus funcionalidades multi-empresa, pero sí sirve de base para el listado de comprobantes y la navegación. Estructura observada, adaptada a esta app:

Navegación lateral con dos secciones: **Comprobantes** (Resumen, Mis Comprobantes, Conciliaciones, Manual de Usuario) y **Configuración** (Usuarios, Operadores/Vendedores, Bancos, Teléfonos registrados). No existe una entidad "Clientes": la app no registra clientes, solo movimientos. El campo que en la captura de referencia figura como "Cliente/Ruta" es, en esta app, **"Factura o Cuenta"** — el mismo dato de texto libre que el operador declara por WhatsApp (sección 4, paso 7), sin registro ni validación contra un padrón de clientes.

La pantalla "Mis Comprobantes" es un listado de todos los comprobantes registrados, con filtros por banco, estado de conciliación, operador/vendedor, fecha de transacción y fecha de subida, más una búsqueda libre. Las columnas del listado son: fecha de transacción, fecha de subida/recepción, **Factura o Cuenta** (dato declarado por el operador), cuenta bancaria asociada, número de operación, monto, operador/vendedor que lo registró, y estado de conciliación. El panel incluye acciones de **Exportar** (el listado, para análisis externo) y **Carga Manual** (ver sección 4.1).

La pantalla "Resumen" es el dashboard principal, con vista general de comprobantes por vendedor. Incluye filtros por tipo de reporte (por vendedor), búsqueda de vendedor, y rango de fechas; tarjetas con totales generales (cantidad de vendedores, monto total, cantidad de comprobantes); y una tabla "Resumen por Vendedor" con columnas: vendedor, teléfono, tipo (por ejemplo Administrativo / Reparto, según el rol del operador), total, cantidad de comprobantes, conciliados con el banco y pendientes de conciliar con el banco. A diferencia de la captura de referencia, esta app no tiene columnas de conciliación con ERP (Conc./Pend. ERP), porque no hay integración con ningún ERP — solo se muestra la conciliación bancaria.

## 7. Modelo de datos conceptual

**Operador (Vendedor)**: ID interno, nombre, número de WhatsApp, tipo/rol (por ejemplo Administrativo / Reparto), estado (activo/inactivo), fecha de alta.

**Cuenta bancaria**: ID interno, banco, número de cuenta, moneda, alias/apodo para identificarla fácilmente.

**Movimiento (comprobante registrado)**. Campos mínimos que debe tener todo comprobante registrado, definidos según las columnas de la interfaz de referencia (sección 6.1):

- Fecha de transacción (fecha en que se hizo la transferencia, según el comprobante).
- Fecha de subida/recepción (fecha en que el comprobante llegó al sistema, por WhatsApp o carga manual).
- Factura o Cuenta (dato que el operador declara como destino del pago — campo de texto libre descrito en el flujo; no hay registro ni padrón de clientes, solo movimientos).
- Cuenta banco (la cuenta bancaria de la empresa a la que se asoció el pago; puede quedar "Sin banco" hasta que se le asigne en la conciliación).
- Número de operación (referencia de la transferencia extraída del comprobante).
- Monto.
- Vendedor (el operador que registró el comprobante, o que lo cargó manualmente).
- Estado de conciliación (Pendiente / Conciliado / Conciliado manualmente / Con diferencia).

A esto se suman los campos internos de control ya definidos: ID interno, archivo original del comprobante, otros datos extraídos de la transferencia si están disponibles (hora de la operación, banco emisor, titular), origen del registro (WhatsApp / Carga manual), y estado del registro (Pendiente de confirmación / Confirmado). No existe un estado "Cancelado": si el operador rechaza los datos antes de confirmar, el registro se descarta directamente y no queda guardado.

**Resumen bancario importado**: cuenta bancaria asociada, período, líneas de movimiento (fecha, monto, descripción, referencia), estado de cada línea (conciliada / sin conciliar).

**Rendición diaria**: operador, fecha, listado de movimientos del día, monto total.

**Usuario de panel**: ID interno, nombre, email, rol (Administrador/Conciliador), estado (activo/inactivo) — para el acceso al panel web de administración.

## 8. Requisitos no funcionales

El sistema debe validar que todo comprobante provenga de un número de WhatsApp previamente registrado como operador, para evitar el ingreso de comprobantes de personas no autorizadas.

Todo el historial de conversación y confirmaciones debe quedar trazado (quién confirmó qué dato y cuándo), dado que sirve como respaldo de la rendición diaria.

Los archivos originales de los comprobantes (PDF/imagen) deben conservarse asociados a cada movimiento, para poder auditar la extracción en caso de duda.

La interacción por WhatsApp debe responder en tiempos razonables para no cortar el flujo de confirmación con el operador.

El sistema debe operar en español, dado que es el idioma de los operadores.

## 9. Preguntas abiertas para cerrar el spec funcional

Con las últimas definiciones, el spec funcional queda cerrado en sus puntos principales. Solo queda como pregunta abierta menor, sin bloquear el paso al spec técnico: si en el futuro se prevén otros medios de pago además de transferencia bancaria.

## 10. Fuera de alcance (por ahora)

Integración con Odoo o cualquier ERP/sistema de facturación. Multi-empresa (varios clientes distintos usando la misma instancia — la captura de referencia usada para el panel es solo inspiración visual, esa función no se replica). Medios de pago distintos a transferencia bancaria. Panel o app propia para el operador fuera de WhatsApp (el operador solo interactúa por WhatsApp; el panel web es exclusivo del rol Administrador).
