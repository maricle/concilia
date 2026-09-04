import re
from dataclasses import dataclass

_PATRON_INICIO = re.compile(r"^inicio\s+movil\s+(\S+)\s+reparto\s+nro\.?\s*(\d+)$", re.IGNORECASE)
_PATRON_CERRAR = re.compile(r"^cerrar\s+reparto\s+nro\.?\s*(\d+)$", re.IGNORECASE)


@dataclass
class IniciarRepartoComando:
    movil_numero: str
    numero_reparto: int


@dataclass
class CerrarRepartoComando:
    numero_reparto: int


def parse_comando_reparto(text: str) -> IniciarRepartoComando | CerrarRepartoComando | None:
    """Interpreta los comandos de texto libre de Telegram para iniciar/cerrar un
    reparto. Devuelve None si el texto no matchea ninguno de los dos patrones (en
    ese caso el llamador debe seguir el flujo normal de la conversacion)."""
    normalizado = text.strip()

    match_inicio = _PATRON_INICIO.match(normalizado)
    if match_inicio:
        return IniciarRepartoComando(movil_numero=match_inicio.group(1), numero_reparto=int(match_inicio.group(2)))

    match_cerrar = _PATRON_CERRAR.match(normalizado)
    if match_cerrar:
        return CerrarRepartoComando(numero_reparto=int(match_cerrar.group(1)))

    return None
