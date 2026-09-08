from datetime import date, datetime
from zoneinfo import ZoneInfo

_ARGENTINA = ZoneInfo("America/Argentina/Buenos_Aires")


def ahora_argentina() -> datetime:
    """Hora actual en Argentina (UTC-3, sin horario de verano), como datetime
    naive. El servidor corre en UTC (Railway); todas las columnas de fecha/hora
    del proyecto son naive y se comparan/muestran asumiendo hora argentina, asi
    que hay que convertir aca y no en cada lugar que registra un timestamp --
    usar esto (o hoy_argentina) en vez de datetime.utcnow()/date.today()."""
    return datetime.now(_ARGENTINA).replace(tzinfo=None)


def hoy_argentina() -> date:
    return ahora_argentina().date()
