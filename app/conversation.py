import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import BankAccount, ConversationState, Movement, Operator, RecordState, WhatsAppConversation

NO_CUENTA_RECEPTORA_TEXTO = (
    "No pudimos identificar a que cuenta bancaria de la empresa corresponde este pago. "
    "Reenvia el comprobante, o si el problema persiste contacta al administrador para cargarlo manualmente."
)


@dataclass
class ExtractedTransfer:
    monto: Decimal | None
    fecha_transaccion: datetime
    numero_operacion: str | None
    banco_emisor: str | None = None
    cuenta_receptora: str | None = None
    titular: str | None = None


def _find_cuenta_bancaria(session: Session, cuenta_receptora: str | None) -> BankAccount | None:
    """Matchea el CBU/CVU/alias leido del comprobante contra las cuentas bancarias
    registradas en /config/cuentas. Compara el alias tal cual (sin distinguir
    mayusculas) y el numero de cuenta solo por sus digitos, para tolerar espacios,
    guiones u otro formato."""
    if not cuenta_receptora:
        return None
    normalizado = cuenta_receptora.strip().lower()
    digitos = re.sub(r"\D", "", cuenta_receptora)
    for cuenta in session.scalars(select(BankAccount)).all():
        if cuenta.alias.strip().lower() == normalizado:
            return cuenta
        if digitos and digitos == re.sub(r"\D", "", cuenta.numero_cuenta):
            return cuenta
    return None


class ConversationService:
    def __init__(self, session: Session):
        self.session = session

    def handle_text(self, number: str, text: str) -> str:
        operator = self.session.scalar(select(Operator).where(Operator.whatsapp_numero == number, Operator.activo.is_(True)))
        if operator is None:
            return "Este numero no esta habilitado para registrar comprobantes."

        conversation = self.session.get(WhatsAppConversation, number)
        if conversation is None:
            conversation = WhatsAppConversation(numero=number)
            self.session.add(conversation)
            self.session.flush()

        normalized = text.strip().lower()
        if conversation.estado == ConversationState.ESPERANDO_CONFIRMACION_DATOS:
            if normalized in {"si", "sí", "ok", "confirmo", "correcto"}:
                conversation.estado = ConversationState.ESPERANDO_CUENTA_FACTURA
                self.session.commit()
                return "Indica el numero de cuenta o factura asociado a este pago."
            if normalized in {"no", "cancelar", "cancelo"}:
                self._discard(conversation)
                self.session.commit()
                return "Registro descartado. Puedes reenviar el comprobante."
            return "Responde SI para confirmar los datos o NO para descartar el comprobante."

        if conversation.estado == ConversationState.ESPERANDO_CUENTA_FACTURA:
            movement = conversation.movimiento_borrador
            if movement is None:
                conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
                self.session.commit()
                return "La sesion vencio. Reenvia el comprobante, por favor."
            movement.factura_o_cuenta = text.strip()
            conversation.estado = ConversationState.ESPERANDO_CONFIRMACION_FINAL
            self.session.commit()
            return self._summary(movement)

        if conversation.estado == ConversationState.ESPERANDO_CONFIRMACION_FINAL:
            if normalized in {"si", "sí", "ok", "confirmo", "registrar"}:
                movement = conversation.movimiento_borrador
                if movement is None:
                    conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
                    self.session.commit()
                    return "La sesion vencio. Reenvia el comprobante, por favor."
                movement.estado_registro = RecordState.CONFIRMADO
                conversation.movimiento_borrador_id = None
                conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
                self.session.commit()
                return "Comprobante registrado correctamente."
            if normalized in {"no", "cancelar", "cancelo"}:
                self._discard(conversation)
                self.session.commit()
                return "Registro descartado. Puedes reenviar el comprobante."
            return "Responde OK para registrar el comprobante o NO para descartarlo."

        return "Envia una imagen o PDF del comprobante de transferencia."

    def start_transfer(self, number: str, transfer: ExtractedTransfer, archivo_prueba_id: int | None = None) -> str:
        operator = self.session.scalar(select(Operator).where(Operator.whatsapp_numero == number, Operator.activo.is_(True)))
        if operator is None:
            return "Este numero no esta habilitado para registrar comprobantes."
        if transfer.numero_operacion:
            duplicate = self.session.scalar(
                select(Movement).where(Movement.numero_operacion == transfer.numero_operacion)
            )
            if duplicate is not None:
                return "Ya existe un comprobante con ese numero de operacion."
        cuenta_bancaria = _find_cuenta_bancaria(self.session, transfer.cuenta_receptora)
        if cuenta_bancaria is None:
            return NO_CUENTA_RECEPTORA_TEXTO
        conversation = self.session.get(WhatsAppConversation, number) or WhatsAppConversation(numero=number)
        movement = Movement(
            operador_id=operator.id,
            monto=transfer.monto,
            fecha_transaccion=transfer.fecha_transaccion,
            numero_operacion=transfer.numero_operacion,
            banco_emisor=transfer.banco_emisor,
            cuenta_receptora_extraida=transfer.cuenta_receptora,
            titular=transfer.titular,
            archivo_prueba_id=archivo_prueba_id,
            cuenta_bancaria_id=cuenta_bancaria.id,
        )
        self.session.add(movement)
        self.session.flush()
        conversation.movimiento_borrador_id = movement.id
        conversation.estado = ConversationState.ESPERANDO_CONFIRMACION_DATOS
        self.session.add(conversation)
        self.session.commit()
        return self._summary(movement) + " Responde SI para confirmar o NO para descartar."

    def _discard(self, conversation: WhatsAppConversation) -> None:
        if conversation.movimiento_borrador is not None:
            self.session.delete(conversation.movimiento_borrador)
        conversation.movimiento_borrador_id = None
        conversation.estado = ConversationState.ESPERANDO_COMPROBANTE

    @staticmethod
    def _summary(movement: Movement) -> str:
        monto = f"${movement.monto}" if movement.monto is not None else "no detectado"
        return (
            f"Monto: {monto}; fecha: {movement.fecha_transaccion:%Y-%m-%d}; "
            f"banco emisor: {movement.banco_emisor or 'no detectado'}; "
            f"operacion: {movement.numero_operacion or 'no detectado'}; "
            f"factura/cuenta: {movement.factura_o_cuenta or 'pendiente'}."
        )
