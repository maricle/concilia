import logging
import re
from dataclasses import dataclass
from datetime import datetime
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
    RepartoOperador,
    TipoIdentificador,
    WhatsAppConversation,
)
from .numeros import formato_monto_ar as _formato_monto_ar
from .repartos import CerrarRepartoComando, IniciarRepartoComando, parse_comando_reparto
from .zona_horaria import ahora_argentina, hoy_argentina

NO_CUENTA_RECEPTORA_TEXTO = (
    "No pudimos identificar a que cuenta bancaria de la empresa corresponde este pago. "
    "Reenvia el comprobante, o si el problema persiste contacta al administrador para cargarlo manualmente."
)

_CANCELAR_TEXTO = {"no", "cancelar", "cancelo"}

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
        # (numero_whatsapp, mensaje) para operadores DISTINTOS al que disparo la
        # accion -- ej. el resto de los asociados a un reparto que se cierra. El
        # canal (telegram.py) los consume con pop_notificaciones() despues de
        # cada handle_text() y les manda el mensaje aparte.
        self._notificaciones: list[tuple[str, str]] = []

    def pop_notificaciones(self) -> list[tuple[str, str]]:
        notificaciones = self._notificaciones
        self._notificaciones = []
        return notificaciones

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

        # 'cerrar'/'continuar' tienen un significado especifico en este estado (que
        # se resuelve aca abajo) -- se chequean antes de parsear como comando
        # general para que "cerrar" no se interprete como un cierre de reparto
        # cualquiera y se pierda el reparto nuevo que estaba pendiente de arrancar.
        if conversation.estado == ConversationState.ESPERANDO_DECISION_REPARTO_ABIERTO and normalized in (
            "cerrar",
            "continuar",
        ):
            if normalized == "cerrar":
                return self._cerrar_y_arrancar_reparto_pendiente(operator, conversation)
            self._limpiar_decision_reparto(conversation)
            self.session.commit()
            return "Seguis con la salida que ya estaba abierta."

        comando = parse_comando_reparto(text)
        if comando is not None:
            if conversation.estado not in (
                ConversationState.ESPERANDO_COMPROBANTE,
                ConversationState.ESPERANDO_DECISION_REPARTO_ABIERTO,
                ConversationState.ESPERANDO_DATOS_INICIO_REPARTO,
                ConversationState.ESPERANDO_CONFIRMACION_INICIO_REPARTO,
            ):
                return "Todavia tenes un comprobante pendiente de confirmar. Termina o cancela esa carga antes de iniciar/cerrar una salida."
            return self._handle_comando_reparto(operator, conversation, comando)

        if conversation.estado == ConversationState.ESPERANDO_DATOS_INICIO_REPARTO:
            if conversation.movil_pendiente_numero is None:
                conversation.movil_pendiente_numero = text.strip()
            else:
                numero_texto = text.strip()
                if not numero_texto.isdigit():
                    return "Ese no es un numero de salida valido. Respondé solo con el numero."
                conversation.numero_reparto_pendiente = int(numero_texto)

            if conversation.movil_pendiente_numero is None or conversation.numero_reparto_pendiente is None:
                self.session.commit()
                return self._prompt_dato_faltante_inicio_reparto(conversation)

            comando_completo = IniciarRepartoComando(
                movil_numero=conversation.movil_pendiente_numero,
                numero_reparto=conversation.numero_reparto_pendiente,
            )
            self._limpiar_decision_reparto(conversation)
            return self._iniciar_reparto(operator, comando_completo)

        if conversation.estado == ConversationState.ESPERANDO_CONFIRMACION_INICIO_REPARTO:
            if normalized in {"si", "sí", "ok", "confirmo"}:
                return self._confirmar_inicio_reparto(operator, conversation)
            if normalized in _CANCELAR_TEXTO:
                self._limpiar_decision_reparto(conversation)
                self.session.commit()
                return "Inicio de salida cancelado."
            return "Respondé SI para confirmar el inicio de la salida o NO para cancelarlo."

        if conversation.estado == ConversationState.ESPERANDO_DECISION_REPARTO_ABIERTO:
            # 'cerrar'/'continuar' ya se resolvieron arriba, antes del parseo de
            # comando -- si llegamos aca es que mando otra cosa.
            return "Responde 'cerrar' para cerrar la salida abierta y arrancar la nueva, o 'continuar' para seguir con la que ya esta abierta."

        if conversation.estado == ConversationState.ESPERANDO_CUENTA_BANCARIA:
            movement = conversation.movimiento_borrador
            if movement is None:
                conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
                self.session.commit()
                return "La sesion vencio. Reenvia el comprobante, por favor."
            if normalized in _CANCELAR_TEXTO:
                self._discard(conversation)
                self.session.commit()
                return "Registro descartado. Puedes reenviar el comprobante."
            cuenta = self._buscar_cuenta_por_eleccion(text)
            if cuenta is None:
                return "Esa no es una de las opciones. " + self._prompt_elegir_cuenta_bancaria()
            movement.cuenta_bancaria_id = cuenta.id
            return self._avanzar_a_tipo_factura_cuenta(conversation, movement)

        if conversation.estado == ConversationState.ESPERANDO_MOVIL:
            movement = conversation.movimiento_borrador
            if movement is None:
                conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
                self.session.commit()
                return "La sesion vencio. Reenvia el comprobante, por favor."
            movil = self._buscar_movil_activo(text)
            if movil is None:
                return f"No encontramos un movil activo con el numero {text.strip()}. Respondé con el numero correcto."

            reparto_abierto = self._reparto_abierto(movil.id)
            if reparto_abierto is not None:
                self._asociar_operador(reparto_abierto, operator)
                movement.movil_id = movil.id
                movement.reparto_id = reparto_abierto.id
                movement.estado_registro = RecordState.CONFIRMADO
                conversation.movimiento_borrador_id = None
                conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
                self.session.commit()
                numero_abierto = reparto_abierto.numero_reparto if reparto_abierto.numero_reparto is not None else "sin numero"
                return f"Comprobante registrado correctamente. Salida Nº {numero_abierto} en el movil {movil.numero}."

            conversation.estado = ConversationState.ESPERANDO_CONFIRMACION_CREAR_REPARTO
            conversation.movil_pendiente_numero = movil.numero
            self.session.commit()
            return (
                f"No hay ninguna salida abierta en el movil {movil.numero}. ¿Queres iniciar una? "
                "Respondé SI o NO."
            )

        if conversation.estado == ConversationState.ESPERANDO_CONFIRMACION_CREAR_REPARTO:
            if normalized in {"si", "sí", "ok", "confirmo"}:
                conversation.estado = ConversationState.ESPERANDO_NUMERO_REPARTO_NUEVO
                self.session.commit()
                return "¿Que numero de salida es? Respondé solo con el numero."
            if normalized in _CANCELAR_TEXTO:
                conversation.movil_pendiente_numero = None
                conversation.estado = ConversationState.ESPERANDO_MOVIL
                self.session.commit()
                return "Entendido, no inicio una salida nueva ahi. ¿En que movil estas?"
            return "Respondé SI para iniciar una salida nueva en ese movil o NO para indicar otro movil."

        if conversation.estado == ConversationState.ESPERANDO_NUMERO_REPARTO_NUEVO:
            if normalized in _CANCELAR_TEXTO:
                self._discard(conversation)
                conversation.movil_pendiente_numero = None
                self.session.commit()
                return "Registro descartado. Puedes reenviar el comprobante."
            numero_texto = text.strip()
            if not numero_texto.isdigit():
                return "Ese no es un numero de salida valido. Respondé solo con el numero."
            return self._crear_reparto_y_confirmar_comprobante(operator, conversation, int(numero_texto))

        if conversation.estado == ConversationState.ESPERANDO_CONFIRMACION_DATOS:
            if normalized in {"si", "sí", "ok", "confirmo", "correcto"}:
                conversation.estado = ConversationState.ESPERANDO_TIPO_FACTURA_CUENTA
                self.session.commit()
                return "¿El dato que vas a cargar es un numero de factura o un numero de cuenta del cliente?"
            if normalized in _CANCELAR_TEXTO:
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
            if normalized in _CANCELAR_TEXTO:
                self._discard(conversation)
                self.session.commit()
                return "Registro descartado. Puedes reenviar el comprobante."
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
            return self._summary(movement) + "\n\nConfirma la operacion respondiendo SI para que se registre el movimiento, o NO para descartarlo."

        if conversation.estado == ConversationState.ESPERANDO_CONFIRMACION_FINAL:
            if normalized in {"si", "sí", "ok", "confirmo", "registrar"}:
                movement = conversation.movimiento_borrador
                if movement is None:
                    conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
                    self.session.commit()
                    return "La sesion vencio. Reenvia el comprobante, por favor."
                reparto_propio = self._reparto_abierto_de_operador(operator.id)
                if reparto_propio is None:
                    conversation.estado = ConversationState.ESPERANDO_MOVIL
                    self.session.commit()
                    return "¿En que movil estas? Respondé con el numero del movil."
                movement.movil_id = reparto_propio.movil_id
                movement.reparto_id = reparto_propio.id
                movement.estado_registro = RecordState.CONFIRMADO
                conversation.movimiento_borrador_id = None
                conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
                self.session.commit()
                return "Comprobante registrado correctamente."
            if normalized in _CANCELAR_TEXTO:
                self._discard(conversation)
                self.session.commit()
                return "Registro descartado. Puedes reenviar el comprobante."
            return "Responde SI para registrar el comprobante o NO para descartarlo."

        return "Envia una imagen o PDF del comprobante de transferencia."

    def needs_confirmation_keyboard(self, number: str) -> bool:
        """True si el operador esta en un paso de SI/NO (confirmar datos o registrar
        definitivamente), para que el canal le muestre botones en vez de pedirle que
        escriba la respuesta."""
        conversation = self.session.get(WhatsAppConversation, number)
        return conversation is not None and conversation.estado in {
            ConversationState.ESPERANDO_CONFIRMACION_DATOS,
            ConversationState.ESPERANDO_CONFIRMACION_FINAL,
            ConversationState.ESPERANDO_CONFIRMACION_INICIO_REPARTO,
            ConversationState.ESPERANDO_CONFIRMACION_CREAR_REPARTO,
        }

    def needs_tipo_keyboard(self, number: str) -> bool:
        """True si el operador tiene que elegir entre factura o cuenta, para que el
        canal le muestre botones en vez de pedirle que escriba la respuesta."""
        conversation = self.session.get(WhatsAppConversation, number)
        return conversation is not None and conversation.estado == ConversationState.ESPERANDO_TIPO_FACTURA_CUENTA

    def needs_cuenta_keyboard(self, number: str) -> bool:
        """True si el operador tiene que elegir a mano la cuenta bancaria del pago
        (no se pudo identificar sola), para que el canal le muestre un boton por
        cada cuenta cargada en vez de pedirle que la escriba."""
        conversation = self.session.get(WhatsAppConversation, number)
        return conversation is not None and conversation.estado == ConversationState.ESPERANDO_CUENTA_BANCARIA

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
            return "Todavia estoy esperando que respondas 'cerrar' o 'continuar' sobre la salida que ya tenes abierta."

        if conversation.estado == ConversationState.ESPERANDO_DATOS_INICIO_REPARTO:
            return self._prompt_dato_faltante_inicio_reparto(conversation)

        if conversation.estado == ConversationState.ESPERANDO_CONFIRMACION_INICIO_REPARTO:
            return (
                f"Vas a iniciar la Salida Nº {conversation.numero_reparto_pendiente} en el movil "
                f"{conversation.movil_pendiente_numero}. Respondé SI para confirmar o NO para cancelar."
            )

        if conversation.estado == ConversationState.ESPERANDO_CONFIRMACION_CREAR_REPARTO:
            return (
                f"No hay ninguna salida abierta en el movil {conversation.movil_pendiente_numero}. "
                "¿Queres iniciar una? Respondé SI o NO."
            )

        if conversation.estado == ConversationState.ESPERANDO_NUMERO_REPARTO_NUEVO:
            return "¿Que numero de salida es? Respondé solo con el numero."

        if conversation.estado == ConversationState.ESPERANDO_CUENTA_BANCARIA:
            return self._prompt_elegir_cuenta_bancaria()

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
                + "\n\nConfirma la operacion respondiendo SI para que se registre el movimiento, o NO para descartarlo."
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
        cuentas = self.session.scalars(select(BankAccount)).all()
        cuenta_bancaria = _find_cuenta_bancaria(self.session, transfer.cuenta_receptora)
        if cuenta_bancaria is None and not cuentas:
            # Sin ninguna cuenta cargada en /config/cuentas no hay nada para
            # ofrecerle a elegir -- ahi si hace falta un administrador.
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
            cuenta_bancaria_id=cuenta_bancaria.id if cuenta_bancaria is not None else None,
        )
        self.session.add(movement)
        self.session.flush()
        conversation.movimiento_borrador_id = movement.id
        self.session.add(conversation)

        if cuenta_bancaria is None:
            # No se pudo identificar la cuenta sola -- se le pide al operador que
            # elija entre las cargadas, en vez de rechazar el comprobante entero.
            conversation.estado = ConversationState.ESPERANDO_CUENTA_BANCARIA
            self.session.commit()
            return self._prompt_elegir_cuenta_bancaria()

        # Se salta directo al paso de factura/cuenta -- mostrar el resumen y pedir
        # una confirmacion aparte antes de esto era una revision redundante, ya que
        # el resumen se vuelve a mostrar completo (con factura/cuenta ya cargada)
        # en la confirmacion final antes de registrar.
        return self._avanzar_a_tipo_factura_cuenta(conversation, movement)

    def _prompt_elegir_cuenta_bancaria(self) -> str:
        # Se lista el banco primero -- es lo que el operador reconoce del
        # comprobante (Galicia, Mercado Pago, etc.), el alias interno a veces no
        # tiene nada que ver con el banco (ej. "el.paquete.llega").
        cuentas = self.session.scalars(select(BankAccount)).all()
        listado = "\n".join(f"- {c.banco} ({c.alias})" for c in cuentas)
        return (
            "No pudimos identificar a que cuenta corresponde este pago. ¿A cual de estas pertenece?\n\n"
            f"{listado}\n\n"
            "Respondé con el banco o el nombre de la cuenta, o 'cancelar' si no lo sabés."
        )

    def _buscar_cuenta_por_eleccion(self, texto: str) -> BankAccount | None:
        """Matchea la eleccion del operador contra el alias (valor estable que
        manda el boton de Telegram) o, si tipeo texto libre, contra el nombre del
        banco (lo que reconoce del comprobante, no el alias interno)."""
        elegido = texto.strip().lower()
        cuentas = self.session.scalars(select(BankAccount)).all()
        for cuenta in cuentas:
            if cuenta.alias.strip().lower() == elegido:
                return cuenta
        coincidencias = [c for c in cuentas if c.banco.strip().lower() == elegido]
        return coincidencias[0] if len(coincidencias) == 1 else None

    def _avanzar_a_tipo_factura_cuenta(self, conversation: WhatsAppConversation, movement: Movement) -> str:
        conversation.estado = ConversationState.ESPERANDO_TIPO_FACTURA_CUENTA
        self.session.commit()
        return (
            self._summary(movement)
            + "\n\n¿El dato que vas a cargar es un numero de factura o un numero de cuenta del cliente?"
        )

    def _discard(self, conversation: WhatsAppConversation) -> None:
        if conversation.movimiento_borrador is not None:
            self.session.delete(conversation.movimiento_borrador)
        conversation.movimiento_borrador_id = None
        conversation.estado = ConversationState.ESPERANDO_COMPROBANTE

    def _reparto_abierto(self, movil_id: int) -> Reparto | None:
        return self.session.scalar(select(Reparto).where(Reparto.movil_id == movil_id, Reparto.hora_fin.is_(None)))

    def _reparto_abierto_de_operador(self, operador_id: int) -> Reparto | None:
        """El reparto abierto (de cualquier movil) al que este operador esta
        asociado ahora mismo. Un operador solo puede estar asociado a un reparto
        abierto a la vez (se valida al asociar, no lo impide el modelo)."""
        return self.session.scalar(
            select(Reparto)
            .join(RepartoOperador, RepartoOperador.reparto_id == Reparto.id)
            .where(RepartoOperador.operador_id == operador_id, Reparto.hora_fin.is_(None))
        )

    def _asociar_operador(self, reparto: Reparto, operator: Operator) -> None:
        """Suma al operador como participante del reparto (chofer, ayudante, o el
        que entra en un cambio de turno) si todavia no estaba. movil_id del
        operador se actualiza como referencia -- no es la fuente de verdad para
        nada, solo un dato de conveniencia de "en que movil arranco el dia"."""
        ya_asociado = self.session.scalar(
            select(RepartoOperador).where(
                RepartoOperador.reparto_id == reparto.id, RepartoOperador.operador_id == operator.id
            )
        )
        if ya_asociado is None:
            self.session.add(RepartoOperador(reparto_id=reparto.id, operador_id=operator.id))
        operator.movil_id = reparto.movil_id

    def _buscar_movil_activo(self, texto: str | None) -> Movil | None:
        """Busca un movil activo por numero, tolerando que el operador escriba con
        o sin el prefijo 'M-' -- 'M-01', 'm01', '01' y '1' matchean el mismo movil
        si sus digitos coinciden. Primero intenta match exacto (por si el numero
        del movil no es puramente numerico, ej. "Camion Rojo")."""
        if texto is None:
            return None
        texto = texto.strip()
        movil = self.session.scalar(select(Movil).where(Movil.numero == texto, Movil.activo.is_(True)))
        if movil is not None:
            return movil
        digitos = re.sub(r"\D", "", texto).lstrip("0")
        if not digitos:
            return None
        for candidato in self.session.scalars(select(Movil).where(Movil.activo.is_(True))):
            if re.sub(r"\D", "", candidato.numero).lstrip("0") == digitos:
                return candidato
        return None

    @staticmethod
    def _limpiar_decision_reparto(conversation: WhatsAppConversation) -> None:
        conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
        conversation.movil_pendiente_numero = None
        conversation.numero_reparto_pendiente = None

    def _handle_comando_reparto(
        self,
        operator: Operator,
        conversation: WhatsAppConversation,
        comando: IniciarRepartoComando | CerrarRepartoComando,
    ) -> str:
        if isinstance(comando, IniciarRepartoComando):
            if comando.movil_numero is None or comando.numero_reparto is None:
                reparto_propio = self._reparto_abierto_de_operador(operator.id)
                if reparto_propio is not None:
                    # Con el comando incompleto no sabemos si el operador quiere seguir
                    # con el reparto que ya tiene abierto o arrancar uno distinto -- se lo
                    # avisamos en vez de pedirle datos para un reparto que capaz ni queria.
                    numero_abierto = (
                        reparto_propio.numero_reparto if reparto_propio.numero_reparto is not None else "sin numero"
                    )
                    return (
                        f"Ya tenes la Salida Nº {numero_abierto} iniciada en el movil {reparto_propio.movil.numero}. "
                        f"Si queres cerrarla, mandá 'cerrar salida nro {numero_abierto}'. Si queres arrancar una "
                        "distinta, mandá el comando completo con el movil y el numero de salida nuevo."
                    )
                return self._recopilar_datos_inicio_reparto(conversation, comando)
            return self._iniciar_reparto(operator, comando)
        return self._cerrar_reparto(operator, comando)

    def _recopilar_datos_inicio_reparto(
        self, conversation: WhatsAppConversation, comando: IniciarRepartoComando
    ) -> str:
        conversation.estado = ConversationState.ESPERANDO_DATOS_INICIO_REPARTO
        conversation.movil_pendiente_numero = comando.movil_numero
        conversation.numero_reparto_pendiente = comando.numero_reparto
        self.session.commit()
        return self._prompt_dato_faltante_inicio_reparto(conversation)

    @staticmethod
    def _prompt_dato_faltante_inicio_reparto(conversation: WhatsAppConversation) -> str:
        if conversation.movil_pendiente_numero is None:
            return "¿En que movil vas a iniciar la salida? Respondé con el numero del movil."
        return "¿Que numero de salida es? Respondé solo con el numero."

    def _iniciar_reparto(self, operator: Operator, comando: IniciarRepartoComando) -> str:
        movil = self._buscar_movil_activo(comando.movil_numero)
        if movil is None:
            return f"No encontramos un movil activo con el numero {comando.movil_numero}."

        # Un operador solo puede estar asociado a un reparto abierto a la vez -- se
        # chequea contra SU reparto propio actual (no contra el movil X recien
        # pedido), para no permitir que quede sumado a dos turnos en simultaneo.
        reparto_propio = self._reparto_abierto_de_operador(operator.id)
        if reparto_propio is not None:
            if reparto_propio.movil_id == movil.id:
                numero_abierto = (
                    reparto_propio.numero_reparto if reparto_propio.numero_reparto is not None else "sin numero"
                )
                return f"Ya estas asociado a la Salida Nº {numero_abierto} en el movil {movil.numero}."
            conversation = self.session.get(WhatsAppConversation, operator.whatsapp_numero)
            if conversation is None:
                conversation = WhatsAppConversation(numero=operator.whatsapp_numero)
                self.session.add(conversation)
            conversation.estado = ConversationState.ESPERANDO_DECISION_REPARTO_ABIERTO
            conversation.movil_pendiente_numero = movil.numero
            conversation.numero_reparto_pendiente = comando.numero_reparto
            self.session.commit()
            numero_abierto = reparto_propio.numero_reparto if reparto_propio.numero_reparto is not None else "sin numero"
            return (
                f"Ya tenes una salida abierta (Nº {numero_abierto}). Responde 'cerrar' para cerrarla y arrancar "
                "la nueva, o 'continuar' para seguir con la que ya esta abierta."
            )

        # El movil pedido puede ya tener un reparto abierto con otro operador (o con
        # este mismo bajo otra circunstancia) -- en ese caso no se crea uno nuevo,
        # el operador se suma al que ya esta en curso, sin pedir confirmacion.
        reparto_movil = self._reparto_abierto(movil.id)
        if reparto_movil is not None:
            self._asociar_operador(reparto_movil, operator)
            self.session.commit()
            numero_abierto = reparto_movil.numero_reparto if reparto_movil.numero_reparto is not None else "sin numero"
            return f"Te asociaste a la Salida Nº {numero_abierto} en el movil {movil.numero}."

        conversation = self.session.get(WhatsAppConversation, operator.whatsapp_numero)
        if conversation is None:
            conversation = WhatsAppConversation(numero=operator.whatsapp_numero)
            self.session.add(conversation)
        conversation.estado = ConversationState.ESPERANDO_CONFIRMACION_INICIO_REPARTO
        conversation.movil_pendiente_numero = movil.numero
        conversation.numero_reparto_pendiente = comando.numero_reparto
        self.session.commit()
        return (
            f"Vas a iniciar la Salida Nº {comando.numero_reparto} en el movil {movil.numero}. "
            "Respondé SI para confirmar o NO para cancelar."
        )

    def _confirmar_inicio_reparto(self, operator: Operator, conversation: WhatsAppConversation) -> str:
        movil = self._buscar_movil_activo(conversation.movil_pendiente_numero)
        numero_reparto = conversation.numero_reparto_pendiente
        self._limpiar_decision_reparto(conversation)
        if movil is None:
            self.session.commit()
            return "El movil ya no esta disponible. Volve a mandar el comando de inicio."

        reparto = Reparto(movil_id=movil.id, fecha=hoy_argentina(), hora_inicio=ahora_argentina(), numero_reparto=numero_reparto)
        self.session.add(reparto)
        self.session.flush()
        self._asociar_operador(reparto, operator)
        self.session.commit()
        return f"Salida Nº {numero_reparto} iniciada en el movil {movil.numero}."

    def _cerrar_y_arrancar_reparto_pendiente(self, operator: Operator, conversation: WhatsAppConversation) -> str:
        reparto_propio = self._reparto_abierto_de_operador(operator.id)
        if reparto_propio is not None:
            reparto_propio.hora_fin = ahora_argentina()
            self._notificar_cierre_reparto(reparto_propio, operator, self._resumen_bancario_cierre(reparto_propio))

        movil = self._buscar_movil_activo(conversation.movil_pendiente_numero)
        numero_reparto_nuevo = conversation.numero_reparto_pendiente
        if movil is None:
            self._limpiar_decision_reparto(conversation)
            self.session.commit()
            return "El movil que habias indicado ya no esta disponible. Volve a mandar el comando de inicio."

        reparto_movil = self._reparto_abierto(movil.id)
        if reparto_movil is not None:
            self._asociar_operador(reparto_movil, operator)
            self._limpiar_decision_reparto(conversation)
            self.session.commit()
            numero_abierto = reparto_movil.numero_reparto if reparto_movil.numero_reparto is not None else "sin numero"
            return f"Salida anterior cerrada. Te asociaste a la Salida Nº {numero_abierto} en el movil {movil.numero}."

        reparto_nuevo = Reparto(
            movil_id=movil.id, fecha=hoy_argentina(), hora_inicio=ahora_argentina(), numero_reparto=numero_reparto_nuevo
        )
        self.session.add(reparto_nuevo)
        self.session.flush()
        self._asociar_operador(reparto_nuevo, operator)
        self._limpiar_decision_reparto(conversation)
        self.session.commit()
        return f"Salida anterior cerrada. Salida Nº {numero_reparto_nuevo} iniciada en el movil {movil.numero}."

    def _resumen_bancario_cierre(self, reparto: Reparto) -> str:
        """Texto con el total por banco de los comprobantes registrados durante la
        salida, para avisar al cerrarla -- reemplaza el PDF que se mandaba antes."""
        movimientos = self.session.scalars(select(Movement).where(Movement.reparto_id == reparto.id)).all()
        subtotales: dict[str, Decimal] = {}
        for movimiento in movimientos:
            banco = movimiento.cuenta_bancaria.banco if movimiento.cuenta_bancaria else "Sin banco identificado"
            subtotales[banco] = subtotales.get(banco, Decimal("0")) + (movimiento.monto or Decimal("0"))

        numero = reparto.numero_reparto if reparto.numero_reparto is not None else "sin numero"
        lineas = [f"Cierre Salida Nº {numero}"]
        for banco in sorted(banco for banco in subtotales if banco != "Sin banco identificado"):
            lineas.append(f"{banco}: {_formato_monto_ar(subtotales[banco])}")
        if "Sin banco identificado" in subtotales:
            lineas.append(f"Sin banco identificado: {_formato_monto_ar(subtotales['Sin banco identificado'])}")
        if not movimientos:
            lineas.append("Sin comprobantes registrados.")
        return "\n".join(lineas)

    def _notificar_cierre_reparto(self, reparto: Reparto, quien_cierra: Operator, resumen: str) -> None:
        """Encola un aviso para cada operador asociado al reparto DISTINTO de quien
        lo cerro -- el canal (telegram.py) los envia despues via pop_notificaciones()."""
        numero = reparto.numero_reparto if reparto.numero_reparto is not None else "sin numero"
        mensaje = (
            f"La Salida Nº {numero} en el movil {reparto.movil.numero} fue cerrada por {quien_cierra.nombre}.\n\n"
            f"{resumen}"
        )
        asociados = self.session.scalars(
            select(RepartoOperador).where(RepartoOperador.reparto_id == reparto.id)
        ).all()
        for asociado in asociados:
            if asociado.operador_id == quien_cierra.id:
                continue
            operador = self.session.get(Operator, asociado.operador_id)
            if operador is not None:
                self._notificaciones.append((operador.whatsapp_numero, mensaje))

    def _cerrar_reparto(self, operator: Operator, comando: CerrarRepartoComando) -> str:
        reparto_abierto = self._reparto_abierto_de_operador(operator.id)
        if reparto_abierto is None:
            return "No tenes ninguna salida abierta para cerrar."
        numero_real = reparto_abierto.numero_reparto
        if comando.numero_reparto is not None and numero_real is not None and numero_real != comando.numero_reparto:
            return (
                f"La salida abierta es la Nº {numero_real}, no la {comando.numero_reparto}. "
                "Reenvia el comando con el numero correcto."
            )
        # Cierra para todos los operadores asociados, no solo para quien manda el comando.
        reparto_abierto.hora_fin = ahora_argentina()
        resumen = self._resumen_bancario_cierre(reparto_abierto)
        self._notificar_cierre_reparto(reparto_abierto, operator, resumen)
        self.session.commit()
        etiqueta_numero = numero_real if numero_real is not None else "sin numero"
        return f"Salida Nº {etiqueta_numero} cerrada.\n\n{resumen}"

    def _crear_reparto_y_confirmar_comprobante(
        self, operator: Operator, conversation: WhatsAppConversation, numero_reparto: int
    ) -> str:
        movement = conversation.movimiento_borrador
        if movement is None:
            conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
            conversation.movil_pendiente_numero = None
            self.session.commit()
            return "La sesion vencio. Reenvia el comprobante, por favor."

        movil = self._buscar_movil_activo(conversation.movil_pendiente_numero)
        if movil is None:
            conversation.estado = ConversationState.ESPERANDO_MOVIL
            conversation.movil_pendiente_numero = None
            self.session.commit()
            return "Ese movil ya no esta disponible. ¿En que movil estas?"

        reparto = Reparto(
            movil_id=movil.id, fecha=hoy_argentina(), hora_inicio=ahora_argentina(), numero_reparto=numero_reparto
        )
        self.session.add(reparto)
        self.session.flush()
        self._asociar_operador(reparto, operator)

        movement.movil_id = movil.id
        movement.reparto_id = reparto.id
        movement.estado_registro = RecordState.CONFIRMADO
        conversation.movimiento_borrador_id = None
        conversation.movil_pendiente_numero = None
        conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
        self.session.commit()
        return (
            f"Comprobante registrado correctamente. Se inicio la Salida Nº {numero_reparto} en el movil "
            f"{movil.numero}."
        )

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
