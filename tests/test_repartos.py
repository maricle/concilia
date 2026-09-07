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


def test_parse_iniciar_reparto_acepta_variante_iniciar():
    comando = parse_comando_reparto("iniciar movil M-01 reparto nro 5")
    assert comando == IniciarRepartoComando(movil_numero="M-01", numero_reparto=5)


def test_parse_cerrar_reparto_basico():
    comando = parse_comando_reparto("cerrar reparto nro 7")
    assert comando == CerrarRepartoComando(numero_reparto=7)


def test_parse_cerrar_reparto_con_punto_y_espacios():
    comando = parse_comando_reparto("  Cerrar Reparto Nro.  9  ")
    assert comando == CerrarRepartoComando(numero_reparto=9)


def test_parse_comando_reparto_devuelve_none_para_texto_random():
    assert parse_comando_reparto("hola, como estas?") is None


def test_parse_cerrar_sin_numero_queda_pendiente():
    # "cerrar"/"fin" solos (o sin un numero reconocible) cierran el reparto abierto
    # del operador sin que tenga que acordarse del numero.
    assert parse_comando_reparto("cerrar reparto nro") == CerrarRepartoComando(numero_reparto=None)
    assert parse_comando_reparto("cerrar") == CerrarRepartoComando(numero_reparto=None)
    assert parse_comando_reparto("fin") == CerrarRepartoComando(numero_reparto=None)
    assert parse_comando_reparto("fin de reparto") == CerrarRepartoComando(numero_reparto=None)
    assert parse_comando_reparto("cerrar reparto") == CerrarRepartoComando(numero_reparto=None)


def test_parse_cerrar_toma_el_primer_numero_sin_exigir_la_palabra_reparto():
    assert parse_comando_reparto("cerrar 234") == CerrarRepartoComando(numero_reparto=234)
    assert parse_comando_reparto("fin reparto nro 7") == CerrarRepartoComando(numero_reparto=7)


def test_parse_comando_reparto_devuelve_none_para_keyword_incorrecta():
    assert parse_comando_reparto("empezar movil M-01 reparto nro 1") is None
    assert parse_comando_reparto("terminar reparto nro 1") is None


def test_parse_inicio_reparto_orden_invertido():
    comando = parse_comando_reparto("iniciar reparto nro 5 movil M-01")
    assert comando == IniciarRepartoComando(movil_numero="M-01", numero_reparto=5)


def test_parse_inicio_solo_la_palabra_clave_deja_ambos_datos_pendientes():
    assert parse_comando_reparto("inicio") == IniciarRepartoComando(movil_numero=None, numero_reparto=None)
    assert parse_comando_reparto("iniciar") == IniciarRepartoComando(movil_numero=None, numero_reparto=None)


def test_parse_inicio_solo_con_movil_deja_reparto_pendiente():
    comando = parse_comando_reparto("inicio movil M-01")
    assert comando == IniciarRepartoComando(movil_numero="M-01", numero_reparto=None)


def test_parse_inicio_solo_con_numero_de_reparto_deja_movil_pendiente():
    comando = parse_comando_reparto("iniciar reparto nro 5")
    assert comando == IniciarRepartoComando(movil_numero=None, numero_reparto=5)


def test_parse_inicio_con_reparto_sin_numero_deja_numero_pendiente():
    comando = parse_comando_reparto("inicio movil M-01 reparto nro")
    assert comando == IniciarRepartoComando(movil_numero="M-01", numero_reparto=None)


def test_parse_inicio_seguido_de_texto_no_relacionado_devuelve_none():
    assert parse_comando_reparto("iniciar sesion en otra cosa") is None
