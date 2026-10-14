import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from agent.graph import MENSAJE_NEGOCIO_NO_DISPONIBLE, enviar_mensaje, enviar_respuesta_humana, obtener_historial
from core.settings import (
    CHAT_LIMITE_IP_POR_MINUTO,
    CHAT_LIMITE_SESION_POR_HORA,
    CHAT_LIMITE_SESION_POR_MINUTO,
    CHAT_MENSAJE_MAX_CARACTERES,
)
from services import rate_limit
from services.ai_usage_tracking import presupuesto_agotado
from services.auth import obtener_usuario_actual, verificar_acceso_negocio
from services.db import get_business_by_id, get_employee_by_id
from services.whatsapp import construir_mensaje_contacto_humano

router = APIRouter(prefix="/chat", tags=["chat"])
business_chat_router = APIRouter(prefix="/business/{business_id}/chat-sessions", tags=["chat"])


def _obtener_negocio_o_404(business_id: str) -> dict:
    business = get_business_by_id(business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Negocio no encontrado")
    return business


def _obtener_empleado_o_404(business_id: str, employee_id: str) -> dict:
    """
    Valida el empleado del enlace de chat individual: debe existir,
    pertenecer a este negocio, y estar activo. 404 en cualquier otro caso
    (no distinguimos el motivo para no filtrar informacion interna).
    """
    empleado = get_employee_by_id(employee_id)
    if not empleado or empleado["business_id"] != business_id or not empleado.get("active"):
        raise HTTPException(status_code=404, detail="Empleado no encontrado")
    return empleado


def _negocio_habilitado(business: dict) -> bool:
    # onboarding_completed usa default True: un negocio ya existente (creado
    # antes de este flag, o si por algun motivo la columna no vino en el
    # select) nunca debe perder su chat por accidente. Solo los negocios
    # nuevos, que arrancan en False, quedan bloqueados hasta terminar el
    # onboarding (ver routes/business_settings_routes.py:complete_onboarding).
    return (
        business.get("plan", "basic") != "none"
        and not business.get("blocked")
        and business.get("onboarding_completed", True)
    )


def _historial_o_503(business_id: str, session_id: str) -> tuple[list, list | None]:
    """
    obtener_historial lee el checkpointer en Postgres. Si la base falla (ej.
    sin conexiones libres en el pooler) se responde 503 en vez de un 500
    crudo: el widget lo trata como algo temporal y conserva la sesion del
    cliente (antes la borraba y la conversacion se perdia).
    """
    try:
        return obtener_historial(business_id, session_id)
    except Exception as e:
        print(f"No se pudo leer el historial del chat {business_id}:{session_id}: {type(e).__name__}: {e}")
        raise HTTPException(status_code=503, detail="Historial temporalmente no disponible")


MENSAJE_DEMASIADO_LARGO = (
    "Tu mensaje es muy largo para que pueda leerlo bien 🙏. "
    "¿Me lo cuentas de forma más corta?"
)
MENSAJE_ASISTENTE_NO_DISPONIBLE = "El asistente automático no está disponible por ahora."


def _ip_del_cliente(request: Request) -> str:
    """
    IP de quien escribe. Detras de un proxy (nginx, Cloudflare) se toma la
    ULTIMA entrada de X-Forwarded-For, que es la que agrego el proxy de
    confianza: las anteriores las puede falsear el cliente para esquivar el
    limite.
    """
    reenviado = request.headers.get("x-forwarded-for")
    if reenviado:
        ultima = reenviado.split(",")[-1].strip()
        if ultima:
            return ultima
    return request.client.host if request.client else "desconocida"


def _aplicar_salvaguardas(
    request: Request, business: dict, session_id: str, data: "ChatMessageInput"
) -> "ChatMessageResponse | None":
    """
    Controles de costo que corren ANTES de invocar al modelo (cada mensaje que
    pasa cuesta tokens de OpenAI). Devuelve una respuesta lista si el mensaje
    no debe procesarse, o None si puede seguir. Ver
    docs/ESTRATEGIA_OPTIMIZACION_TOKENS.md (T0.2).
    """
    if len(data.mensaje) > CHAT_MENSAJE_MAX_CARACTERES:
        return ChatMessageResponse(respuesta=MENSAJE_DEMASIADO_LARGO)

    ip = _ip_del_cliente(request)
    if (
        not rate_limit.permitir(f"ip:{ip}", CHAT_LIMITE_IP_POR_MINUTO, 60)
        or not rate_limit.permitir(f"sesion-min:{session_id}", CHAT_LIMITE_SESION_POR_MINUTO, 60)
        or not rate_limit.permitir(f"sesion-hora:{session_id}", CHAT_LIMITE_SESION_POR_HORA, 3600)
    ):
        raise HTTPException(
            status_code=429,
            detail="Estás enviando mensajes muy rápido. Espera un momento e inténtalo de nuevo.",
            headers={"Retry-After": "30"},
        )

    if presupuesto_agotado(business):
        return ChatMessageResponse(
            respuesta=MENSAJE_ASISTENTE_NO_DISPONIBLE + "\n\n" + construir_mensaje_contacto_humano(business)
        )

    return None


def _validar_session_id(session_id: str) -> str:
    try:
        return str(uuid.UUID(session_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="session_id invalido")


class ChatConfigResponse(BaseModel):
    business_id: str
    name: str
    business_type: str | None = None
    enabled: bool
    employee_id: str | None = None
    employee_name: str | None = None


class CreateSessionResponse(BaseModel):
    session_id: str


class ChatMessageInput(BaseModel):
    mensaje: str
    # Id que genera el widget por cada mensaje (y que reutiliza si reintenta
    # el envio): hace el envio idempotente, ver agent/graph.py:enviar_mensaje.
    # Opcional para no romper clientes viejos del widget que no lo mandan.
    client_message_id: str | None = Field(default=None, max_length=64)


class ChatOption(BaseModel):
    """Opcion de seleccion rapida (boton) para el widget de chat: el cliente toca en vez de escribir."""

    label: str
    value: str


class ChatMessageResponse(BaseModel):
    respuesta: str
    opciones: list[ChatOption] | None = None


class ChatHistoryMessage(BaseModel):
    role: str
    content: str


class ChatHistoryResponse(BaseModel):
    session_id: str
    mensajes: list[ChatHistoryMessage]
    opciones: list[ChatOption] | None = None


@router.get("/{business_id}/config", response_model=ChatConfigResponse)
def obtener_config_chat(business_id: str):
    """
    Info publica que el widget de chat necesita antes de arrancar: nombre
    del negocio y si el agendamiento automatico esta activo (plan distinto
    de "none" y no bloqueado). El enlace `/chat/<business_id>` es el
    enlace general del negocio: el cliente elige el empleado conversando.
    """
    business = _obtener_negocio_o_404(business_id)
    return ChatConfigResponse(
        business_id=business["id"],
        name=business.get("name", ""),
        business_type=business.get("business_type"),
        enabled=_negocio_habilitado(business),
    )


@router.post("/{business_id}/sessions", response_model=CreateSessionResponse)
def crear_sesion_chat(business_id: str):
    """
    Crea una sesion de chat nueva (conversacion vacia) para este negocio.
    El frontend guarda el session_id (ej: localStorage) y lo reutiliza en
    cada mensaje de esa visita; para empezar de cero basta con crear otra sesion.
    """
    _obtener_negocio_o_404(business_id)
    return CreateSessionResponse(session_id=str(uuid.uuid4()))


@router.post("/{business_id}/sessions/{session_id}/messages", response_model=ChatMessageResponse)
def enviar_mensaje_chat(business_id: str, session_id: str, data: ChatMessageInput, request: Request):
    """
    Manda un mensaje del cliente al agente de este negocio y devuelve su
    respuesta. Aislado por negocio en dos capas: el business_id de la URL,
    y el thread_id interno del checkpointer (`business_id:session_id`), asi
    que un session_id nunca puede continuar la conversacion de otro negocio.
    """
    business = _obtener_negocio_o_404(business_id)
    session_id = _validar_session_id(session_id)

    if not _negocio_habilitado(business):
        return ChatMessageResponse(respuesta=MENSAJE_NEGOCIO_NO_DISPONIBLE)

    bloqueado = _aplicar_salvaguardas(request, business, session_id, data)
    if bloqueado is not None:
        return bloqueado

    respuesta, opciones = enviar_mensaje(
        business_id, session_id, data.mensaje, client_message_id=data.client_message_id
    )
    return ChatMessageResponse(respuesta=respuesta, opciones=opciones)


@router.get("/{business_id}/sessions/{session_id}/messages", response_model=ChatHistoryResponse)
def obtener_historial_chat(business_id: str, session_id: str):
    """Historial de una sesion (para recargar el chat si el cliente refresca la pagina)."""
    _obtener_negocio_o_404(business_id)
    session_id = _validar_session_id(session_id)
    historial, opciones = _historial_o_503(business_id, session_id)
    return ChatHistoryResponse(session_id=session_id, mensajes=historial, opciones=opciones)


# ---------------------------------------------------------------------
# Chat propio de un empleado: mismo contrato que arriba, pero con el
# empleado fijo (el cliente no lo elige conversando, ya viene en la URL).
# ---------------------------------------------------------------------


@router.get("/{business_id}/{employee_id}/config", response_model=ChatConfigResponse)
def obtener_config_chat_empleado(business_id: str, employee_id: str):
    business = _obtener_negocio_o_404(business_id)
    empleado = _obtener_empleado_o_404(business_id, employee_id)
    return ChatConfigResponse(
        business_id=business["id"],
        name=business.get("name", ""),
        business_type=business.get("business_type"),
        enabled=_negocio_habilitado(business),
        employee_id=empleado["id"],
        employee_name=empleado.get("name"),
    )


@router.post("/{business_id}/{employee_id}/sessions", response_model=CreateSessionResponse)
def crear_sesion_chat_empleado(business_id: str, employee_id: str):
    _obtener_negocio_o_404(business_id)
    _obtener_empleado_o_404(business_id, employee_id)
    return CreateSessionResponse(session_id=str(uuid.uuid4()))


@router.post("/{business_id}/{employee_id}/sessions/{session_id}/messages", response_model=ChatMessageResponse)
def enviar_mensaje_chat_empleado(
    business_id: str, employee_id: str, session_id: str, data: ChatMessageInput, request: Request
):
    business = _obtener_negocio_o_404(business_id)
    _obtener_empleado_o_404(business_id, employee_id)
    session_id = _validar_session_id(session_id)

    if not _negocio_habilitado(business):
        return ChatMessageResponse(respuesta=MENSAJE_NEGOCIO_NO_DISPONIBLE)

    bloqueado = _aplicar_salvaguardas(request, business, session_id, data)
    if bloqueado is not None:
        return bloqueado

    respuesta, opciones = enviar_mensaje(
        business_id, session_id, data.mensaje, employee_id=employee_id, client_message_id=data.client_message_id
    )
    return ChatMessageResponse(respuesta=respuesta, opciones=opciones)


@router.get("/{business_id}/{employee_id}/sessions/{session_id}/messages", response_model=ChatHistoryResponse)
def obtener_historial_chat_empleado(business_id: str, employee_id: str, session_id: str):
    _obtener_negocio_o_404(business_id)
    _obtener_empleado_o_404(business_id, employee_id)
    session_id = _validar_session_id(session_id)
    historial, opciones = _historial_o_503(business_id, session_id)
    return ChatHistoryResponse(session_id=session_id, mensajes=historial, opciones=opciones)


# ---------------------------------------------------------------------
# Vista del negocio sobre una conversacion (autenticado): a diferencia de
# las rutas publicas de arriba (pensadas para el propio widget, que ya
# conoce y controla su session_id), estas exigen ser dueño o empleado
# activo del negocio, para poder leer y responder una conversacion desde
# el panel cuando el bot escalo (agent/tools.py:escalar_por_confusion /
# transferir_a_equipo).
# ---------------------------------------------------------------------


class ReplyInput(BaseModel):
    mensaje: str


@business_chat_router.get("/{session_id}", response_model=ChatHistoryResponse)
def obtener_conversacion_negocio(business_id: str, session_id: str, user_id: str = Depends(obtener_usuario_actual)):
    verificar_acceso_negocio(business_id, user_id)
    session_id = _validar_session_id(session_id)
    historial, _ = obtener_historial(business_id, session_id)
    return ChatHistoryResponse(session_id=session_id, mensajes=historial)


@business_chat_router.post("/{session_id}/reply", response_model=ChatHistoryResponse)
def responder_conversacion_negocio(
    business_id: str, session_id: str, data: ReplyInput, user_id: str = Depends(obtener_usuario_actual)
):
    verificar_acceso_negocio(business_id, user_id)
    session_id = _validar_session_id(session_id)
    enviar_respuesta_humana(business_id, session_id, data.mensaje)
    historial, _ = obtener_historial(business_id, session_id)
    return ChatHistoryResponse(session_id=session_id, mensajes=historial)
