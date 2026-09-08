from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .zona_horaria import ahora_argentina


class ConversationState(StrEnum):
    ESPERANDO_COMPROBANTE = "esperando_comprobante"
    ESPERANDO_CONFIRMACION_DATOS = "esperando_confirmacion_datos"
    ESPERANDO_TIPO_FACTURA_CUENTA = "esperando_tipo_factura_cuenta"
    ESPERANDO_NUMERO_FACTURA_CUENTA = "esperando_numero_factura_cuenta"
    ESPERANDO_CONFIRMACION_FINAL = "esperando_confirmacion_final"
    ESPERANDO_MOVIL = "esperando_movil"
    ESPERANDO_DECISION_REPARTO_ABIERTO = "esperando_decision_reparto_abierto"
    ESPERANDO_DATOS_INICIO_REPARTO = "esperando_datos_inicio_reparto"
    ESPERANDO_CONFIRMACION_INICIO_REPARTO = "esperando_confirmacion_inicio_reparto"
    ESPERANDO_CONFIRMACION_CREAR_REPARTO = "esperando_confirmacion_crear_reparto"
    ESPERANDO_CUENTA_BANCARIA = "esperando_cuenta_bancaria"


class TipoIdentificador(StrEnum):
    FACTURA = "factura"
    CUENTA = "cuenta"


class RecordState(StrEnum):
    PENDIENTE_CONFIRMACION = "pendiente_confirmacion"
    CONFIRMADO = "confirmado"


class ReconciliationState(StrEnum):
    PENDIENTE = "pendiente"
    CONCILIADO = "conciliado"
    CONCILIADO_MANUALMENTE = "conciliado_manualmente"
    CON_DIFERENCIA = "con_diferencia"


class StatementLineState(StrEnum):
    PENDIENTE = "pendiente"
    CONCILIADA = "conciliada"
    NO_CORRESPONDE = "no_corresponde"


class Operator(Base):
    __tablename__ = "operadores"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(150))
    whatsapp_numero: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(30), unique=True, index=True)
    tipo: Mapped[str] = mapped_column(String(50), default="Reparto")
    activo: Mapped[bool] = mapped_column(default=True)
    fecha_alta: Mapped[datetime] = mapped_column(DateTime, default=ahora_argentina)
    # use_alter rompe el FK circular operadores<->moviles al crear las tablas desde
    # cero (create_all() sobre una base nueva, como en los tests); en produccion no
    # afecta nada porque operadores ya existe y create_all() no la vuelve a tocar.
    movil_id: Mapped[int | None] = mapped_column(
        ForeignKey("moviles.id", use_alter=True, name="fk_operadores_movil_id"), index=True
    )

    movil: Mapped["Movil | None"] = relationship(foreign_keys=[movil_id])


class BankAccount(Base):
    __tablename__ = "cuentas_bancarias"

    id: Mapped[int] = mapped_column(primary_key=True)
    banco: Mapped[str] = mapped_column(String(120))
    numero_cuenta: Mapped[str] = mapped_column(String(60))
    moneda: Mapped[str] = mapped_column(String(10), default="ARS")
    alias: Mapped[str] = mapped_column(String(120))


class Movement(Base):
    __tablename__ = "movimientos"
    __table_args__ = (
        UniqueConstraint("numero_operacion", name="uq_movimiento_operacion"),
        Index("ix_movimientos_movil_fecha_transaccion", "movil_id", "fecha_transaccion"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operador_id: Mapped[int] = mapped_column(ForeignKey("operadores.id"))
    fecha_transaccion: Mapped[datetime | None] = mapped_column(DateTime)
    fecha_subida: Mapped[datetime] = mapped_column(DateTime, default=ahora_argentina)
    monto: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    banco_emisor: Mapped[str | None] = mapped_column(String(120))
    cuenta_receptora_extraida: Mapped[str | None] = mapped_column(String(150))
    numero_operacion: Mapped[str | None] = mapped_column(String(120), index=True)
    titular: Mapped[str | None] = mapped_column(String(150))
    factura_o_cuenta_tipo: Mapped[TipoIdentificador | None] = mapped_column(default=None)
    factura_o_cuenta_numero: Mapped[str | None] = mapped_column(String(150))
    cuenta_bancaria_id: Mapped[int | None] = mapped_column(ForeignKey("cuentas_bancarias.id"))
    archivo_url: Mapped[str | None] = mapped_column(Text)
    archivo_id: Mapped[int | None] = mapped_column(ForeignKey("comprobantes_archivo.id"))
    origen: Mapped[str] = mapped_column(String(20), default="whatsapp")
    estado_registro: Mapped[RecordState] = mapped_column(default=RecordState.PENDIENTE_CONFIRMACION)
    estado_conciliacion: Mapped[ReconciliationState] = mapped_column(default=ReconciliationState.PENDIENTE)
    # movil vigente del operador al momento de registrarse el movimiento -- no se
    # resuelve dinamicamente despues, queda fijo aunque el operador cambie de movil.
    movil_id: Mapped[int | None] = mapped_column(ForeignKey("moviles.id"), index=True)
    # reparto abierto del movil al momento de confirmarse el movimiento -- misma
    # logica de "foto del momento" que movil_id, null si no habia ningun reparto
    # abierto en ese movil cuando se confirmo.
    reparto_id: Mapped[int | None] = mapped_column(ForeignKey("repartos.id"), index=True)

    operador: Mapped[Operator] = relationship()
    cuenta_bancaria: Mapped[BankAccount | None] = relationship()
    archivo: Mapped["ComprobanteArchivo | None"] = relationship()
    movil: Mapped["Movil | None"] = relationship()
    reparto: Mapped["Reparto | None"] = relationship()


class Movil(Base):
    __tablename__ = "moviles"

    id: Mapped[int] = mapped_column(primary_key=True)
    numero: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    nombre: Mapped[str] = mapped_column(String(150))
    descripcion: Mapped[str | None] = mapped_column(String(255))
    responsable_operador_id: Mapped[int | None] = mapped_column(ForeignKey("operadores.id"), index=True)
    activo: Mapped[bool] = mapped_column(default=True)
    fecha_alta: Mapped[datetime] = mapped_column(DateTime, default=ahora_argentina)

    responsable: Mapped["Operator | None"] = relationship(foreign_keys=[responsable_operador_id])


class Reparto(Base):
    """Turno de reparto de un movil en un dia -- un movil puede tener varios en
    el mismo dia (turno manana/tarde), pero nunca dos abiertos (hora_fin nula) a
    la vez. Puede tener varios operadores asociados en simultaneo (ver
    RepartoOperador, ej. chofer + ayudante, o un cambio de turno donde el que
    entra se suma antes de que el que sale cierre); cualquiera de los asociados
    puede cerrarlo, y al cerrarse queda cerrado para todos."""

    __tablename__ = "repartos"
    __table_args__ = (Index("ix_repartos_movil_fecha", "movil_id", "fecha"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    movil_id: Mapped[int] = mapped_column(ForeignKey("moviles.id"))
    fecha: Mapped[date] = mapped_column(Date)
    hora_inicio: Mapped[datetime] = mapped_column(DateTime)
    hora_fin: Mapped[datetime | None] = mapped_column(DateTime)
    numero_reparto: Mapped[int | None] = mapped_column(Integer)
    comentarios: Mapped[str | None] = mapped_column(String(500))
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=ahora_argentina)

    movil: Mapped[Movil] = relationship()


class RepartoOperador(Base):
    """Asociacion entre un reparto y cada operador que trabajo en el (puede haber
    mas de uno: chofer + ayudante, o un cambio de turno). Un operador solo puede
    estar asociado a un reparto ABIERTO a la vez -- eso se valida en
    ConversationService, no aca (esta tabla no lo impide por si sola, dos filas
    con reparto_id distintos y el mismo operador son validas si uno de esos
    repartos ya esta cerrado)."""

    __tablename__ = "reparto_operadores"
    __table_args__ = (UniqueConstraint("reparto_id", "operador_id", name="uq_reparto_operador"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    reparto_id: Mapped[int] = mapped_column(ForeignKey("repartos.id"), index=True)
    operador_id: Mapped[int] = mapped_column(ForeignKey("operadores.id"), index=True)
    asociado_en: Mapped[datetime] = mapped_column(DateTime, default=ahora_argentina)

    reparto: Mapped[Reparto] = relationship()
    operador: Mapped[Operator] = relationship()


class ComprobanteArchivo(Base):
    """Archivo original (imagen o PDF) de un comprobante recibido por Telegram,
    guardado directo en Postgres (no en storage externo: son archivos chicos y
    Postgres ya es la base confiable que usa el resto de la app)."""

    __tablename__ = "comprobantes_archivo"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre_archivo: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    contenido: Mapped[bytes] = mapped_column(LargeBinary)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=ahora_argentina)


class ImportedStatement(Base):
    __tablename__ = "resumenes_importados"

    id: Mapped[int] = mapped_column(primary_key=True)
    cuenta_bancaria_id: Mapped[int] = mapped_column(ForeignKey("cuentas_bancarias.id"))
    fecha: Mapped[datetime] = mapped_column(DateTime)
    archivo_nombre: Mapped[str] = mapped_column(String(255))
    formato: Mapped[str] = mapped_column(String(10))
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios_panel.id"))
    fecha_importacion: Mapped[datetime] = mapped_column(DateTime, default=ahora_argentina)

    cuenta_bancaria: Mapped[BankAccount] = relationship()


class StatementLine(Base):
    __tablename__ = "lineas_resumen"

    id: Mapped[int] = mapped_column(primary_key=True)
    resumen_id: Mapped[int] = mapped_column(ForeignKey("resumenes_importados.id"))
    fecha: Mapped[datetime] = mapped_column(DateTime)
    monto: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    descripcion: Mapped[str | None] = mapped_column(String(255))
    referencia: Mapped[str | None] = mapped_column(String(120))
    movimiento_id: Mapped[int | None] = mapped_column(ForeignKey("movimientos.id"))
    estado: Mapped[StatementLineState] = mapped_column(default=StatementLineState.PENDIENTE)

    resumen: Mapped[ImportedStatement] = relationship()
    movimiento: Mapped[Movement | None] = relationship()


class PanelUser(Base):
    __tablename__ = "usuarios_panel"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(150))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    rol: Mapped[str] = mapped_column(String(50), default="Administrador")
    activo: Mapped[bool] = mapped_column(default=True)


class WhatsAppConversation(Base):
    __tablename__ = "conversaciones_whatsapp"

    numero: Mapped[str] = mapped_column(String(30), primary_key=True)
    estado: Mapped[ConversationState] = mapped_column(default=ConversationState.ESPERANDO_COMPROBANTE)
    movimiento_borrador_id: Mapped[int | None] = mapped_column(ForeignKey("movimientos.id"))
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=ahora_argentina, onupdate=ahora_argentina)
    # datos transitorios del comando "inicio movil X reparto nro Y" mientras se
    # espera la decision cerrar/continuar sobre un reparto ya abierto distinto.
    movil_pendiente_numero: Mapped[str | None] = mapped_column(String(30))
    numero_reparto_pendiente: Mapped[int | None] = mapped_column(Integer)

    movimiento_borrador: Mapped[Movement | None] = relationship()
