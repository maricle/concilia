from app.repartos import CerrarRepartoComando, IniciarRepartoComando, parse_comando_reparto


def test_parse_inicio_reparto_basico():
    comando = parse_comando_reparto("inicio movil M-01 reparto nro 5")
    assert comando == IniciarRepartoComando(movil_numero="M-01", numero_reparto=5)


def test_parse_inicio_reparto_case_insensitive_y_espacios_extra():
    comando = parse_comando_reparto("  INICIO   MOVIL   m-02   REPARTO   NRO   12  ")
    assert comando == IniciarRepartoComando(movil_numero="m-02", numero_reparto=12)


def test_parse_inicio_reparto_con_punto_en_nro():
    comando = parse_comando_reparto("inicio movil M-01 reparto nro. 3")
    assert comando == IniciarRepartoComando(movil_numero="M-01", numero_reparto=3)


def test_parse_cerrar_reparto_basico():
    comando = parse_comando_reparto("cerrar reparto nro 7")
    assert comando == CerrarRepartoComando(numero_reparto=7)


def test_parse_cerrar_reparto_con_punto_y_espacios():
    comando = parse_comando_reparto("  Cerrar Reparto Nro.  9  ")
    assert comando == CerrarRepartoComando(numero_reparto=9)


def test_parse_comando_reparto_devuelve_none_para_texto_random():
    assert parse_comando_reparto("hola, como estas?") is None


def test_parse_comando_reparto_devuelve_none_si_falta_numero():
    assert parse_comando_reparto("inicio movil M-01 reparto nro") is None
    assert parse_comando_reparto("cerrar reparto nro") is None


def test_parse_comando_reparto_devuelve_none_para_keyword_incorrecta():
    assert parse_comando_reparto("empezar movil M-01 reparto nro 1") is None
    assert parse_comando_reparto("cerrar movil M-01") is None
