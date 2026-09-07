import re
from dataclasses import dataclass

_PALABRA_INICIO = re.compile(r"^inici(?:o|ar)\b", re.IGNORECASE)
_PATRON_MOVIL = re.compile(r"\bmovil\s+(\S+)", re.IGNORECASE)
_PATRON_NUMERO_REPARTO = re.compile(r"\breparto\s+nro\.?\s*(\d+)", re.IGNORECASE)
_PALABRA_CERRAR = re.compile(r"^(?:cerrar|fin)\b", re.IGNORECASE)
_PATRON_NUMERO = re.compile(r"(\d+)")


@dataclass
class IniciarRepartoComando:
    """movil_numero y numero_reparto pueden venir en None -- el operador puede
    mandar el comando incompleto (o solo la palabra "inicio"/"iniciar"), y el
    llamador es quien le pide el dato que falte antes de ejecutar el inicio."""

    movil_numero: str | None
    numero_reparto: int | None


@dataclass
class CerrarRepartoComando:
    """numero_reparto puede venir en None -- "cerrar"/"fin" a secas cierra el
    reparto abierto del operador sin que tenga que acordarse del numero."""

    numero_reparto: int | None


def parse_comando_reparto(text: str) -> IniciarRepartoComando | CerrarRepartoComando | None:
    """Interpreta los comandos de texto libre de Telegram para iniciar/cerrar un
    reparto. Devuelve None si el texto no matchea ninguno de los dos patrones (en
    ese caso el llamador debe seguir el flujo normal de la conversacion).

    El comando de inicio es flexible: la palabra "inicio"/"iniciar" siempre va
    primero, pero "movil X" y "reparto nro Y" pueden venir en cualquier orden
    despues, y cualquiera de los dos (o ambos) puede faltar -- en ese caso los
    campos correspondientes quedan en None.

    El comando de cierre acepta "cerrar" o "fin" solos, con o sin la palabra
    "reparto"/"nro", y con o sin numero -- toma el primer numero que encuentre
    en el resto del texto, o None si no hay ninguno."""
    normalizado = text.strip()

    match_inicio = _PALABRA_INICIO.match(normalizado)
    if match_inicio:
        resto = normalizado[match_inicio.end() :].strip()
        match_movil = _PATRON_MOVIL.search(resto)
        match_numero = _PATRON_NUMERO_REPARTO.search(resto)
        # Si sobra texto que no matchea "movil X" ni "reparto nro Y", no es un
        # comando de inicio valido (ej. "iniciar sesion" no deberia dispararlo).
        if resto and match_movil is None and match_numero is None:
            return None
        return IniciarRepartoComando(
            movil_numero=match_movil.group(1) if match_movil else None,
            numero_reparto=int(match_numero.group(1)) if match_numero else None,
        )

    match_cerrar = _PALABRA_CERRAR.match(normalizado)
    if match_cerrar:
        resto = normalizado[match_cerrar.end() :].strip()
        match_numero = _PATRON_NUMERO.search(resto)
        return CerrarRepartoComando(numero_reparto=int(match_numero.group(1)) if match_numero else None)

    return None
