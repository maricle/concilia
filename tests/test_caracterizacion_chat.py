"""Tests de caracterizacion del chat (WhatsApp/Telegram).

Graban el comportamiento ACTUAL de ConversationService -- cada respuesta de
texto, el teclado que muestra el canal, el estado de la conversacion, las
notificaciones a otros operadores y como queda la base al final -- en una serie
de conversaciones completas, y lo comparan contra tests/datos/caracterizacion_chat.json.

Sirven de red para refactors que no deberian cambiar nada visible para el
operador (ej. extraer servicios compartidos con la interfaz web): si un cambio
altera una sola palabra de una respuesta, este test falla y muestra la diferencia.

Si un cambio de comportamiento es intencional, se regenera la referencia con
    CARACTERIZACION_REGENERAR=1 pytest tests/test_caracterizacion_chat.py
y se revisa el diff del JSON antes de commitearlo.
"""

import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.canales import teclado_para
from app.conversation import ConversationService, ExtractedTransfer
from app.db import Base
from app.models import (
    BankAccount,
    Movement,
    Movil,
    Operator,
    Reparto,
    RepartoOperador,
    WhatsAppConversation,
)

REFERENCIA = Path(__file__).parent / "datos" / "caracterizacion_chat.json"
ANA = "5491111111111"
BETO = "5491122222222"


def _transfer(numero_operacion: str, monto: str = "15000.00", cuenta: str = "empresa.mp") -> ExtractedTransfer:
    return ExtractedTransfer(
        Decimal(monto),
        datetime(2026, 10, 2, 14, 35),
        numero_operacion,
        banco_emisor="Galicia",
        cuenta_receptora=cuenta,
        titular="Juan Perez",
    )


def _base(*, con_cuentas: bool = True) -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add_all([Operator(nombre="Ana", whatsapp_numero=ANA), Operator(nombre="Beto", whatsapp_numero=BETO)])
    if con_cuentas:
        db.add_all(
            [
                BankAccount(banco="Mercado Pago", numero_cuenta="1", alias="empresa.mp"),
                BankAccount(banco="Galicia", numero_cuenta="2", alias="empresa.galicia"),
            ]
        )
    db.commit()
    db.add_all(
        [
            Movil(numero="M-01", nombre="Camion 1", responsable_operador_id=1),
            Movil(numero="M-02", nombre="Camion 2", responsable_operador_id=2),
        ]
    )
    db.commit()
    return db


def _salida_abierta(db: Session, operador_id: int, movil_id: int, numero: int) -> None:
    reparto = Reparto(movil_id=movil_id, fecha=datetime(2026, 10, 2).date(), hora_inicio=datetime(2026, 10, 2, 8), numero_reparto=numero)
    db.add(reparto)
    db.flush()
    db.add(RepartoOperador(reparto_id=reparto.id, operador_id=operador_id))
    db.commit()


def _correr(db: Session, pasos: list[tuple]) -> dict:
    """Ejecuta los pasos y devuelve la transcripcion + la foto final de la base.
    Paso: ("texto", numero, texto) | ("foto", numero, ExtractedTransfer, caption) | ("pendiente", numero)."""
    service = ConversationService(db)
    transcripcion = []
    for paso in pasos:
        tipo, numero = paso[0], paso[1]
        if tipo == "texto":
            entrada = paso[2]
            respuesta = service.handle_text(numero, entrada)
        elif tipo == "foto":
            entrada = f"[foto {paso[2].numero_operacion}" + (f" + '{paso[3]}']" if paso[3] else "]")
            respuesta = service.start_transfer(numero, paso[2], None, caption=paso[3])
        else:
            entrada = "[otra foto, con algo pendiente]"
            respuesta = service.pending_prompt(numero)
        conversacion = db.get(WhatsAppConversation, numero)
        teclado = teclado_para(service, numero)
        transcripcion.append(
            {
                "operador": numero,
                "entrada": entrada,
                "respuesta": respuesta,
                "teclado": teclado.value if teclado else None,
                "estado": conversacion.estado.value if conversacion else None,
                "notificaciones": [list(n) for n in service.pop_notificaciones()],
            }
        )

    movimientos = [
        {
            "numero_operacion": m.numero_operacion,
            "estado_registro": m.estado_registro.value,
            "factura_o_cuenta": f"{m.factura_o_cuenta_tipo.value if m.factura_o_cuenta_tipo else None}:{m.factura_o_cuenta_numero}",
            "cuenta_bancaria_id": m.cuenta_bancaria_id,
            "movil_id": m.movil_id,
            "reparto_id": m.reparto_id,
            "origen": m.origen,
        }
        for m in db.scalars(select(Movement).order_by(Movement.id))
    ]
    repartos = [
        {
            "id": r.id,
            "numero": r.numero_reparto,
            "movil_id": r.movil_id,
            "cerrado": r.hora_fin is not None,
            "operadores": sorted(
                ro.operador_id for ro in db.scalars(select(RepartoOperador).where(RepartoOperador.reparto_id == r.id))
            ),
        }
        for r in db.scalars(select(Reparto).order_by(Reparto.id))
    ]
    operadores = {o.nombre: {"movil_id": o.movil_id} for o in db.scalars(select(Operator).order_by(Operator.id))}
    return {"transcripcion": transcripcion, "movimientos": movimientos, "repartos": repartos, "operadores": operadores}


def _escenarios() -> dict:
    escenarios = {}

    db = _base()
    _salida_abierta(db, 1, 1, 5)
    escenarios["foto_sin_texto_tipo_y_numero_juntos"] = _correr(
        db, [("foto", ANA, _transfer("OP-1"), None), ("texto", ANA, "F 12345")]
    )

    db = _base()
    _salida_abierta(db, 1, 1, 5)
    escenarios["foto_sin_texto_con_botones"] = _correr(
        db,
        [
            ("foto", ANA, _transfer("OP-1"), None),
            ("texto", ANA, "hola"),
            ("texto", ANA, "factura"),
            ("texto", ANA, "12345"),
        ],
    )

    db = _base()
    _salida_abierta(db, 1, 1, 5)
    escenarios["foto_con_caption_y_si"] = _correr(
        db, [("foto", ANA, _transfer("OP-1"), "c 987"), ("texto", ANA, "tal vez"), ("texto", ANA, "si")]
    )

    db = _base()
    escenarios["sin_salida_numero_no_abierto_pide_movil"] = _correr(
        db,
        [
            ("foto", ANA, _transfer("OP-1"), "F 1"),
            ("texto", ANA, "si"),
            ("texto", ANA, "siete"),
            ("texto", ANA, "7"),
            ("texto", ANA, "M-01"),
        ],
    )

    db = _base()
    escenarios["sin_salida_sin_movil_pide_movil"] = _correr(
        db,
        [
            ("foto", ANA, _transfer("OP-1"), None),
            ("texto", ANA, "F 1"),
            ("texto", ANA, "8"),
            ("texto", ANA, "M-99"),
            ("texto", ANA, "m01"),
        ],
    )

    db = _base()
    _salida_abierta(db, 2, 2, 3)
    escenarios["sin_salida_se_suma_a_la_del_companero"] = _correr(
        db, [("foto", ANA, _transfer("OP-1"), "F 1"), ("texto", ANA, "si"), ("texto", ANA, "3")]
    )

    db = _base()
    _salida_abierta(db, 2, 1, 4)
    escenarios["sin_salida_movil_elegido_con_otra_salida"] = _correr(
        db,
        [
            ("foto", ANA, _transfer("OP-1"), "F 1"),
            ("texto", ANA, "si"),
            ("texto", ANA, "9"),
            ("texto", ANA, "M-01"),
            ("texto", ANA, "4"),
        ],
    )

    db = _base()
    _salida_abierta(db, 2, 2, 3)
    escenarios["inicio_incompleto_se_suma_a_la_salida_abierta"] = _correr(
        db, [("texto", ANA, "inicio"), ("texto", ANA, "3")]
    )

    db = _base()
    escenarios["pendientes_y_descartes"] = _correr(
        db,
        [
            ("foto", ANA, _transfer("OP-1"), None),
            ("pendiente", ANA),
            ("texto", ANA, "inicio movil M-01 salida nro 5"),
            ("texto", ANA, "descartar"),
            ("foto", ANA, _transfer("OP-2"), None),
            ("texto", ANA, "cuenta"),
            ("pendiente", ANA),
            ("texto", ANA, "no"),
            ("foto", ANA, _transfer("OP-3"), "F 9"),
            ("pendiente", ANA),
            ("texto", ANA, "cancelar"),
            ("foto", ANA, _transfer("OP-4"), "F 9"),
            ("texto", ANA, "si"),
            ("pendiente", ANA),
            ("texto", ANA, "descartar"),
        ],
    )

    db = _base()
    _salida_abierta(db, 1, 1, 5)
    db.add(Movement(operador_id=2, monto=Decimal("1"), fecha_transaccion=datetime(2026, 10, 1), numero_operacion="OP-DUP"))
    db.commit()
    escenarios["duplicado_y_cuenta_no_identificada"] = _correr(
        db,
        [
            ("foto", ANA, _transfer("OP-DUP"), None),
            ("foto", ANA, _transfer("OP-X", cuenta="0000000000000000000099"), "F 1"),
            ("texto", ANA, "si"),
        ],
    )

    db = _base(con_cuentas=False)
    escenarios["sin_cuentas_cargadas"] = _correr(db, [("foto", ANA, _transfer("OP-1"), None)])

    db = _base()
    escenarios["salidas_inicio_cierre_y_companeros"] = _correr(
        db,
        [
            ("texto", ANA, "inicio movil M-01 salida nro 5"),
            ("texto", ANA, "quizas"),
            ("texto", ANA, "si"),
            ("texto", ANA, "inicio movil M-01 salida nro 5"),
            ("texto", BETO, "inicio"),
            ("texto", BETO, "cinco"),
            ("texto", BETO, "5"),
            ("texto", BETO, "M-01"),
            ("foto", ANA, _transfer("OP-1"), "F 1"),
            ("texto", ANA, "si"),
            ("foto", BETO, _transfer("OP-2", monto="8000.00", cuenta="empresa.galicia"), "C 2"),
            ("texto", BETO, "si"),
            ("texto", ANA, "inicio movil M-02 salida nro 6"),
            ("texto", ANA, "otra cosa"),
            ("texto", ANA, "continuar"),
            ("texto", ANA, "inicio movil M-02 salida nro 6"),
            ("texto", ANA, "cerrar"),
            ("texto", ANA, "cerrar"),
            ("texto", BETO, "fin"),
            ("texto", BETO, "cerrar"),
        ],
    )

    db = _base()
    escenarios["salida_cancelada_y_comandos_bloqueados"] = _correr(
        db,
        [
            ("texto", ANA, "inicio movil M-01 salida nro 5"),
            ("texto", ANA, "no"),
            ("texto", ANA, "inicio movil M-77 salida nro 5"),
            ("foto", ANA, _transfer("OP-1"), None),
            ("texto", ANA, "cerrar"),
            ("texto", ANA, "hola"),
        ],
    )

    db = _base()
    escenarios["numero_no_habilitado"] = _correr(db, [("texto", "5490000000000", "hola")])
    return escenarios


def test_el_chat_se_comporta_igual_que_la_referencia():
    obtenido = _escenarios()
    if os.environ.get("CARACTERIZACION_REGENERAR"):
        REFERENCIA.parent.mkdir(exist_ok=True)
        REFERENCIA.write_text(json.dumps(obtenido, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pytest.skip("Referencia regenerada: revisar el diff antes de commitear.")
    esperado = json.loads(REFERENCIA.read_text(encoding="utf-8"))
    assert obtenido.keys() == esperado.keys()
    for nombre in esperado:
        assert obtenido[nombre] == esperado[nombre], nombre
