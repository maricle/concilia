from datetime import datetime, timezone

from app.zona_horaria import ahora_argentina, hoy_argentina


def test_ahora_argentina_es_utc_menos_3():
    real_utc = datetime.now(timezone.utc)
    resultado = ahora_argentina()

    assert resultado.tzinfo is None  # naive, para comparar con las columnas existentes
    diferencia_horas = round((real_utc.replace(tzinfo=None) - resultado).total_seconds() / 3600)
    assert diferencia_horas == 3  # Argentina = UTC-3, sin horario de verano


def test_hoy_argentina_coincide_con_la_fecha_de_ahora_argentina():
    assert hoy_argentina() == ahora_argentina().date()
