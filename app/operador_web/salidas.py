"""Salidas desde la interfaz web: iniciar y cerrar, con las mismas reglas que los
comandos del chat (app/servicios.py). Cerrar desde la web no avisa a los
companeros de la salida (decision del cliente; el chat sigue avisando)."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import servicios
from ..canales import turno_de
from ..models import Movil, Operator
from ..numeros import formato_monto_ar
from .comun import templates
from .sesion import get_db, require_operator

router = APIRouter()


def _moviles_activos(db: Session) -> list[Movil]:
    return list(db.scalars(select(Movil).where(Movil.activo.is_(True)).order_by(Movil.numero)))


def _movil_asignado(db: Session, operador: Operator) -> Movil | None:
    movil = db.get(Movil, operador.movil_id) if operador.movil_id else None
    return movil if movil is not None and movil.activo else None


def _formulario(request: Request, db: Session, operador: Operator, *, error: str | None = None, numero: str = ""):
    return templates.TemplateResponse(
        request,
        "m/salida_iniciar.html",
        {
            "operador": operador,
            "movil_asignado": _movil_asignado(db, operador),
            "moviles": _moviles_activos(db),
            "numero": numero,
            "error": error,
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
        return _formulario(request, db, operador, error="Indicá el número de salida (solo números).", numero=numero)
    movil = db.get(Movil, int(movil_id)) if movil_id.isdigit() else _movil_asignado(db, operador)
    if movil is None or not movil.activo:
        return _formulario(request, db, operador, error="Elegí el móvil de la salida.", numero=numero)

    async with turno_de(operador.whatsapp_numero):
        abierta = servicios.reparto_abierto_del_operador(db, operador.id)
        if abierta is not None and abierta.movil_id == movil.id:
            return _resultado(request, operador, f"Ya estás en la Salida Nº {abierta.numero_reparto}", f"Móvil {movil.numero}.")
        if abierta is not None:
            if decision == "continuar":
                return RedirectResponse("/m/", status_code=303)
            if decision != "cerrar":
                return templates.TemplateResponse(
                    request,
                    "m/salida_abierta.html",
                    {"operador": operador, "abierta": abierta, "numero": numero, "movil_id": movil.id},
                )
            servicios.cerrar_salida(db, abierta, operador)

        de_otro = servicios.reparto_abierto_del_movil(db, movil.id)
        if de_otro is not None:
            # Igual que en el chat: el movil ya esta en salida (ej. la abrio el
            # chofer); el operador se suma sin abrir otra.
            servicios.asociar_operador(db, de_otro, operador)
            db.commit()
            return _resultado(
                request, operador, f"Te sumaste a la Salida Nº {de_otro.numero_reparto}", f"Móvil {movil.numero}."
            )
        nueva = servicios.crear_salida(db, operador, movil, int(numero))
        db.commit()
    return _resultado(request, operador, f"Salida Nº {nueva.numero_reparto} iniciada", f"Móvil {movil.numero}.")


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
