import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    BankAccount,
    ConversationState,
    Movement,
    Movil,
    Operator,
    RecordState,
    Reparto,
    TipoIdentificador,
    WhatsAppConversation,
)
from .repartos import CerrarRepartoComando, IniciarRepartoComando, parse_comando_reparto

NO_CUENTA_RECEPTORA_TEXTO = (
    "No pudimos identificar a que cuenta bancaria de la empresa corresponde este pago. "
    "Reenvia el comprobante, o si el problema persiste contacta al administrador para cargarlo manualmente."
)

_TIPO_POR_TEXTO = {
    "factura": TipoIdentificador.FACTURA,
    "nro factura": TipoIdentificador.FACTURA,
    "numero de factura": TipoIdentificador.FACTURA,
    "n de factura": TipoIdentificador.FACTURA,
    "cuenta": TipoIdentificador.CUENTA,
    "nro cuenta": TipoIdentificador.CUENTA,
    "numero de cuenta": TipoIdentificador.CUENTA,
    "n de cuenta": TipoIdentificador.CUENTA,
}


def _parse_tipo_identificador(normalized: str) -> TipoIdentificador | None:
    return _TIPO_POR_TEXTO.get(normalized)


def _etiqueta_tipo(tipo: TipoIdentificador | None) -> str:
    return "factura" if tipo == TipoIdentificador.FACTURA else "cuenta"


def _formatear_fecha(fecha: datetime) -> str:
    """La hora solo se muestra si se detecto (fecha.hour/minute != 0); si el
    comprobante no traia hora, fecha_transaccion queda en medianoche y no tiene
    sentido mostrar una hora que nunca se leyo."""
    if fecha.hour or fecha.minute:
        return fecha.strftime("%Y-%m-%d %H:%M")
    return fecha.strftime("%Y-%m-%d")


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
    cuentas = session.scalars(select(BankAccount)).all()
    for cuenta in cuentas:
        if cuenta.alias.strip().lower() == normalizado:
            return cuenta
        if digitos and digitos == re.sub(r"\D", "", cuenta.numero_cuenta):
            return cuenta
    logging.warning(
        "Cuenta receptora sin match: extraida=%r cuentas_registradas=%r",
        cuenta_receptora,
        [(c.alias, c.numero_cuenta) for c in cuentas],
    )
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

        comando = parse_comando_reparto(text)
        if comando is not None:
            if conversation.estado not in (
                ConversationState.ESPERANDO_COMPROBANTE,
                ConversationState.ESPERANDO_DECISION_REPARTO_ABIERTO,
            ):
                return "Todavia tenes un comprobante pendiente de confirmar. Termina o cancela esa carga antes de iniciar/cerrar un reparto."
            return self._handle_comando_reparto(operator, comando)

        normalized = text.strip().lower()

        if conversation.estado == ConversationState.ESPERANDO_DECISION_REPARTO_ABIERTO:
            if normalized == "cerrar":
                return self._cerrar_y_arrancar_reparto_pendiente(operator, conversation)
            if normalized == "continuar":
                self._limpiar_decision_reparto(conversation)
                self.session.commit()
                return "Seguis con el reparto que ya estaba abierto."
            return "Responde 'cerrar' para cerrar el reparto abierto y arrancar el nuevo, o 'continuar' para seguir con el que ya esta abierto."

        if conversation.estado == ConversationState.ESPERANDO_MOVIL:
            movement = conversation.movimiento_borrador
            if movement is None:
                conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
                self.session.commit()
                return "La sesion vencio. Reenvia el comprobante, por favor."
            movil = self.session.scalar(select(Movil).where(Movil.numero == text.strip(), Movil.activo.is_(True)))
            if movil is None:
                return f"No encontramos un movil activo con el numero {text.strip()}. Respondé con el numero correcto."
            operator.movil_id = movil.id
            movement.movil_id = movil.id
            movement.estado_registro = RecordState.CONFIRMADO
            conversation.movimiento_borrador_id = None
            conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
            self.session.commit()
            return "Comprobante registrado correctamente."

        if conversation.estado == ConversationState.ESPERANDO_CONFIRMACION_DATOS:
            if normalized in {"si", "sí", "ok", "confirmo", "correcto"}:
                conversation.estado = ConversationState.ESPERANDO_TIPO_FACTURA_CUENTA
                self.session.commit()
                return "¿El dato que vas a cargar es un numero de factura o un numero de cuenta del cliente?"
            if normalized in {"no", "cancelar", "cancelo"}:
                self._discard(conversation)
                self.session.commit()
                return "Registro descartado. Puedes reenviar el comprobante."
            return "Responde SI para confirmar los datos o NO para descartar el comprobante."

        if conversation.estado == ConversationState.ESPERANDO_TIPO_FACTURA_CUENTA:
            movement = conversation.movimiento_borrador
            if movement is None:
                conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
                self.session.commit()
                return "La sesion vencio. Reenvia el comprobante, por favor."
            tipo = _parse_tipo_identificador(normalized)
            if tipo is None:
                return "Respondé 'factura' o 'cuenta' para indicar que numero vas a cargar."
            movement.factura_o_cuenta_tipo = tipo
            conversation.estado = ConversationState.ESPERANDO_NUMERO_FACTURA_CUENTA
            self.session.commit()
            return f"Indica el numero de {_etiqueta_tipo(tipo)} del cliente asociado a este pago."

        if conversation.estado == ConversationState.ESPERANDO_NUMERO_FACTURA_CUENTA:
            movement = conversation.movimiento_borrador
            if movement is None:
                conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
                self.session.commit()
                return "La sesion vencio. Reenvia el comprobante, por favor."
            movement.factura_o_cuenta_numero = text.strip()
            conversation.estado = ConversationState.ESPERANDO_CONFIRMACION_FINAL
            self.session.commit()
            return self._summary(movement) + "\n\nConfirma la operacion respondiendo OK para que se registre el movimiento, o NO para descartarlo."

        if conversation.estado == ConversationState.ESPERANDO_CONFIRMACION_FINAL:
            if normalized in {"si", "sí", "ok", "confirmo", "registrar"}:
                movement = conversation.movimiento_borrador
                if movement is None:
                    conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
                    self.session.commit()
                    return "La sesion vencio. Reenvia el comprobante, por favor."
                if operator.movil_id is None:
                    conversation.estado = ConversationState.ESPERANDO_MOVIL
                    self.session.commit()
                    return "¿En que movil estas? Respondé con el numero del movil."
                movement.movil_id = operator.movil_id
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

    def needs_confirmation_keyboard(self, number: str) -> bool:
        """True si el operador esta en un paso de SI/NO (confirmar datos o registrar
        definitivamente), para que el canal le muestre botones en vez de pedirle que
        escriba la respuesta."""
        conversation = self.session.get(WhatsAppConversation, number)
        return conversation is not None and conversation.estado in {
            ConversationState.ESPERANDO_CONFIRMACION_DATOS,
            ConversationState.ESPERANDO_CONFIRMACION_FINAL,
        }

    def needs_tipo_keyboard(self, number: str) -> bool:
        """True si el operador tiene que elegir entre factura o cuenta, para que el
        canal le muestre botones en vez de pedirle que escriba la respuesta."""
        conversation = self.session.get(WhatsAppConversation, number)
        return conversation is not None and conversation.estado == ConversationState.ESPERANDO_TIPO_FACTURA_CUENTA

    def pending_prompt(self, number: str) -> str | None:
        """Si el operador ya tiene un comprobante sin cerrar, devuelve el mensaje que
        corresponde re-mostrarle en vez de arrancar uno nuevo (para no dejar el
        anterior huerfano). None si no hay nada pendiente y puede recibir un
        comprobante nuevo."""
        conversation = self.session.get(WhatsAppConversation, number)
        if conversation is None or conversation.estado == ConversationState.ESPERANDO_COMPROBANTE:
            return None

        if conversation.estado == ConversationState.ESPERANDO_TIPO_FACTURA_CUENTA:
            return "Todavia estoy esperando que indiques si el dato que vas a cargar es un numero de factura o de cuenta."

        if conversation.estado == ConversationState.ESPERANDO_DECISION_REPARTO_ABIERTO:
            return "Todavia estoy esperando que respondas 'cerrar' o 'continuar' sobre el reparto que ya tenes abierto."

        movement = conversation.movimiento_borrador
        if movement is None:
            conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
            self.session.commit()
            return None

        if conversation.estado == ConversationState.ESPERANDO_CONFIRMACION_DATOS:
            return self._summary(movement) + "\n\nResponde SI para confirmar o NO para descartar."
        if conversation.estado == ConversationState.ESPERANDO_NUMERO_FACTURA_CUENTA:
            return f"Todavia estoy esperando el numero de {_etiqueta_tipo(movement.factura_o_cuenta_tipo)} del comprobante anterior."
        if conversation.estado == ConversationState.ESPERANDO_CONFIRMACION_FINAL:
            return (
                self._summary(movement)
                + "\n\nConfirma la operacion respondiendo OK para que se registre el movimiento, o NO para descartarlo."
            )
        if conversation.estado == ConversationState.ESPERANDO_MOVIL:
            return "Todavia estoy esperando que me digas en que movil estas para poder registrar el comprobante."
        return None

    def start_transfer(self, number: str, transfer: ExtractedTransfer, archivo_id: int | None = None) -> str:
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
            archivo_id=archivo_id,
            cuenta_bancaria_id=cuenta_bancaria.id,
        )
        self.session.add(movement)
        self.session.flush()
        conversation.movimiento_borrador_id = movement.id
        conversation.estado = ConversationState.ESPERANDO_CONFIRMACION_DATOS
        self.session.add(conversation)
        self.session.commit()
        return self._summary(movement) + "\n\nResponde SI para confirmar o NO para descartar."

    def _discard(self, conversation: WhatsAppConversation) -> None:
        if conversation.movimiento_borrador is not None:
            self.session.delete(conversation.movimiento_borrador)
        conversation.movimiento_borrador_id = None
        conversation.estado = ConversationState.ESPERANDO_COMPROBANTE

    def _reparto_abierto(self, movil_id: int) -> Reparto | None:
        return self.session.scalar(select(Reparto).where(Reparto.movil_id == movil_id, Reparto.hora_fin.is_(None)))

    @staticmethod
    def _limpiar_decision_reparto(conversation: WhatsAppConversation) -> None:
        conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
        conversation.movil_pendiente_numero = None
        conversation.numero_reparto_pendiente = None

    def _handle_comando_reparto(
        self, operator: Operator, comando: IniciarRepartoComando | CerrarRepartoComando
    ) -> str:
        if isinstance(comando, IniciarRepartoComando):
            return self._iniciar_reparto(operator, comando)
        return self._cerrar_reparto(operator, comando)

    def _iniciar_reparto(self, operator: Operator, comando: IniciarRepartoComando) -> str:
        movil = self.session.scalar(
            select(Movil).where(Movil.numero == comando.movil_numero, Movil.activo.is_(True))
        )
        if movil is None:
            return f"No encontramos un movil activo con el numero {comando.movil_numero}."

        # El operador puede tener solo un reparto abierto a la vez -- se chequea
        # contra su movil ACTUAL (antes de sobrescribirlo), no contra el movil X
        # recien pedido, para no permitir dos repartos abiertos en simultaneo si
        # el operador cambia de movil sin cerrar el anterior.
        reparto_abierto = self._reparto_abierto(operator.movil_id) if operator.movil_id else None
        if reparto_abierto is not None:
            conversation = self.session.get(WhatsAppConversation, operator.whatsapp_numero)
            if conversation is None:
                conversation = WhatsAppConversation(numero=operator.whatsapp_numero)
                self.session.add(conversation)
            conversation.estado = ConversationState.ESPERANDO_DECISION_REPARTO_ABIERTO
            conversation.movil_pendiente_numero = comando.movil_numero
            conversation.numero_reparto_pendiente = comando.numero_reparto
            self.session.commit()
            numero_abierto = reparto_abierto.numero_reparto if reparto_abierto.numero_reparto is not None else "sin numero"
            return (
                f"Ya tenes un reparto abierto (Nº {numero_abierto}). Responde 'cerrar' para cerrarlo y arrancar "
                "el nuevo, o 'continuar' para seguir con el que ya esta abierto."
            )

        operator.movil_id = movil.id
        self.session.add(
            Reparto(movil_id=movil.id, fecha=date.today(), hora_inicio=datetime.utcnow(), numero_reparto=comando.numero_reparto)
        )
        self.session.commit()
        return f"Reparto Nº {comando.numero_reparto} iniciado en el movil {movil.numero}."

    def _cerrar_y_arrancar_reparto_pendiente(self, operator: Operator, conversation: WhatsAppConversation) -> str:
        reparto_abierto = self._reparto_abierto(operator.movil_id) if operator.movil_id else None
        if reparto_abierto is not None:
            reparto_abierto.hora_fin = datetime.utcnow()

        movil = self.session.scalar(
            select(Movil).where(Movil.numero == conversation.movil_pendiente_numero, Movil.activo.is_(True))
        )
        numero_reparto_nuevo = conversation.numero_reparto_pendiente
        if movil is None:
            self._limpiar_decision_reparto(conversation)
            self.session.commit()
            return "El movil que habias indicado ya no esta disponible. Volve a mandar el comando de inicio."

        operator.movil_id = movil.id
        self.session.add(
            Reparto(movil_id=movil.id, fecha=date.today(), hora_inicio=datetime.utcnow(), numero_reparto=numero_reparto_nuevo)
        )
        self._limpiar_decision_reparto(conversation)
        self.session.commit()
        return f"Reparto anterior cerrado. Reparto Nº {numero_reparto_nuevo} iniciado en el movil {movil.numero}."

    def _cerrar_reparto(self, operator: Operator, comando: CerrarRepartoComando) -> str:
        if operator.movil_id is None:
            return "No tenes ningun movil asignado, asi que no hay ningun reparto para cerrar."
        reparto_abierto = self._reparto_abierto(operator.movil_id)
        if reparto_abierto is None:
            return "No hay ningun reparto abierto para cerrar."
        if reparto_abierto.numero_reparto is not None and reparto_abierto.numero_reparto != comando.numero_reparto:
            return (
                f"El reparto abierto es el Nº {reparto_abierto.numero_reparto}, no el {comando.numero_reparto}. "
                "Reenvia el comando con el numero correcto."
            )
        reparto_abierto.hora_fin = datetime.utcnow()
        self.session.commit()
        return f"Reparto Nº {comando.numero_reparto} cerrado."

    @staticmethod
    def _summary(movement: Movement) -> str:
        monto = f"${movement.monto}" if movement.monto is not None else "no detectado"
        cuenta = movement.cuenta_bancaria.alias if movement.cuenta_bancaria is not None else "no detectada"
        if movement.factura_o_cuenta_tipo is not None and movement.factura_o_cuenta_numero:
            factura_cuenta = f"{_etiqueta_tipo(movement.factura_o_cuenta_tipo).capitalize()}: {movement.factura_o_cuenta_numero}"
        else:
            factura_cuenta = "pendiente"
        lineas = [
            f"Monto: {monto}",
            f"Fecha: {_formatear_fecha(movement.fecha_transaccion)}",
            f"Cuenta receptora: {cuenta}",
            f"Operacion: {movement.numero_operacion or 'no detectado'}",
        ]
        if movement.titular:
            lineas.append(f"Emisor: {movement.titular}")
        lineas.append(f"Factura/cuenta: {factura_cuenta}")
        return "\n".join(lineas)
