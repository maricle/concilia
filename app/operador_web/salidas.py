"""Salidas desde la interfaz web: iniciar y cerrar, con las mismas reglas que los
comandos del chat (app/servicios.py). Para iniciar se piden de entrada el numero
de salida y el movil: si esa salida ya esta abierta en el movil, el operador se
suma (y se le avisa); si no, se registra la salida nueva. Cerrar desde la web
no avisa a los companeros de la salida (decision del cliente; el chat sigue
avisando)."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import servicios
from ..canales import turno_de
from ..models import Movil, Operator
from ..numeros import formato_monto_ar
from .comun import moviles_activos, templates, texto_salida_ocupada
from .sesion import get_db, require_operator

router = APIRouter()


def _formulario(
    request: Request, db: Session, operador: Operator, *, error: str | None = None, aviso: str | None = None,
    numero: str = "", movil_id: str = "",
):
    return templates.TemplateResponse(
        request,
        "m/salida_iniciar.html",
        {
            "operador": operador,
            "moviles": moviles_activos(db),
            "numero": numero,
            "movil_id": movil_id,
            "error": error,
            "aviso": aviso,
        },
        status_code=400 if error else 200,
    )


def _resultado(request: Request, operador: Operator, titulo: str, detalle: str = "", lineas: list[str] | None = None):
    return templates.TemplateResponse(
        request, "m/resultado.html", {"operador": operador, "titulo": titulo, "detalle": detalle, "lineas": lineas or []}
    )


@router.get("/m/salida/iniciar")
def iniciar_form(request: Request, operador: Operator = Depends(require_operator), db: Session = Depends(get_db)):
    abierta = servicios.reparto_abierto_del_operador(db, operador.id)
    if abierta is not None:
        return templates.TemplateResponse(
            request, "m/salida_abierta.html", {"operador": operador, "abierta": abierta, "numero": "", "movil_id": ""}
        )
    return _formulario(request, db, operador)


@router.post("/m/salida/iniciar")
async def iniciar(
    request: Request,
    numero: str = Form(...),
    movil_id: str = Form(""),
    decision: str = Form(""),
    operador: Operator = Depends(require_operator),
    db: Session = Depends(get_db),
):
    numero = numero.strip()
    if not numero.isdigit():
        return _formulario(
            request, db, operador, error="Indicá el número de salida (solo números).", numero=numero, movil_id=movil_id
        )
    movil = db.get(Movil, int(movil_id)) if movil_id.isdigit() else None
    if movil is None:
        return _formulario(request, db, operador, error="Elegí el móvil.", numero=numero)
    if not movil.activo:
        return _formulario(request, db, operador, error="Ese móvil ya no está activo.", numero=numero)

    async with turno_de(operador.whatsapp_numero):
        abierta = servicios.reparto_abierto_del_operador(db, operador.id)
        if abierta is not None and abierta.numero_reparto == int(numero):
            return _resultado(
                request, operador, f"Ya estás en la Salida Nº {abierta.numero_reparto}", f"Móvil {abierta.movil.numero}."
            )
        if abierta is not None:
            if decision == "continuar":
                return RedirectResponse("/m/", status_code=303)
            if decision != "cerrar":
                return templates.TemplateResponse(
                    request,
                    "m/salida_abierta.html",
                    {"operador": operador, "abierta": abierta, "numero": numero, "movil_id": movil_id},
                )
            servicios.cerrar_salida(db, abierta, operador)
            db.commit()

        salida = servicios.ubicar_salida(db, int(numero), movil)
        if isinstance(salida, servicios.SalidaEnOtroMovil | servicios.MovilConOtraSalida):
            return _formulario(request, db, operador, error=texto_salida_ocupada(salida), numero=numero, movil_id=movil_id)
        if isinstance(salida, servicios.SalidaNueva):
            nueva = servicios.crear_salida(db, operador, salida.movil, int(numero))
            db.commit()
            return _resultado(request, operador, f"Salida Nº {nueva.numero_reparto} iniciada", f"Móvil {salida.movil.numero}.")
        # Ya abierta en ese movil (ej. la abrio el chofer o el panel): se suma.
        servicios.asociar_operador(db, salida, operador)
        db.commit()
    return _resultado(
        request,
        operador,
        f"Te sumaste a la Salida Nº {salida.numero_reparto}",
        f"La salida ya estaba abierta en el móvil {salida.movil.numero}.",
    )


def _lineas_totales(db: Session, reparto) -> list[str]:
    totales = servicios.totales_de_salida(db, reparto)
    sin_banco = servicios.SIN_BANCO_IDENTIFICADO
    lineas = [f"{banco}: {formato_monto_ar(monto)}" for banco, monto in sorted(totales.por_banco.items()) if banco != sin_banco]
    if sin_banco in totales.por_banco:
        lineas.append(f"{sin_banco}: {formato_monto_ar(totales.por_banco[sin_banco])}")
    if not totales.cantidad_comprobantes:
        lineas.append("Sin comprobantes registrados.")
    return lineas


@router.get("/m/salida/cerrar")
def cerrar_form(request: Request, operador: Operator = Depends(require_operator), db: Session = Depends(get_db)):
    abierta = servicios.reparto_abierto_del_operador(db, operador.id)
    if abierta is None:
        return RedirectResponse("/m/", status_code=303)
    return templates.TemplateResponse(
        request, "m/salida_cerrar.html", {"operador": operador, "abierta": abierta, "lineas": _lineas_totales(db, abierta)}
    )


@router.post("/m/salida/cerrar")
async def cerrar(request: Request, operador: Operator = Depends(require_operator), db: Session = Depends(get_db)):
    async with turno_de(operador.whatsapp_numero):
        abierta = servicios.reparto_abierto_del_operador(db, operador.id)
        if abierta is None:
            return RedirectResponse("/m/", status_code=303)
        servicios.cerrar_salida(db, abierta, operador)
        db.commit()
    return _resultado(
        request, operador, f"Salida Nº {abierta.numero_reparto} cerrada", "Total cobrado por banco:", _lineas_totales(db, abierta)
    )
