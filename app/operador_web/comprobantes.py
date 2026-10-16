"""Carga de comprobantes desde la web: el flujo corto del chat, en pantalla.

Mismas reglas que el chat (app/servicios.py) y el MISMO borrador: un unico
borrador por operador, guardado en su fila de conversaciones_whatsapp, asi un
comprobante empezado por la web se puede terminar por el chat y al reves.

Concurrencia al subir: la extraccion con Claude (varios segundos) corre sin
ningun lock ni transaccion abierta; recien despues se toma el turno del
operador (canales.turno_de) y se bloquea su fila de conversacion
(SELECT ... FOR UPDATE) para volver a verificar que no haya borrador y crearlo.
Registrar y descartar toman lo mismo solo mientras escriben.
"""

import logging
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from .. import servicios
from ..canales import turno_de
from ..config import get_settings
from ..extraction import extract_transfer
from ..models import (
    ConversationState,
    Movement,
    Movil,
    Operator,
    RecordState,
    TipoIdentificador,
    WhatsAppConversation,
)
from ..numeros import formato_monto_ar
from ..panel.comun import del_dia
from ..storage import save_comprobante_archivo
from ..zona_horaria import hoy_argentina
from .archivos import ArchivoInvalido, validar_comprobante
from .comun import templates
from .sesion import get_db, require_operator

router = APIRouter()
logger = logging.getLogger(__name__)

ORIGEN_WEB = "web"
ELEGIR_MOVIL = "Elegí en qué móvil estás."


# -------------------------------------------------------------------- helpers


def borrador_del_operador(db: Session, operador: Operator) -> Movement | None:
    conversacion = db.get(WhatsAppConversation, operador.whatsapp_numero)
    borrador = conversacion.movimiento_borrador if conversacion is not None else None
    if borrador is None or borrador.estado_registro != RecordState.PENDIENTE_CONFIRMACION:
        return None
    return borrador


def _mi_borrador_o_404(db: Session, operador: Operator, movimiento_id: int) -> Movement:
    """El borrador abierto del operador con ese id. Cualquier otra cosa (de otro
    operador, ya confirmado, inexistente) es 404: no se revela si existe."""
    borrador = borrador_del_operador(db, operador)
    if borrador is None or borrador.id != movimiento_id or borrador.operador_id != operador.id:
        raise HTTPException(status_code=404)
    return borrador


def _conversacion_bloqueada(db: Session, operador: Operator) -> WhatsAppConversation:
    """La fila de conversacion del operador, bloqueada hasta el commit
    (FOR UPDATE; en SQLite, usado en tests, no hace nada)."""
    conversacion = db.scalar(
        select(WhatsAppConversation).where(WhatsAppConversation.numero == operador.whatsapp_numero).with_for_update()
    )
    return conversacion if conversacion is not None else servicios.conversacion_de(db, operador)


def _mensaje(request: Request, operador: Operator, titulo: str, detalle: str, *, otra_foto: bool = False, status: int = 200):
    return templates.TemplateResponse(
        request,
        "m/comprobante_mensaje.html",
        {"operador": operador, "titulo": titulo, "detalle": detalle, "otra_foto": otra_foto},
        status_code=status,
    )


def _monto(movimiento: Movement) -> str:
    return formato_monto_ar(movimiento.monto) if movimiento.monto is not None else "no detectado"


def _fecha(fecha: datetime | None) -> str:
    if fecha is None:
        return "no detectada"
    return fecha.strftime("%d/%m/%Y %H:%M") if (fecha.hour or fecha.minute) else fecha.strftime("%d/%m/%Y")


templates.env.filters["monto_comprobante"] = _monto
templates.env.filters["fecha_comprobante"] = _fecha


# ---------------------------------------------------------------------- cargar


@router.get("/m/comprobante/nuevo")
def nuevo(request: Request, operador: Operator = Depends(require_operator), db: Session = Depends(get_db)):
    borrador = borrador_del_operador(db, operador)
    if borrador is not None:
        return RedirectResponse(f"/m/comprobante/{borrador.id}/revisar", status_code=303)
    return templates.TemplateResponse(
        request, "m/comprobante_nuevo.html", {"operador": operador, "max_mb": get_settings().web_max_upload_mb}
    )


@router.post("/m/comprobante")
async def subir(
    request: Request,
    operador: Operator = Depends(require_operator),
    db: Session = Depends(get_db),
):
    # 1) Sin lock: si ya hay un borrador, no se gasta una lectura con Claude.
    borrador = borrador_del_operador(db, operador)
    if borrador is not None:
        return RedirectResponse(f"/m/comprobante/{borrador.id}/revisar", status_code=303)

    # 2) Validar, extraer y guardar el archivo, sin lock ni transaccion abierta.
    # Llegan dos campos "archivo" (camara y archivo elegido). El que quedo vacio
    # el navegador lo manda sin nombre de archivo, y Starlette lo entrega como
    # texto: por eso se lee el formulario a mano y se usa el que trae un archivo.
    contenido = b""
    for subido in (await request.form()).getlist("archivo"):
        if isinstance(subido, StarletteUploadFile):
            contenido = await subido.read()
            if contenido:
                break
    try:
        valido = validar_comprobante(contenido, get_settings().web_max_upload_mb)
    except ArchivoInvalido as exc:
        return _mensaje(request, operador, "No se pudo subir", str(exc), otra_foto=True, status=400)
    db.rollback()  # cerrar la transaccion de la verificacion antes de la espera larga
    try:
        transfer = await run_in_threadpool(extract_transfer, valido.content_type, valido.contenido)
    except Exception:
        logger.exception("No se pudo interpretar un comprobante subido desde la web por el operador %s", operador.id)
        return _mensaje(
            request, operador, "No pudimos procesar el comprobante",
            "Probá de nuevo en unos minutos. Si sigue fallando, avisale al administrador.", otra_foto=True, status=502,
        )
    if transfer is None:
        return _mensaje(
            request, operador, "No pudimos leer el comprobante",
            "Sacá otra foto, derecha y con buena luz. Si no se puede, pedile al administrador que lo cargue a mano.",
            otra_foto=True, status=422,
        )
    archivo_id = None
    try:
        archivo_id = save_comprobante_archivo(nombre_archivo=valido.nombre, content_type=valido.content_type, contenido=valido.contenido)
    except Exception:
        logger.exception("No se pudo guardar el archivo del comprobante; se continua sin bloquear el registro.")

    # 3) Recien ahora: turno del operador + fila bloqueada, re-verificar y crear.
    async with turno_de(operador.whatsapp_numero):
        _conversacion_bloqueada(db, operador)
        borrador = borrador_del_operador(db, operador)
        if borrador is not None:
            db.commit()
            return RedirectResponse(f"/m/comprobante/{borrador.id}/revisar", status_code=303)
        resultado = servicios.crear_borrador(db, operador, transfer, archivo_id, origen=ORIGEN_WEB)
        if isinstance(resultado, servicios.Rechazo):
            db.rollback()  # libera la fila bloqueada
    if resultado is servicios.Rechazo.COMPROBANTE_DUPLICADO:
        return _mensaje(request, operador, "Ese comprobante ya está registrado", "Ya existe un comprobante con ese número de operación.", status=409)
    if resultado is servicios.Rechazo.SIN_CUENTAS_BANCARIAS:
        return _mensaje(
            request, operador, "Todavía no se pueden cargar comprobantes",
            "No hay cuentas bancarias configuradas. Avisale al administrador.", status=409,
        )
    return RedirectResponse(f"/m/comprobante/{resultado.id}/revisar", status_code=303)


# --------------------------------------------------------------------- revisar


def _pantalla_revision(
    request: Request, db: Session, operador: Operator, borrador: Movement, *, error: str | None = None,
    tipo: str | None = None, numero: str | None = None, numero_salida: str = "", pedir_movil: bool = False,
):
    salida = servicios.reparto_abierto_del_operador(db, operador.id)
    movil_asignado = db.get(Movil, operador.movil_id) if operador.movil_id else None
    return templates.TemplateResponse(
        request,
        "m/comprobante_revisar.html",
        {
            "operador": operador,
            "borrador": borrador,
            "salida": salida,
            "pedir_movil": salida is None and (pedir_movil or movil_asignado is None or not movil_asignado.activo),
            "moviles": list(db.scalars(select(Movil).where(Movil.activo.is_(True)).order_by(Movil.numero))),
            "tipo": tipo if tipo is not None else (borrador.factura_o_cuenta_tipo.value if borrador.factura_o_cuenta_tipo else ""),
            "numero": numero if numero is not None else (borrador.factura_o_cuenta_numero or ""),
            "numero_salida": numero_salida,
            "error": error,
        },
        status_code=400 if error else 200,
    )


@router.get("/m/comprobante/{movimiento_id}/revisar")
def revisar(
    movimiento_id: int, request: Request, operador: Operator = Depends(require_operator), db: Session = Depends(get_db)
):
    return _pantalla_revision(request, db, operador, _mi_borrador_o_404(db, operador, movimiento_id))


@router.post("/m/comprobante/{movimiento_id}/registrar")
async def registrar(
    movimiento_id: int,
    request: Request,
    tipo: str = Form(""),
    numero: str = Form(""),
    numero_salida: str = Form(""),
    movil_id: str = Form(""),
    operador: Operator = Depends(require_operator),
    db: Session = Depends(get_db),
):
    borrador = _mi_borrador_o_404(db, operador, movimiento_id)
    # Mismas reglas que "F 12345" / "C 987" en el chat.
    prefijo = {"factura": "F", "cuenta": "C"}.get(tipo)
    factura_cuenta = servicios.interpretar_factura_cuenta(f"{prefijo} {numero.strip()}") if prefijo else None
    if factura_cuenta is None:
        return _pantalla_revision(
            request, db, operador, borrador, error="Elegí Factura o Cuenta e indicá el número (sin espacios).",
            tipo=tipo, numero=numero, numero_salida=numero_salida,
        )

    async with turno_de(operador.whatsapp_numero):
        conversacion = _conversacion_bloqueada(db, operador)
        borrador = _mi_borrador_o_404(db, operador, movimiento_id)
        borrador.factura_o_cuenta_tipo, borrador.factura_o_cuenta_numero = factura_cuenta
        if servicios.reparto_abierto_del_operador(db, operador.id) is not None:
            resultado = servicios.registrar(db, operador, conversacion, borrador)
        else:
            resultado = _registrar_resolviendo_la_salida(db, operador, conversacion, borrador, numero_salida, movil_id)
        if isinstance(resultado, str):
            # No se pudo resolver la salida: la conversacion vuelve a quedar
            # esperando factura/cuenta (nunca en un paso del chat que la web no
            # usa), con lo cargado guardado para no volver a escribirlo.
            conversacion.estado = ConversationState.ESPERANDO_TIPO_FACTURA_CUENTA
            conversacion.numero_reparto_pendiente = None
            conversacion.movil_pendiente_numero = None
            db.commit()
            return _pantalla_revision(
                request, db, operador, borrador, error=resultado, tipo=tipo, numero=numero,
                numero_salida=numero_salida, pedir_movil=resultado == ELEGIR_MOVIL,
            )
    return templates.TemplateResponse(request, "m/comprobante_registrado.html", {"operador": operador, "registrado": resultado})


def _registrar_resolviendo_la_salida(
    db: Session, operador: Operator, conversacion: WhatsAppConversation, borrador: Movement, numero_salida: str, movil_id: str
) -> servicios.Registrado | str:
    """Sin salida abierta, el numero de salida (y el movil si el operador no
    tiene uno) vienen en el mismo envio. Devuelve el error a mostrar si no alcanza."""
    if not numero_salida.strip().isdigit():
        return "No tenés una salida abierta: indicá el número de salida."
    numero = int(numero_salida.strip())
    movil = db.get(Movil, int(movil_id)) if movil_id.isdigit() else None
    if movil is not None and movil.activo:
        conversacion.numero_reparto_pendiente = numero
        return servicios.registrar_en_movil(db, operador, conversacion, borrador, movil)
    resultado = servicios.resolver_salida_por_numero(db, operador, conversacion, borrador, numero)
    if isinstance(resultado, servicios.MovilConOtraSalida):
        return (
            f"El móvil {resultado.movil.numero} ya tiene abierta la Salida Nº {resultado.reparto.numero_reparto}. "
            "Indicá ese número de salida."
        )
    if isinstance(resultado, servicios.PedirMovil):
        return ELEGIR_MOVIL
    return resultado


@router.post("/m/comprobante/{movimiento_id}/descartar")
async def descartar(
    movimiento_id: int, request: Request, operador: Operator = Depends(require_operator), db: Session = Depends(get_db)
):
    _mi_borrador_o_404(db, operador, movimiento_id)
    async with turno_de(operador.whatsapp_numero):
        conversacion = _conversacion_bloqueada(db, operador)
        _mi_borrador_o_404(db, operador, movimiento_id)
        servicios.descartar_borrador(db, conversacion)
        db.commit()
    return RedirectResponse("/m/", status_code=303)


# ------------------------------------------------------------ mis comprobantes


@router.get("/m/comprobantes")
def mis_comprobantes(
    request: Request, fecha: str = "", operador: Operator = Depends(require_operator), db: Session = Depends(get_db)
):
    try:
        dia = datetime.strptime(fecha, "%Y-%m-%d").date()
    except ValueError:
        dia = hoy_argentina()
    movimientos = list(
        db.scalars(
            select(Movement)
            .where(
                Movement.operador_id == operador.id,
                Movement.estado_registro == RecordState.CONFIRMADO,
                del_dia(Movement.fecha_transaccion, dia),
            )
            .order_by(Movement.fecha_transaccion)
        )
    )
    total = sum((m.monto for m in movimientos if m.monto is not None), start=Decimal("0"))
    return templates.TemplateResponse(
        request,
        "m/mis_comprobantes.html",
        {"operador": operador, "movimientos": movimientos, "dia": dia, "total": formato_monto_ar(total), "TipoIdentificador": TipoIdentificador},
    )
