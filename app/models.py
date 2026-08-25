from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class ConversationState(StrEnum):
    ESPERANDO_COMPROBANTE = "esperando_comprobante"
    ESPERANDO_CONFIRMACION_DATOS = "esperando_confirmacion_datos"
    ESPERANDO_CUENTA_FACTURA = "esperando_cuenta_factura"
    ESPERANDO_CONFIRMACION_FINAL = "esperando_confirmacion_final"


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
    fecha_alta: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BankAccount(Base):
    __tablename__ = "cuentas_bancarias"

    id: Mapped[int] = mapped_column(primary_key=True)
    banco: Mapped[str] = mapped_column(String(120))
    numero_cuenta: Mapped[str] = mapped_column(String(60))
    moneda: Mapped[str] = mapped_column(String(10), default="ARS")
    alias: Mapped[str] = mapped_column(String(120))


class Movement(Base):
    __tablename__ = "movimientos"
    __table_args__ = (UniqueConstraint("numero_operacion", name="uq_movimiento_operacion"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    operador_id: Mapped[int] = mapped_column(ForeignKey("operadores.id"))
    fecha_transaccion: Mapped[datetime | None] = mapped_column(DateTime)
    fecha_subida: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    monto: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    banco_emisor: Mapped[str | None] = mapped_column(String(120))
    cuenta_receptora_extraida: Mapped[str | None] = mapped_column(String(150))
    numero_operacion: Mapped[str] = mapped_column(String(120), index=True)
    titular: Mapped[str | None] = mapped_column(String(150))
    factura_o_cuenta: Mapped[str | None] = mapped_column(String(150))
    cuenta_bancaria_id: Mapped[int | None] = mapped_column(ForeignKey("cuentas_bancarias.id"))
    archivo_url: Mapped[str | None] = mapped_column(Text)
    origen: Mapped[str] = mapped_column(String(20), default="whatsapp")
    estado_registro: Mapped[RecordState] = mapped_column(default=RecordState.PENDIENTE_CONFIRMACION)
    estado_conciliacion: Mapped[ReconciliationState] = mapped_column(default=ReconciliationState.PENDIENTE)

    operador: Mapped[Operator] = relationship()
    cuenta_bancaria: Mapped[BankAccount | None] = relationship()


class ImportedStatement(Base):
    __tablename__ = "resumenes_importados"

    id: Mapped[int] = mapped_column(primary_key=True)
    cuenta_bancaria_id: Mapped[int] = mapped_column(ForeignKey("cuentas_bancarias.id"))
    fecha: Mapped[datetime] = mapped_column(DateTime)
    archivo_nombre: Mapped[str] = mapped_column(String(255))
    formato: Mapped[str] = mapped_column(String(10))
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios_panel.id"))
    fecha_importacion: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    movimiento_borrador: Mapped[Movement | None] = relationship()
