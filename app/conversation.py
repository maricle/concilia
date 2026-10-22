import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import servicios
from .models import (
    ConversationState,
    Movement,
    Movil,
    Operator,
    Reparto,
    TipoIdentificador,
    WhatsAppConversation,
)
from .numeros import formato_monto_ar as _formato_monto_ar
from .repartos import CerrarRepartoComando, IniciarRepartoComando, parse_comando_reparto
from .servicios import ExtractedTransfer, buscar_cuenta_bancaria  # noqa: F401 (re-exportados)

logger = logging.getLogger(__name__)

NO_CUENTA_RECEPTORA_TEXTO = (
    "No pudimos identificar a que cuenta bancaria de la empresa corresponde este pago. "
    "Reenvia el comprobante, o si el problema persiste contacta al administrador para cargarlo manualmente."
)

SIN_SALIDA_ABIERTA_TEXTO = "No tenes una salida abierta. Indica el numero de salida para registrar el comprobante."

PENDIENTE_TEXTO = "Tenes movimientos pendientes."

_CANCELAR_TEXTO = {"no", "cancelar", "cancelo", "descartar"}

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


def _numero_salida(reparto: Reparto) -> int | str:
    return reparto.numero_reparto if reparto.numero_reparto is not None else "sin numero"


def _texto_pedir_movil(numero_reparto: int | None, varias: bool = False) -> str:
    if varias:
        return f"Hay mas de una Salida Nº {numero_reparto} abierta. ¿En que movil estas? Respondé con el numero del movil."
    return f"La Salida Nº {numero_reparto} no esta abierta. ¿En que movil la vas a iniciar? Respondé con el numero del movil."


def _texto_salida_ocupada(salida: "servicios.SalidaEnOtroMovil | servicios.MovilConOtraSalida") -> str:
    if isinstance(salida, servicios.MovilConOtraSalida):
        return (
            f"El movil {salida.movil.numero} ya tiene abierta la Salida Nº {_numero_salida(salida.reparto)}. "
            "Si vas en ese movil, indicá ese numero de salida."
        )
    return (
        f"La Salida Nº {_numero_salida(salida.reparto)} esta abierta en el movil {salida.reparto.movil.numero}. "
        "Revisá el numero de salida."
    )


def _formatear_fecha(fecha: datetime) -> str:
    """La hora solo se muestra si se detecto (fecha.hour/minute != 0); si el
    comprobante no traia hora, fecha_transaccion queda en medianoche y no tiene
    sentido mostrar una hora que nunca se leyo."""
    if fecha.hour or fecha.minute:
        return fecha.strftime("%Y-%m-%d %H:%M")
    return fecha.strftime("%Y-%m-%d")


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
            if conversation.numero_reparto_pendiente is None:
                numero_texto = text.strip()
                if not numero_texto.isdigit():
                    return "Ese no es un numero de salida valido. Respondé solo con el numero."
                if conversation.movil_pendiente_numero is None:
                    return self._entrar_a_salida(operator, conversation, int(numero_texto), None, confirmar_creacion=True)
                conversation.numero_reparto_pendiente = int(numero_texto)
            else:
                conversation.movil_pendiente_numero = text.strip()

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

        if conversation.estado == ConversationState.ESPERANDO_MOVIL:
            movement = conversation.movimiento_borrador
            if movement is None:
                conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
                self.session.commit()
                return "La sesion vencio. Reenvia el comprobante, por favor."
            if normalized in _CANCELAR_TEXTO:
                servicios.descartar_borrador(self.session, conversation)
                self.session.commit()
                return "Registro descartado. Puedes reenviar el comprobante."
            movil = servicios.buscar_movil_activo(self.session, text)
            if movil is None:
                return f"No encontramos un movil activo con el numero {text.strip()}. Respondé con el numero correcto."

            resultado = servicios.registrar_en_movil(self.session, operator, conversation, movement, movil)
            if isinstance(resultado, servicios.Registrado):
                return self._texto_registrado(resultado)
            if isinstance(resultado, servicios.MovilSinSalida):
                return (
                    f"No hay ninguna salida abierta en el movil {movil.numero}. ¿Queres iniciar una? "
                    "Respondé SI o NO."
                )
            return _texto_salida_ocupada(resultado)

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
                servicios.descartar_borrador(self.session, conversation)
                conversation.movil_pendiente_numero = None
                self.session.commit()
                return "Registro descartado. Puedes reenviar el comprobante."
            numero_texto = text.strip()
            if not numero_texto.isdigit():
                return "Ese no es un numero de salida valido. Respondé solo con el numero."
            if conversation.movil_pendiente_numero is None:
                return self._resolver_salida_por_numero(operator, conversation, int(numero_texto))
            return self._crear_reparto_y_confirmar_comprobante(operator, conversation, int(numero_texto))

        if conversation.estado == ConversationState.ESPERANDO_CONFIRMACION_DATOS:
            if normalized in {"si", "sí", "ok", "confirmo", "correcto"}:
                conversation.estado = ConversationState.ESPERANDO_TIPO_FACTURA_CUENTA
                self.session.commit()
                return "¿El dato que vas a cargar es un numero de factura o un numero de cuenta del cliente?"
            if normalized in _CANCELAR_TEXTO:
                servicios.descartar_borrador(self.session, conversation)
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
                servicios.descartar_borrador(self.session, conversation)
                self.session.commit()
                return "Registro descartado. Puedes reenviar el comprobante."
            factura_cuenta = servicios.interpretar_factura_cuenta(text)
            if factura_cuenta is not None:
                # Tipo y numero en un solo mensaje ("F 12345"): se registra
                # directo, sin el paso intermedio de pedir el numero.
                movement.factura_o_cuenta_tipo, movement.factura_o_cuenta_numero = factura_cuenta
                return self._registrar(operator, conversation, movement)
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
            if normalized in _CANCELAR_TEXTO:
                servicios.descartar_borrador(self.session, conversation)
                self.session.commit()
                return "Registro descartado. Puedes reenviar el comprobante."
            movement.factura_o_cuenta_numero = text.strip()
            # Sin una segunda confirmacion con el resumen repetido: el operador ya
            # vio los datos extraidos al elegir factura/cuenta, y el mensaje de
            # "registrado" le muestra el numero cargado -- si se equivoco, un
            # administrador lo corrige desde el panel (/comprobantes/{id}/editar).
            # Es un mensaje de WhatsApp (pago) menos por comprobante.
            return self._registrar(operator, conversation, movement)

        if conversation.estado == ConversationState.ESPERANDO_CONFIRMACION_FINAL:
            if normalized in {"si", "sí", "ok", "confirmo", "confirmar", "registrar"}:
                movement = conversation.movimiento_borrador
                if movement is None:
                    conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
                    self.session.commit()
                    return "La sesion vencio. Reenvia el comprobante, por favor."
                return self._registrar(operator, conversation, movement)
            if normalized in _CANCELAR_TEXTO:
                servicios.descartar_borrador(self.session, conversation)
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

    def needs_descartar_keyboard(self, number: str) -> bool:
        """True si el operador tiene que escribir un dato (numero de factura/cuenta,
        de salida o movil) de un comprobante a medio cargar -- se le ofrece un
        boton para descartarlo sin tener que escribir "descartar"."""
        conversation = self.session.get(WhatsAppConversation, number)
        if conversation is None or conversation.movimiento_borrador_id is None:
            return False
        return conversation.estado in {
            ConversationState.ESPERANDO_NUMERO_FACTURA_CUENTA,
            ConversationState.ESPERANDO_NUMERO_REPARTO_NUEVO,
            ConversationState.ESPERANDO_MOVIL,
        }

    def pending_prompt(self, number: str) -> str | None:
        """Si el operador ya tiene un comprobante sin cerrar, devuelve el mensaje que
        corresponde re-mostrarle en vez de arrancar uno nuevo (para no dejar el
        anterior huerfano). None si no hay nada pendiente y puede recibir un
        comprobante nuevo."""
        conversation = self.session.get(WhatsAppConversation, number)
        if conversation is None or conversation.estado == ConversationState.ESPERANDO_COMPROBANTE:
            return None

        if conversation.estado == ConversationState.ESPERANDO_TIPO_FACTURA_CUENTA:
            return f"{PENDIENTE_TEXTO} Indica el tipo y numero de comprobante (ej: F 12345) o descartalo."

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
            if conversation.movil_pendiente_numero is None:
                return f"{PENDIENTE_TEXTO} Indica el numero de salida o descartalo."
            return "¿Que numero de salida es? Respondé solo con el numero."

        movement = conversation.movimiento_borrador
        if movement is None:
            conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
            self.session.commit()
            return None

        if conversation.estado == ConversationState.ESPERANDO_CONFIRMACION_DATOS:
            return self._summary(movement) + "\n\nResponde SI para confirmar o NO para descartar."
        if conversation.estado == ConversationState.ESPERANDO_NUMERO_FACTURA_CUENTA:
            return f"{PENDIENTE_TEXTO} Indica el numero de {_etiqueta_tipo(movement.factura_o_cuenta_tipo)} o descartalo."
        if conversation.estado == ConversationState.ESPERANDO_CONFIRMACION_FINAL:
            return f"{PENDIENTE_TEXTO} {self._resumen_corto(movement)}. ¿Lo registro? Respondé SI, o NO para descartarlo."
        if conversation.estado == ConversationState.ESPERANDO_MOVIL:
            if conversation.numero_reparto_pendiente is not None:
                return f"{PENDIENTE_TEXTO} {_texto_pedir_movil(conversation.numero_reparto_pendiente)}"
            return f"{PENDIENTE_TEXTO} Indica en que movil estas o descartalo."
        return None

    def start_transfer(
        self, number: str, transfer: ExtractedTransfer, archivo_id: int | None = None, caption: str | None = None
    ) -> str:
        operator = self.session.scalar(select(Operator).where(Operator.whatsapp_numero == number, Operator.activo.is_(True)))
        if operator is None:
            return "Este numero no esta habilitado para registrar comprobantes."
        resultado = servicios.crear_borrador(self.session, operator, transfer, archivo_id)
        if resultado is servicios.Rechazo.COMPROBANTE_DUPLICADO:
            return "Ya existe un comprobante con ese numero de operacion."
        if resultado is servicios.Rechazo.SIN_CUENTAS_BANCARIAS:
            return NO_CUENTA_RECEPTORA_TEXTO
        movement = resultado

        # El resumen va junto con la pregunta de factura/cuenta: elegir una de las
        # dos es la confirmacion de que los datos extraidos estan bien (o
        # "Descartar" si no), sin un paso de SI/NO aparte.
        factura_cuenta = servicios.interpretar_factura_cuenta(caption)
        if factura_cuenta is None:
            return self._pedir_tipo_factura_cuenta(movement)
        # El operador ya mando factura/cuenta en el texto del comprobante: un solo
        # mensaje con el resumen completo para confirmar, y listo.
        movement.factura_o_cuenta_tipo, movement.factura_o_cuenta_numero = factura_cuenta
        servicios.conversacion_de(self.session, operator).estado = ConversationState.ESPERANDO_CONFIRMACION_FINAL
        self.session.commit()
        return self._summary(movement) + "\n\n¿Registro el comprobante? Respondé SI o NO."

    def _pedir_tipo_factura_cuenta(self, movement: Movement) -> str:
        return (
            self._summary(movement)
            + "\n\nSi los datos estan bien, elegi si vas a cargar un numero de factura o un numero de cuenta del cliente. "
            "Si no, descartalo."
            "\nTip: la proxima vez manda el comprobante con el texto \"F 12345\" o \"C 12345\" y te ahorras este paso."
            "\n\nIndica el tipo y numero de comprobante (ej: F 12345 o C 987)."
        )

    def _registrar(self, operator: Operator, conversation: WhatsAppConversation, movement: Movement) -> str:
        resultado = servicios.registrar(self.session, operator, conversation, movement)
        if isinstance(resultado, servicios.SinSalidaAbierta):
            return SIN_SALIDA_ABIERTA_TEXTO
        return self._texto_registrado(resultado)

    def _texto_registrado(self, resultado: servicios.Registrado) -> str:
        corto = self._resumen_corto(resultado.movimiento)
        if resultado.salida == servicios.SalidaUsada.PROPIA:
            return f"Comprobante registrado correctamente.\n{corto}"
        numero = resultado.reparto.numero_reparto
        if resultado.salida == servicios.SalidaUsada.NUEVA:
            return (
                f"Comprobante registrado correctamente. Se inicio la Salida Nº {numero} en el movil "
                f"{resultado.movil.numero}.\n{corto}"
            )
        return (
            f"Comprobante registrado correctamente. Salida Nº {_numero_salida(resultado.reparto)} en el movil "
            f"{resultado.movil.numero}.\n{corto}"
        )

    @staticmethod
    def _resumen_corto(movement: Movement) -> str:
        monto = f"${movement.monto}" if movement.monto is not None else "monto no detectado"
        return f"{_etiqueta_tipo(movement.factura_o_cuenta_tipo).capitalize()}: {movement.factura_o_cuenta_numero} · {monto}"

    def _resolver_salida_por_numero(
        self, operator: Operator, conversation: WhatsAppConversation, numero_reparto: int
    ) -> str:
        """El operador sin salida abierta indico el numero de salida: si esta
        abierta se registra ahi sin mas preguntas; si no, se le pide el movil
        para iniciarla."""
        movement = conversation.movimiento_borrador
        if movement is None:
            conversation.estado = ConversationState.ESPERANDO_COMPROBANTE
            self.session.commit()
            return "La sesion vencio. Reenvia el comprobante, por favor."

        resultado = servicios.resolver_salida_por_numero(self.session, operator, conversation, movement, numero_reparto)
        if isinstance(resultado, servicios.PedirMovil):
            return _texto_pedir_movil(numero_reparto, resultado.varias)
        return self._texto_registrado(resultado)

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
                reparto_propio = servicios.reparto_abierto_del_operador(self.session, operator.id)
                if reparto_propio is not None:
                    # Antes de pedirle datos para una salida nueva, se le pregunta si
                    # sigue con la que ya tiene abierta; si la cierra, recien ahi se
                    # piden los datos que falten.
                    return self._preguntar_cerrar_o_continuar(conversation, reparto_propio, comando)
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
        if conversation.numero_reparto_pendiente is None:
            return "¿Que numero de salida es? Respondé solo con el numero."
        return _texto_pedir_movil(conversation.numero_reparto_pendiente)

    def _iniciar_reparto(self, operator: Operator, comando: IniciarRepartoComando) -> str:
        movil = servicios.buscar_movil_activo(self.session, comando.movil_numero)
        if movil is None:
            return f"No encontramos un movil activo con el numero {comando.movil_numero}."

        # Un operador solo puede estar asociado a un reparto abierto a la vez -- se
        # chequea contra SU reparto propio actual (no contra el movil X recien
        # pedido), para no permitir que quede sumado a dos turnos en simultaneo.
        conversation = servicios.conversacion_de(self.session, operator)
        reparto_propio = servicios.reparto_abierto_del_operador(self.session, operator.id)
        if reparto_propio is not None:
            if reparto_propio.movil_id == movil.id:
                return f"Ya estas asociado a la Salida Nº {_numero_salida(reparto_propio)} en el movil {movil.numero}."
            return self._preguntar_cerrar_o_continuar(
                conversation, reparto_propio, IniciarRepartoComando(movil.numero, comando.numero_reparto)
            )
        return self._entrar_a_salida(operator, conversation, comando.numero_reparto, movil, confirmar_creacion=True)

    def _preguntar_cerrar_o_continuar(
        self, conversation: WhatsAppConversation, reparto_propio: Reparto, comando: IniciarRepartoComando
    ) -> str:
        """El operador quiere iniciar otra salida teniendo una abierta: se guarda
        lo que indico (puede faltar el movil o el numero) y se le pregunta."""
        conversation.estado = ConversationState.ESPERANDO_DECISION_REPARTO_ABIERTO
        conversation.movil_pendiente_numero = comando.movil_numero
        conversation.numero_reparto_pendiente = comando.numero_reparto
        self.session.commit()
        return (
            f"Ya tenes una salida abierta (Nº {_numero_salida(reparto_propio)}). Responde 'cerrar' para cerrarla "
            "y arrancar la nueva, o 'continuar' para seguir con la que ya esta abierta."
        )

    def _entrar_a_salida(
        self,
        operator: Operator,
        conversation: WhatsAppConversation,
        numero_reparto: int,
        movil: Movil | None,
        *,
        confirmar_creacion: bool,
    ) -> str:
        """Para un operador sin salida abierta: lo suma a la salida con ese
        numero si esta abierta (sin pedirle el movil); si no, la crea en el movil,
        pidiendo antes el movil o la confirmacion si hace falta. Avisa si el movil
        esta en otra salida o la salida en otro movil."""
        salida = servicios.ubicar_salida(self.session, numero_reparto, movil)
        self._limpiar_decision_reparto(conversation)
        if isinstance(salida, Reparto):
            servicios.asociar_operador(self.session, salida, operator)
            texto = f"Te sumaste a la Salida Nº {_numero_salida(salida)} en el movil {salida.movil.numero}."
        elif isinstance(salida, servicios.PedirMovil):
            conversation.estado = ConversationState.ESPERANDO_DATOS_INICIO_REPARTO
            conversation.numero_reparto_pendiente = numero_reparto
            texto = _texto_pedir_movil(numero_reparto, salida.varias)
        elif not isinstance(salida, servicios.SalidaNueva):
            conversation.estado = ConversationState.ESPERANDO_DATOS_INICIO_REPARTO
            texto = _texto_salida_ocupada(salida)
        elif confirmar_creacion:
            conversation.estado = ConversationState.ESPERANDO_CONFIRMACION_INICIO_REPARTO
            conversation.movil_pendiente_numero = salida.movil.numero
            conversation.numero_reparto_pendiente = numero_reparto
            texto = (
                f"Vas a iniciar la Salida Nº {numero_reparto} en el movil {salida.movil.numero}. "
                "Respondé SI para confirmar o NO para cancelar."
            )
        else:
            servicios.crear_salida(self.session, operator, salida.movil, numero_reparto)
            texto = f"Salida Nº {numero_reparto} iniciada en el movil {salida.movil.numero}."
        self.session.commit()
        return texto

    def _confirmar_inicio_reparto(self, operator: Operator, conversation: WhatsAppConversation) -> str:
        movil = servicios.buscar_movil_activo(self.session, conversation.movil_pendiente_numero)
        if movil is None:
            self._limpiar_decision_reparto(conversation)
            self.session.commit()
            return "El movil ya no esta disponible. Volve a mandar el comando de inicio."
        return self._entrar_a_salida(
            operator, conversation, conversation.numero_reparto_pendiente, movil, confirmar_creacion=False
        )

    def _cerrar_y_arrancar_reparto_pendiente(self, operator: Operator, conversation: WhatsAppConversation) -> str:
        reparto_propio = servicios.reparto_abierto_del_operador(self.session, operator.id)
        if reparto_propio is not None:
            otros = servicios.cerrar_salida(self.session, reparto_propio, operator)
            self._notificar_cierre_reparto(reparto_propio, operator, otros, self._resumen_bancario_cierre(reparto_propio))

        numero_reparto = conversation.numero_reparto_pendiente
        if numero_reparto is None:
            comando = IniciarRepartoComando(conversation.movil_pendiente_numero, None)
            return f"Salida anterior cerrada. {self._recopilar_datos_inicio_reparto(conversation, comando)}"
        if conversation.movil_pendiente_numero is None:
            texto = self._entrar_a_salida(operator, conversation, numero_reparto, None, confirmar_creacion=True)
            return f"Salida anterior cerrada. {texto}"
        movil = servicios.buscar_movil_activo(self.session, conversation.movil_pendiente_numero)
        if movil is None:
            self._limpiar_decision_reparto(conversation)
            self.session.commit()
            return "El movil que habias indicado ya no esta disponible. Volve a mandar el comando de inicio."
        texto = self._entrar_a_salida(operator, conversation, numero_reparto, movil, confirmar_creacion=False)
        return f"Salida anterior cerrada. {texto}"

    def _resumen_bancario_cierre(self, reparto: Reparto) -> str:
        """Texto con el total por banco de los comprobantes registrados durante la
        salida, para avisar al cerrarla -- reemplaza el PDF que se mandaba antes."""
        totales = servicios.totales_de_salida(self.session, reparto)
        sin_banco = servicios.SIN_BANCO_IDENTIFICADO
        lineas = [f"Cierre Salida Nº {_numero_salida(reparto)}"]
        for banco in sorted(banco for banco in totales.por_banco if banco != sin_banco):
            lineas.append(f"{banco}: {_formato_monto_ar(totales.por_banco[banco])}")
        if sin_banco in totales.por_banco:
            lineas.append(f"{sin_banco}: {_formato_monto_ar(totales.por_banco[sin_banco])}")
        if not totales.cantidad_comprobantes:
            lineas.append("Sin comprobantes registrados.")
        return "\n".join(lineas)

    def _notificar_cierre_reparto(
        self, reparto: Reparto, quien_cierra: Operator, otros: list[Operator], resumen: str
    ) -> None:
        """Encola un aviso para los demas operadores de la salida -- el canal los
        envia despues via pop_notificaciones()."""
        mensaje = (
            f"La Salida Nº {_numero_salida(reparto)} en el movil {reparto.movil.numero} fue cerrada por {quien_cierra.nombre}.\n\n"
            f"{resumen}"
        )
        for operador in otros:
            self._notificaciones.append((operador.whatsapp_numero, mensaje))

    def _cerrar_reparto(self, operator: Operator, comando: CerrarRepartoComando) -> str:
        reparto_abierto = servicios.reparto_abierto_del_operador(self.session, operator.id)
        if reparto_abierto is None:
            return "No tenes ninguna salida abierta para cerrar."
        numero_real = reparto_abierto.numero_reparto
        if comando.numero_reparto is not None and numero_real is not None and numero_real != comando.numero_reparto:
            return (
                f"La salida abierta es la Nº {numero_real}, no la {comando.numero_reparto}. "
                "Reenvia el comando con el numero correcto."
            )
        # Cierra para todos los operadores asociados, no solo para quien manda el comando.
        otros = servicios.cerrar_salida(self.session, reparto_abierto, operator)
        resumen = self._resumen_bancario_cierre(reparto_abierto)
        self._notificar_cierre_reparto(reparto_abierto, operator, otros, resumen)
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

        resultado = servicios.crear_salida_y_registrar(
            self.session, operator, conversation, movement, conversation.movil_pendiente_numero, numero_reparto
        )
        if isinstance(resultado, servicios.MovilNoDisponible):
            return "Ese movil ya no esta disponible. ¿En que movil estas?"
        return self._texto_registrado(resultado)

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
