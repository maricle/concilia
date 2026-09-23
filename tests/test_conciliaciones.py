from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import panel
from app.auth import hash_password
from app.db import Base
from app.main import app
from app.models import (
    BankAccount,
    CierreDiario,
    ImportedStatement,
    Movement,
    Movil,
    Operator,
    PanelUser,
    RecordState,
    ReconciliationState,
    Reparto,
    RepartoOperador,
    StatementLine,
    StatementLineState,
)


def _client_with_admin():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine)

    def override_get_db():
        with test_session() as session:
            yield session

    app.dependency_overrides[panel.get_db] = override_get_db

    with test_session() as session:
        session.add(PanelUser(nombre="Admin", email="admin@concilia.test", password_hash=hash_password("secreta123")))
        session.add(BankAccount(banco="Nacion", numero_cuenta="1", alias="Principal"))
        session.add(Operator(nombre="Ana", whatsapp_numero="111"))
        session.commit()

    return TestClient(app), test_session


def teardown_function():
    app.dependency_overrides.clear()


def _login(client):
    client.post("/login", data={"email": "admin@concilia.test", "password": "secreta123"})


def test_conciliaciones_page_requires_login():
    client, _ = _client_with_admin()
    response = client.get("/conciliaciones", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_importar_resumen_reconciles_matching_movement():
    client, test_session = _client_with_admin()
    _login(client)

    with test_session() as session:
        session.add(
            Movement(
                operador_id=1,
                monto=Decimal("500.00"),
                fecha_transaccion=datetime(2026, 8, 24),
                numero_operacion="OP-1",
                estado_registro=RecordState.CONFIRMADO,
            )
        )
        session.commit()

    csv_contenido = "Fecha,Importe,Descripcion,Referencia\n24/08/2026,500.00,Transferencia,OP-1\n"
    response = client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen.csv", csv_contenido, "text/csv")},
    )
    assert response.status_code == 200
    assert "Se importaron 1 transaccion nueva" in response.text

    pagina = client.get("/conciliaciones", params={"fecha": "2026-08-24", "banco": "Nacion"})
    assert "OP-1" in pagina.text
    assert "Conciliado" in pagina.text

    with test_session() as session:
        movimiento = session.query(Movement).filter_by(numero_operacion="OP-1").one()
        assert movimiento.estado_conciliacion == ReconciliationState.CONCILIADO
        assert movimiento.cuenta_bancaria_id == 1
        linea = session.query(StatementLine).one()
        assert linea.movimiento_id == movimiento.id


def test_importar_resumen_with_bad_file_shows_error():
    client, _ = _client_with_admin()
    _login(client)

    response = client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen.csv", "Descripcion,Referencia\nAlgo,OP-1\n", "text/csv")},
    )
    assert response.status_code == 400
    assert "columnas de fecha y monto" in response.text


def test_importar_resumen_omite_lineas_duplicadas_de_otro_resumen():
    """Pedido del usuario: si se sube un resumen nuevo (no "volver a revisar") con
    lineas que ya estaban cargadas en OTRO resumen de la misma cuenta -- ej. el
    mismo extracto subido dos veces por error -- no se duplican."""
    client, test_session = _client_with_admin()
    _login(client)

    csv_contenido = "Fecha,Importe,Descripcion,Referencia\n24/08/2026,500.00,Pago,OP-1\n25/08/2026,999.00,Otro,OP-2\n"
    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen1.csv", csv_contenido, "text/csv")},
    )

    # Se sube "de nuevo" como resumen aparte (no via /actualizar): una linea
    # repetida (OP-1) y una genuinamente nueva (OP-3).
    csv_contenido_2 = "Fecha,Importe,Descripcion,Referencia\n24/08/2026,500.00,Pago,OP-1\n26/08/2026,111.00,Nuevo,OP-3\n"
    response = client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-26"},
        files={"archivo": ("resumen2.csv", csv_contenido_2, "text/csv")},
    )

    assert response.status_code == 200
    assert "Se importaron 1 transaccion nueva" in response.text
    assert "Se omitieron 1 duplicada" in response.text
    with test_session() as session:
        assert session.query(StatementLine).count() == 3


def test_importar_resumen_reintenta_pendientes_viejas_de_la_cuenta():
    """Pedido del usuario: subir un resumen nuevo tambien reintenta emparejar las
    lineas pendientes de resumenes anteriores de esa cuenta, no solo las recien
    subidas -- mismo efecto que apretar "Reintentar conciliacion" pero automatico."""
    client, test_session = _client_with_admin()
    _login(client)

    # Primer resumen: una linea que en su momento no tenia con que matchear.
    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen1.csv", "Fecha,Importe,Descripcion,Referencia\n24/08/2026,500.00,Pago,OP-1\n", "text/csv")},
    )
    with test_session() as session:
        assert session.query(StatementLine).one().estado == StatementLineState.PENDIENTE

    # Recien ahora aparece el comprobante que le corresponde.
    with test_session() as session:
        session.add(
            Movement(
                operador_id=1,
                monto=Decimal("500.00"),
                fecha_transaccion=datetime(2026, 8, 24),
                numero_operacion="OP-1",
                estado_registro=RecordState.CONFIRMADO,
            )
        )
        session.commit()

    # Se sube un segundo resumen (de otro dia) -- no toca directamente la linea
    # vieja, pero deberia reintentar el emparejamiento igual.
    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-25"},
        files={"archivo": ("resumen2.csv", "Fecha,Importe,Descripcion,Referencia\n25/08/2026,50.00,Otro,OP-9\n", "text/csv")},
    )

    with test_session() as session:
        linea_vieja = session.query(StatementLine).filter_by(referencia="OP-1").one()
        assert linea_vieja.estado == StatementLineState.CONCILIADA
        assert linea_vieja.movimiento_id is not None


def test_importar_resumen_guarda_el_archivo_original_y_se_puede_descargar():
    client, test_session = _client_with_admin()
    _login(client)

    csv_contenido = "Fecha,Importe,Descripcion,Referencia\n24/08/2026,500.00,Transferencia,OP-1\n"
    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen.csv", csv_contenido, "text/csv")},
    )

    with test_session() as session:
        resumen = session.query(ImportedStatement).one()
        assert resumen.archivo_id is not None
        resumen_id = resumen.id

    response = client.get(f"/conciliaciones/resumenes/{resumen_id}/descargar")

    assert response.status_code == 200
    assert response.text == csv_contenido
    assert "resumen.csv" in response.headers["content-disposition"]


def test_actualizar_resumen_reemplaza_el_archivo_guardado():
    client, test_session = _client_with_admin()
    _login(client)

    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("v1.csv", "Fecha,Importe,Descripcion,Referencia\n24/08/2026,500.00,Uno,OP-1\n", "text/csv")},
    )
    with test_session() as session:
        resumen_id = session.query(ImportedStatement).one().id

    client.post(
        f"/conciliaciones/resumenes/{resumen_id}/actualizar",
        files={
            "archivo": (
                "v2.csv",
                "Fecha,Importe,Descripcion,Referencia\n24/08/2026,500.00,Uno,OP-1\n25/08/2026,999.00,Dos,OP-2\n",
                "text/csv",
            )
        },
    )

    response = client.get(f"/conciliaciones/resumenes/{resumen_id}/descargar")
    assert response.status_code == 200
    assert "v2.csv" in response.headers["content-disposition"]
    assert "OP-2" in response.text


def test_descargar_resumen_sin_archivo_guardado_redirige():
    """Resumenes importados antes de que existiera esta columna no tienen archivo
    guardado -- no debe romper, solo no ofrecer nada para descargar."""
    client, test_session = _client_with_admin()
    _login(client)

    with test_session() as session:
        resumen = ImportedStatement(
            cuenta_bancaria_id=1, fecha=datetime(2026, 8, 24), archivo_nombre="viejo.csv", formato="csv", usuario_id=1
        )
        session.add(resumen)
        session.commit()
        resumen_id = resumen.id

    response = client.get(f"/conciliaciones/resumenes/{resumen_id}/descargar", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/conciliaciones/resumenes"


def test_eliminar_resumen_revierte_los_movimientos_conciliados_a_pendiente():
    """Pedido del usuario: eliminar un resumen tiene que volver atras el estado de
    conciliacion de los movimientos que se hayan conciliado a traves de sus
    lineas -- no tiene sentido que sigan "Conciliados" contra un resumen borrado."""
    client, test_session = _client_with_admin()
    _login(client)

    with test_session() as session:
        session.add(
            Movement(
                operador_id=1,
                monto=Decimal("500.00"),
                fecha_transaccion=datetime(2026, 8, 24),
                numero_operacion="OP-1",
                estado_registro=RecordState.CONFIRMADO,
            )
        )
        session.commit()

    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen.csv", "Fecha,Importe,Descripcion,Referencia\n24/08/2026,500.00,Pago,OP-1\n", "text/csv")},
    )

    with test_session() as session:
        movimiento = session.query(Movement).one()
        assert movimiento.estado_conciliacion == ReconciliationState.CONCILIADO
        resumen_id = session.query(ImportedStatement).one().id

    response = client.post(
        f"/conciliaciones/resumenes/{resumen_id}/eliminar",
        data={"fecha": "2026-08-24", "banco": "Nacion"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with test_session() as session:
        assert session.query(ImportedStatement).count() == 0
        assert session.query(StatementLine).count() == 0
        movimiento = session.query(Movement).one()
        assert movimiento.estado_conciliacion == ReconciliationState.PENDIENTE


def test_eliminar_resumen_bloqueado_si_el_dia_esta_cerrado():
    client, test_session = _client_with_admin()
    _login(client)

    with test_session() as session:
        resumen = ImportedStatement(
            cuenta_bancaria_id=1, fecha=datetime(2026, 8, 24), archivo_nombre="resumen.csv", formato="csv", usuario_id=1
        )
        session.add(resumen)
        session.add(CierreDiario(fecha=datetime(2026, 8, 24).date(), cerrado_por_id=1))
        session.commit()
        resumen_id = resumen.id

    client.post(f"/conciliaciones/resumenes/{resumen_id}/eliminar")

    with test_session() as session:
        assert session.query(ImportedStatement).count() == 1


def test_listado_resumenes_muestra_los_mas_recientes_primero_y_filtra():
    client, test_session = _client_with_admin()
    _login(client)

    with test_session() as session:
        session.add(BankAccount(banco="Galicia", numero_cuenta="2", alias="galicia.demonte"))
        session.commit()

    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("primero.csv", "Fecha,Importe\n24/08/2026,100.00\n", "text/csv")},
    )
    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "2", "fecha": "2026-08-25"},
        files={"archivo": ("segundo.csv", "Fecha,Importe\n25/08/2026,200.00\n", "text/csv")},
    )

    pagina = client.get("/conciliaciones/resumenes")
    assert pagina.status_code == 200
    # El importado despues (segundo.csv, Galicia) aparece antes en la pagina.
    assert pagina.text.index("segundo.csv") < pagina.text.index("primero.csv")

    filtrada = client.get("/conciliaciones/resumenes", params={"banco": "Galicia"})
    assert "segundo.csv" in filtrada.text
    assert "primero.csv" not in filtrada.text


def test_emparejar_linea_manualmente():
    client, test_session = _client_with_admin()
    _login(client)

    with test_session() as session:
        session.add(
            Movement(
                operador_id=1,
                monto=Decimal("480.00"),
                fecha_transaccion=datetime(2026, 8, 20),
                numero_operacion="OP-DISTINTO",
                estado_registro=RecordState.CONFIRMADO,
            )
        )
        session.commit()

    csv_contenido = "Fecha,Importe,Descripcion,Referencia\n24/08/2026,500.00,Transferencia,OP-9\n"
    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen.csv", csv_contenido, "text/csv")},
    )

    with test_session() as session:
        linea = session.query(StatementLine).one()
        movimiento = session.query(Movement).filter_by(numero_operacion="OP-DISTINTO").one()
        linea_id, movimiento_id = linea.id, movimiento.id

    response = client.post(
        f"/conciliaciones/lineas/{linea_id}/emparejar",
        data={"movimiento_id": movimiento_id},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with test_session() as session:
        movimiento = session.get(Movement, movimiento_id)
        assert movimiento.estado_conciliacion == ReconciliationState.CONCILIADO_MANUALMENTE
        assert movimiento.cuenta_bancaria_id == 1


def test_emparejar_linea_rechaza_movimiento_ya_conciliado():
    """Bug real: elegir del dropdown un movimiento que ya estaba conciliado con
    otra linea lo reasignaba en silencio, dejando a la primera linea con una
    referencia obsoleta. Ahora se rechaza con un error en vez de reasignar."""
    client, test_session = _client_with_admin()
    _login(client)

    with test_session() as session:
        session.add(
            Movement(
                operador_id=1,
                monto=Decimal("500.00"),
                fecha_transaccion=datetime(2026, 8, 24),
                numero_operacion="OP-1",
                estado_registro=RecordState.CONFIRMADO,
            )
        )
        session.commit()

    # Primer resumen: matchea y concilia automaticamente ese movimiento.
    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen.csv", "Fecha,Importe,Descripcion,Referencia\n24/08/2026,500.00,Pago,OP-1\n", "text/csv")},
    )
    # Segundo resumen: otra linea, sin candidato automatico -- queda pendiente.
    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-25"},
        files={"archivo": ("resumen.csv", "Fecha,Importe,Descripcion,Referencia\n25/08/2026,999.00,Otro,\n", "text/csv")},
    )

    with test_session() as session:
        movimiento_id = session.query(Movement).filter_by(numero_operacion="OP-1").one().id
        linea_pendiente_id = session.query(StatementLine).filter_by(monto=Decimal("999.00")).one().id
        linea_conciliada = session.query(StatementLine).filter_by(monto=Decimal("500.00")).one()
        assert linea_conciliada.movimiento_id == movimiento_id

    response = client.post(
        f"/conciliaciones/lineas/{linea_pendiente_id}/emparejar",
        data={"movimiento_id": movimiento_id, "fecha": "2026-08-25", "banco": "Nacion"},
    )

    assert response.status_code == 400
    assert "ya esta conciliado" in response.text
    with test_session() as session:
        # La linea original conserva su match, la pendiente sigue sin uno.
        assert session.get(StatementLine, linea_pendiente_id).movimiento_id is None
        assert session.get(StatementLine, linea_conciliada.id).movimiento_id == movimiento_id


def test_marcar_linea_no_corresponde():
    client, test_session = _client_with_admin()
    _login(client)

    csv_contenido = "Fecha,Importe,Descripcion,Referencia\n24/08/2026,999.00,Deposito ajeno,\n"
    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen.csv", csv_contenido, "text/csv")},
    )

    with test_session() as session:
        linea = session.query(StatementLine).one()
        linea_id = linea.id

    response = client.post(f"/conciliaciones/lineas/{linea_id}/no-corresponde", follow_redirects=False)
    assert response.status_code == 303

    with test_session() as session:
        linea = session.get(StatementLine, linea_id)
        assert linea.estado.value == "no_corresponde"


def test_resumenes_table_shows_breakdown_by_line_state():
    client, test_session = _client_with_admin()
    _login(client)

    with test_session() as session:
        session.add_all(
            [
                Movement(
                    operador_id=1,
                    monto=Decimal("500.00"),
                    fecha_transaccion=datetime(2026, 8, 24),
                    numero_operacion="OP-CONCILIA",
                    estado_registro=RecordState.CONFIRMADO,
                ),
                Movement(
                    operador_id=1,
                    monto=Decimal("480.00"),
                    fecha_transaccion=datetime(2026, 8, 24),
                    numero_operacion="OP-DIFERENCIA",
                    estado_registro=RecordState.CONFIRMADO,
                ),
            ]
        )
        session.commit()

    csv_contenido = (
        "Fecha,Importe,Descripcion,Referencia\n"
        "24/08/2026,500.00,Transferencia,OP-CONCILIA\n"
        "24/08/2026,500.00,Transferencia,OP-DIFERENCIA\n"
        "24/08/2026,999.00,Sin match,OP-SIN-MATCH\n"
    )
    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen.csv", csv_contenido, "text/csv")},
    )

    pagina = client.get("/conciliaciones")

    assert pagina.status_code == 200
    assert "Pendiente" in pagina.text  # badge de estado del resumen, porque quedo 1 linea sin conciliar

    with test_session() as session:
        lineas = session.query(StatementLine).all()
        con_diferencia = session.query(Movement).filter_by(numero_operacion="OP-DIFERENCIA").one()
        assert con_diferencia.estado_conciliacion == ReconciliationState.CON_DIFERENCIA
        estados = sorted(l.estado.value for l in lineas)
        assert estados == ["conciliada", "conciliada", "pendiente"]


def test_panel_json_sin_resumen_status_when_no_statement_uploaded():
    client, test_session = _client_with_admin()
    _login(client)

    with test_session() as session:
        session.add(
            Movement(
                operador_id=1,
                monto=Decimal("500.00"),
                fecha_transaccion=datetime(2026, 8, 24),
                numero_operacion="OP-1",
                estado_registro=RecordState.CONFIRMADO,
                cuenta_bancaria_id=1,
            )
        )
        session.commit()

    respuesta = client.get("/conciliaciones/panel.json", params={"fecha": "2026-08-24", "banco": "Nacion"})
    assert respuesta.status_code == 200
    panel = respuesta.json()["panel"]
    assert panel["estado"] == "sin_resumen"
    assert panel["cantidad_comprobantes"] == 1
    assert panel["total_declarado"] == "500.00"
    assert panel["total_banco"] is None


def test_panel_json_conciliado_computes_totales_y_diferencia_cero():
    client, test_session = _client_with_admin()
    _login(client)

    with test_session() as session:
        session.add(
            Movement(
                operador_id=1,
                monto=Decimal("500.00"),
                fecha_transaccion=datetime(2026, 8, 24),
                numero_operacion="OP-1",
                estado_registro=RecordState.CONFIRMADO,
            )
        )
        session.commit()

    csv_contenido = "Fecha,Importe,Descripcion,Referencia\n24/08/2026,500.00,Transferencia,OP-1\n"
    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen.csv", csv_contenido, "text/csv")},
    )

    panel = client.get("/conciliaciones/panel.json", params={"fecha": "2026-08-24", "banco": "Nacion"}).json()["panel"]
    assert panel["estado"] == "conciliado"
    assert panel["total_declarado"] == "500.00"
    assert panel["total_banco"] == "500.00"
    assert panel["diferencia"] == "0.00"


def test_panel_json_a_revisar_when_pending_line_exists():
    client, test_session = _client_with_admin()
    _login(client)

    csv_contenido = "Fecha,Importe,Descripcion,Referencia\n24/08/2026,999.00,Sin match,OP-SIN-MATCH\n"
    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen.csv", csv_contenido, "text/csv")},
    )

    panel = client.get("/conciliaciones/panel.json", params={"fecha": "2026-08-24", "banco": "Nacion"}).json()["panel"]
    assert panel["estado"] == "a_revisar"


def test_movimientos_json_pagination_and_search():
    client, test_session = _client_with_admin()
    _login(client)

    with test_session() as session:
        session.add_all(
            [
                Movement(
                    operador_id=1,
                    monto=Decimal(str(100 + i)),
                    fecha_transaccion=datetime(2026, 8, 24, 10, i),
                    numero_operacion=f"OP-{i}",
                    titular="Cliente Uno" if i == 0 else "Cliente Dos",
                    estado_registro=RecordState.CONFIRMADO,
                    cuenta_bancaria_id=1,
                )
                for i in range(5)
            ]
        )
        session.commit()

    respuesta = client.get(
        "/conciliaciones/movimientos.json", params={"fecha": "2026-08-24", "banco": "Nacion", "page": 1, "page_size": 2}
    )
    data = respuesta.json()
    assert data["total"] == 5
    assert data["total_pages"] == 3
    assert len(data["items"]) == 2

    busqueda = client.get(
        "/conciliaciones/movimientos.json",
        params={"fecha": "2026-08-24", "banco": "Nacion", "search": "Cliente Uno"},
    )
    resultado = busqueda.json()
    assert resultado["total"] == 1
    assert resultado["items"][0]["titular"] == "Cliente Uno"


def test_movimientos_json_filtra_por_estado():
    client, test_session = _client_with_admin()
    _login(client)

    with test_session() as session:
        session.add_all(
            [
                Movement(
                    operador_id=1,
                    monto=Decimal("100"),
                    fecha_transaccion=datetime(2026, 8, 24),
                    numero_operacion="OP-PEND",
                    estado_registro=RecordState.CONFIRMADO,
                    estado_conciliacion=ReconciliationState.PENDIENTE,
                    cuenta_bancaria_id=1,
                ),
                Movement(
                    operador_id=1,
                    monto=Decimal("200"),
                    fecha_transaccion=datetime(2026, 8, 24),
                    numero_operacion="OP-CONC",
                    estado_registro=RecordState.CONFIRMADO,
                    estado_conciliacion=ReconciliationState.CONCILIADO,
                    cuenta_bancaria_id=1,
                ),
            ]
        )
        session.commit()

    respuesta = client.get(
        "/conciliaciones/movimientos.json",
        params={"fecha": "2026-08-24", "banco": "Nacion", "estado": "conciliado"},
    )
    data = respuesta.json()
    assert data["total"] == 1
    assert data["items"][0]["comprobante"] == "OP-CONC"


def test_salidas_json_agrupa_por_reparto_con_totales():
    client, test_session = _client_with_admin()
    _login(client)

    with test_session() as session:
        session.add(Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1))
        session.commit()
        session.add(
            Reparto(movil_id=1, fecha=datetime(2026, 8, 24).date(), hora_inicio=datetime(2026, 8, 24, 8, 0), numero_reparto=5)
        )
        session.commit()
        session.add(RepartoOperador(reparto_id=1, operador_id=1))
        session.add_all(
            [
                Movement(
                    operador_id=1,
                    monto=Decimal("300"),
                    fecha_transaccion=datetime(2026, 8, 24, 9, 0),
                    numero_operacion="OP-A",
                    estado_registro=RecordState.CONFIRMADO,
                    cuenta_bancaria_id=1,
                    reparto_id=1,
                ),
                Movement(
                    operador_id=1,
                    monto=Decimal("200"),
                    fecha_transaccion=datetime(2026, 8, 24, 10, 0),
                    numero_operacion="OP-B",
                    estado_registro=RecordState.CONFIRMADO,
                    cuenta_bancaria_id=1,
                    reparto_id=1,
                ),
            ]
        )
        session.commit()

    respuesta = client.get("/conciliaciones/salidas.json", params={"fecha": "2026-08-24", "banco": "Nacion"})
    data = respuesta.json()
    assert data["total"] == 1
    salida = data["items"][0]
    assert salida["numero_reparto"] == 5
    assert salida["cantidad_comprobantes"] == 2
    assert salida["total"] == "500.00"
    assert salida["operador"] == "Ana"


def test_bancos_sin_identificar_no_se_suman_a_ningun_banco():
    client, test_session = _client_with_admin()
    _login(client)

    with test_session() as session:
        session.add(
            Movement(
                operador_id=1,
                monto=Decimal("777.00"),
                fecha_transaccion=datetime(2026, 8, 24),
                numero_operacion="OP-SIN-BANCO",
                estado_registro=RecordState.CONFIRMADO,
                cuenta_bancaria_id=None,
            )
        )
        session.commit()

    panel = client.get("/conciliaciones/panel.json", params={"fecha": "2026-08-24", "banco": "Nacion"}).json()
    assert panel["panel"]["total_declarado"] == "0"
    assert panel["sin_banco_cantidad"] == 1
    assert panel["sin_banco_total"] == "777.00"


def test_cerrar_dia_bloqueado_mientras_hay_banco_a_revisar():
    client, test_session = _client_with_admin()
    _login(client)

    csv_contenido = "Fecha,Importe,Descripcion,Referencia\n24/08/2026,999.00,Sin match,OP-SIN-MATCH\n"
    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen.csv", csv_contenido, "text/csv")},
    )

    respuesta = client.post("/conciliaciones/cierres", data={"fecha": "2026-08-24"})
    assert respuesta.status_code == 400
    assert "pendientes de revision" in respuesta.text

    with test_session() as session:
        assert session.query(CierreDiario).count() == 0


def test_cerrar_dia_y_reabrir():
    client, test_session = _client_with_admin()
    _login(client)

    respuesta = client.post("/conciliaciones/cierres", data={"fecha": "2026-08-24"}, follow_redirects=False)
    assert respuesta.status_code == 303

    with test_session() as session:
        assert session.query(CierreDiario).count() == 1

    respuesta = client.post("/conciliaciones/cierres/2026-08-24/reabrir", follow_redirects=False)
    assert respuesta.status_code == 303
    with test_session() as session:
        assert session.query(CierreDiario).count() == 0


def test_dia_cerrado_bloquea_nuevo_resumen():
    client, test_session = _client_with_admin()
    _login(client)

    with test_session() as session:
        session.add(CierreDiario(fecha=datetime(2026, 8, 24).date(), cerrado_por_id=1))
        session.commit()

    csv_contenido = "Fecha,Importe,Descripcion,Referencia\n24/08/2026,500.00,Transferencia,OP-1\n"
    respuesta = client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen.csv", csv_contenido, "text/csv")},
    )
    assert respuesta.status_code == 400
    assert "ya esta cerrado" in respuesta.text

    with test_session() as session:
        assert session.query(StatementLine).count() == 0


def test_legacy_resumen_id_redirige_a_fecha_banco():
    client, test_session = _client_with_admin()
    _login(client)

    csv_contenido = "Fecha,Importe,Descripcion,Referencia\n24/08/2026,500.00,Transferencia,OP-1\n"
    client.post(
        "/conciliaciones/importar",
        data={"cuenta_bancaria_id": "1", "fecha": "2026-08-24"},
        files={"archivo": ("resumen.csv", csv_contenido, "text/csv")},
    )
    with test_session() as session:
        resumen_id = session.query(StatementLine).one().resumen_id

    respuesta = client.get("/conciliaciones", params={"resumen_id": resumen_id}, follow_redirects=False)
    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/conciliaciones?fecha=2026-08-24&banco=Nacion"
