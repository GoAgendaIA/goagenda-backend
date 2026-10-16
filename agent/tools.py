import json
import re
import unicodedata
from datetime import datetime
from typing import Annotated, Optional

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import Field
from rapidfuzz import fuzz

from services.db import (
    get_home_visit_zones,
    get_services,
    get_client_appointments,
    get_appointment_by_id_and_phone,
    update_appointment_schedule,
    get_employees,
    get_employee_by_id,
    get_employee_services,
    get_business_by_id,
)
from services.geocoding import geocodificar, localidades_de
from services.push_notifications import enviar_notificacion_escalamiento
from services.realtime import gestor_tiempo_real
from services.scheduling import (
    DIAS_SEMANA_ES,
    DIAS_MAP,
    ahora_local,
    es_hora_valida,
    hay_choque_de_horario,
    generar_horas_disponibles,
    obtener_horario_dia,
    formatear_fecha_natural,
)
from services.whatsapp import normalizar_numero_whatsapp

BusinessId = Annotated[str, InjectedState("business_id")]
SessionId = Annotated[str, InjectedState("session_id")]
ClientPhone = Annotated[Optional[str], InjectedState("client_phone")]
EmployeeId = Annotated[Optional[str], InjectedState("employee_id")]
EmployeeFijo = Annotated[bool, InjectedState("employee_fijo")]


def _formato_hora_12h(hora_24: str) -> str:
    """Convierte 'HH:MM' (24h) a texto tipo '9:00 am', para mostrarle al cliente."""
    return datetime.strptime(hora_24, "%H:%M").strftime("%I:%M %p").lstrip("0").lower()


def _horas_por_franja(horas_24: list[str]) -> str:
    """
    Todas las horas libres agrupadas en 'En la mañana' (antes de las 12:00) y
    'En la tarde' (desde las 12:00), una hora por linea, en 12 h y listas para
    mostrarle al cliente tal cual. La arma el codigo y no el modelo: asi no
    omite, inventa ni reordena horas.
    """
    lineas = []
    for titulo, horas in (
        ("🌅 *En la mañana:*", [h for h in horas_24 if h < "12:00"]),
        ("🌇 *En la tarde:*", [h for h in horas_24 if h >= "12:00"]),
    ):
        if horas:
            lineas.append(titulo)
            lineas.extend(f"• {_formato_hora_12h(h)}" for h in horas)
    return "\n".join(lineas)


def _domicilios_activos(business_id: str) -> bool:
    """Flag del negocio: con los domicilios desactivados el chat se comporta como antes (solo en el local)."""
    negocio = get_business_by_id(business_id)
    return bool(negocio and negocio.get("home_visits_enabled"))


def _validar_domicilio(business_id: str, servicio: dict | None, direccion: str | None, mensajes: list) -> str | None:
    """
    Valida una cita a domicilio (direccion presente). Devuelve un mensaje de
    error, o None si esta bien. Solo se puede pedir domicilio en servicios que
    lo permiten, y la direccion debe salir de lo que el cliente escribio: el
    modelo llego a inventar datos (ej. un telefono) que el cliente nunca dio.
    """
    if not direccion or not direccion.strip():
        return None
    if not _domicilios_activos(business_id):
        return "Este negocio no hace domicilios. La cita solo puede ser en el local; no vuelvas a ofrecer domicilio."
    if servicio and not servicio.get("offers_home_visit"):
        return "Ese servicio no se presta a domicilio. Ofrecele agendarlo en el local."

    def _tokens(texto: str) -> set[str]:
        return {t for t in re.findall(r"[a-z0-9áéíóúñ]+", texto.lower()) if len(t) >= 2}

    texto_cliente = " ".join(str(m.content) for m in mensajes if getattr(m, "type", None) == "human")
    tokens_direccion = _tokens(direccion)
    if tokens_direccion and len(tokens_direccion & _tokens(texto_cliente)) / len(tokens_direccion) < 0.7:
        return "El cliente todavia no ha dado esa direccion. NO la inventes: pidele la direccion completa donde debemos ir."
    return None


def _normalizar(texto: str) -> str:
    """Minusculas y sin tildes, para comparar nombres de zonas con lo que escribio el cliente."""
    sin_tildes = unicodedata.normalize("NFD", texto or "")
    return "".join(c for c in sin_tildes if unicodedata.category(c) != "Mn").lower().strip()


# Umbrales de similitud (0-100, rapidfuzz.fuzz.ratio) para el matching
# aproximado de zonas: tolera errores de tipeo/tildes que el substring
# literal exacto no perdonaba (ej. "Betulua" en vez de "Betulia").
# Calibrados empiricamente con nombres de municipios/barrios reales: typos
# de 1 caracter (sustitucion, omision) en palabras de 5+ letras dan
# consistentemente 80-93; pares de lugares realmente distintos dan 0-46 -
# hay un margen amplio (46 a 80) entre "typo" y "otra cosa" para separar
# los 3 niveles con seguridad.
# FUERTE: se acepta automaticamente (typo menor, tilde, mayuscula).
# SUGERIR: parecido pero no suficiente para asumirlo solo - el bot debe
#   confirmarlo explicitamente con el cliente antes de continuar, nunca
#   adivinar en silencio.
_UMBRAL_ZONA_FUERTE = 84
_UMBRAL_ZONA_SUGERIR = 68


def _generar_ngramas(tokens: list[str], n: int) -> list[str]:
    """Todas las secuencias de n palabras consecutivas en tokens (para comparar contra nombres de zona de varias palabras)."""
    if n <= 0 or len(tokens) < n:
        return []
    return [" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def _nombres_candidatos_zona(zona: dict) -> list[str]:
    """El nombre de la zona mas sus alias (variantes que el dueño registro), todos como posibles textos a matchear."""
    return [zona["name"], *[a for a in (zona.get("aliases") or []) if a]]


def _mejor_coincidencia_zona(texto_normalizado: str, zonas: list[dict]) -> tuple[dict | None, int]:
    """
    Busca, entre el nombre y los alias de cada zona, cual tiene mayor
    similitud aproximada (rapidfuzz.fuzz.ratio) con ALGUNA secuencia de
    palabras consecutivas del texto normalizado dado. Retorna
    (zona con mejor score, score 0-100), o (None, 0) si zonas esta vacio.
    """
    tokens = texto_normalizado.split()
    mejor_zona: dict | None = None
    mejor_score = 0

    for z in zonas:
        for nombre in _nombres_candidatos_zona(z):
            nombre_normalizado = _normalizar(nombre)
            n_palabras = len(nombre_normalizado.split())
            for ngrama in _generar_ngramas(tokens, n_palabras):
                score = fuzz.ratio(nombre_normalizado, ngrama)
                if score > mejor_score:
                    mejor_score = score
                    mejor_zona = z

    return mejor_zona, mejor_score


def _texto_menciona_zona(texto_normalizado: str, zona: dict) -> bool:
    """True si el nombre de la zona (o un alias) aparece, exacto o con tolerancia fuerte a typos, en el texto dado."""
    for nombre in _nombres_candidatos_zona(zona):
        if _normalizar(nombre) in texto_normalizado:
            return True
    _, score = _mejor_coincidencia_zona(texto_normalizado, [zona])
    return score >= _UMBRAL_ZONA_FUERTE


def _zona_por_localidad(localidad: str, zonas: list[dict]) -> dict | None:
    """Una zona cuyo nombre o alias coincide (tolerancia fuerte a typos) con el nombre de una localidad geocodificada."""
    localidad_normalizada = _normalizar(localidad)
    for z in zonas:
        for nombre in _nombres_candidatos_zona(z):
            if fuzz.ratio(_normalizar(nombre), localidad_normalizada) >= _UMBRAL_ZONA_FUERTE:
                return z
    return None


def _inferir_zona_por_geocodificacion(direccion: str, zonas: list[dict]) -> tuple[dict | None, str | None]:
    """
    Intenta geocodificar la direccion (Nominatim, ver services/geocoding.py)
    y, si alguna de las localidades que devuelve (barrio, ciudad, municipio)
    coincide con una zona configurada, la retorna directamente - asi el
    cliente no tiene que decir el nombre EXACTO de la zona, basta con dar
    una direccion real.

    Retorna (zona, None) si encontro una coincidencia; (None, lugar) si
    geocodifico una direccion real pero ninguna localidad coincide con una
    zona (para armar un mensaje de "fuera de cobertura" mas preciso, con el
    lugar real en vez de un generico); o (None, None) si Nominatim no
    encontro nada o fallo - en ese caso el caller sigue con el matching por
    texto de siempre, sin bloquear el flujo por esto.
    """
    resultado_geo = geocodificar(direccion)
    if not resultado_geo:
        return None, None

    localidades = localidades_de(resultado_geo)
    for localidad in localidades:
        zona_geo = _zona_por_localidad(localidad, zonas)
        if zona_geo:
            return zona_geo, None

    return None, (localidades[0] if localidades else None)


def _resolver_zona(
    business_id: str,
    direccion: str | None,
    zona: str | None,
    mensajes: list,
    session_id: str | None = None,
    client_phone: str | None = None,
) -> tuple[dict | None, str | None]:
    """
    Politica de domicilios del negocio: valida que el lugar del cliente este
    dentro de las zonas donde se hacen domicilios y devuelve (zona, error).
    - Sin zonas configuradas: domicilio libre, sin recargo (zona None, sin error).
    - Con zonas: primero intenta geocodificar la direccion real (Nominatim)
      para inferir la zona automaticamente sin que el cliente tenga que
      decir su nombre exacto; si eso no aplica (direccion no encontrada,
      Nominatim caido, o el lugar real no coincide con ninguna zona), cae al
      matching por texto: nombre/alias de una zona activa en lo que escribio
      el cliente, tolerando errores de tipeo/tildes/mayusculas (rapidfuzz)
      en vez de exigir el substring exacto de antes. Un match solo
      "parecido" (no fuerte) NUNCA se acepta en silencio: el error le pide
      al bot que se lo confirme al cliente antes de continuar.
    - Cuando el lugar del cliente SI se identifico con certeza (por nombre
      exacto/alias, o por geocodificacion) pero NO esta entre las zonas
      configuradas, se avisa al negocio en silencio (mismo mecanismo que
      escalar_por_confusion: push + evento en tiempo real, el cliente nunca
      se entera) para que un humano decida si hace una excepcion - el bot
      igual le responde al cliente el mensaje normal de fuera de cobertura.
      Esto NO aplica cuando todavia no se sabe en que zona esta el cliente
      (se le esta pidiendo que aclare): ahi no hay nada que un humano deba
      decidir todavia.
    """
    if not direccion or not direccion.strip():
        return None, None

    zonas = [z for z in get_home_visit_zones(business_id) if z.get("active", True)]
    if not zonas:
        return None, None

    texto_cliente = _normalizar(
        direccion + " " + " ".join(str(m.content) for m in mensajes if getattr(m, "type", None) == "human")
    )
    listado = ", ".join(f"{z['name']} (recargo {_formato_precio_cop(z['fee'])})" for z in zonas)

    def _avisar_negocio_fuera_de_cobertura() -> None:
        if session_id:
            _notificar_negocio_escalamiento(business_id, session_id, client_phone or "Un cliente")

    if zona and zona.strip():
        zona_normalizada = _normalizar(zona)
        elegida = next(
            (
                z
                for z in zonas
                if zona_normalizada == _normalizar(z["name"])
                or zona_normalizada in {_normalizar(a) for a in (z.get("aliases") or [])}
            ),
            None,
        )
        if not elegida:
            mejor_zona, score = _mejor_coincidencia_zona(zona_normalizada, zonas)
            if mejor_zona and score >= _UMBRAL_ZONA_FUERTE:
                elegida = mejor_zona
            else:
                _avisar_negocio_fuera_de_cobertura()
                return None, (
                    f"Ese lugar esta fuera de la zona de cobertura de domicilios. Zonas donde SI se llega: {listado}. "
                    "Explicaselo al cliente y ofrecele agendar en el local o en una de esas zonas."
                )
    else:
        exactas = [z for z in zonas if any(_normalizar(n) in texto_cliente for n in _nombres_candidatos_zona(z))]
        if exactas:
            elegida = exactas[0]
        else:
            # La geocodificacion es SIEMPRE una señal positiva (para inferir
            # sin que el cliente diga el nombre), nunca una negativa
            # definitiva por si sola: Nominatim puede resolver una
            # direccion ambigua hacia el lugar equivocado (ej. "Laureles"
            # sin mas contexto puede caer en un barrio de Bogota en vez del
            # de Medellin donde de verdad opera el negocio). Si no matchea
            # ninguna zona, NO se rechaza aqui - se sigue intentando por
            # texto (que compara contra lo que el cliente realmente
            # escribio) y el lugar geocodificado solo se usa para enriquecer
            # el mensaje final si de verdad no se encuentra nada por ningun
            # lado.
            zona_geo, lugar_geo = _inferir_zona_por_geocodificacion(direccion, zonas)
            if zona_geo:
                # Inferida de la direccion real (no de palabras que el
                # cliente escribio): el nombre de la zona puede no aparecer
                # literalmente en su mensaje, asi que se retorna directo,
                # sin pasar por el guard de texto de mas abajo (pensado
                # para detectar zonas que el modelo invento, no aplica aqui
                # porque el origen es geocodificacion, no el LLM).
                return zona_geo, None

            mejor_zona, score = _mejor_coincidencia_zona(texto_cliente, zonas)
            if mejor_zona and score >= _UMBRAL_ZONA_FUERTE:
                elegida = mejor_zona
            elif mejor_zona and score >= _UMBRAL_ZONA_SUGERIR:
                return None, (
                    f"El cliente podria estar refiriendose a la zona '{mejor_zona['name']}' "
                    f"(recargo {_formato_precio_cop(mejor_zona['fee'])}), pero no es seguro por como lo escribio. "
                    "PREGUNTALE explicitamente si es esa zona antes de continuar - NO la des por confirmada tu "
                    f"solo. Si dice que no es esa, o menciona otro lugar, vuelve a intentar. Zonas donde SI se "
                    f"llega: {listado}."
                )
            elif lugar_geo:
                # Ni el texto ni la zona geocodificada matchean ninguna
                # zona: aqui si se usa el lugar geocodificado, como
                # informacion adicional (no como unica fuente de verdad).
                # A diferencia del "else" de abajo, aqui SI se identifico un
                # lugar real (solo que no esta cubierto), asi que si aplica
                # avisar al negocio.
                _avisar_negocio_fuera_de_cobertura()
                return None, (
                    f"Segun la direccion, el cliente podria estar en {lugar_geo}, que no esta entre las zonas de "
                    f"cobertura de domicilios. Confirmaselo y ofrecele agendar en el local o en: {listado}."
                )
            else:
                return None, (
                    "No puedo confirmar si esa direccion esta dentro de la cobertura de domicilios. "
                    f"Pregunta al cliente en que municipio/zona esta. Zonas donde SI se llega: {listado}."
                )

    if not _texto_menciona_zona(texto_cliente, elegida):
        return None, (
            "El cliente todavia no ha dicho en que municipio/zona esta. NO lo supongas: preguntaselo. "
            f"Zonas donde SI se llega: {listado}."
        )
    return elegida, None


_DIAS_MESES = (
    r"lunes|martes|miercoles|jueves|viernes|sabado|domingo|"
    r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre"
)
_PATRON_FECHA = re.compile(
    rf"\b(hoy|manana|pasado manana|semana|{_DIAS_MESES})\b"  # palabras de fecha
    r"|\b\d{4}-\d{2}-\d{2}\b"  # 2026-09-21
    r"|\b\d{1,2}/\d{1,2}\b"  # 21/09
    r"|\b(el|dia)\s+\d{1,2}\b"  # el 21, dia 21
)


def _cliente_dio_fecha(mensajes: list) -> bool:
    """
    True si en algun mensaje del cliente hay una referencia a una fecha (hoy,
    mañana, un dia de la semana, un mes, "el 21", 21/09...). Guardia contra
    alucinaciones: el modelo llego a elegir una fecha por su cuenta
    (ej. "lunes 21") sin que el cliente la dijera. Las direcciones
    ("calle 24 nro 15-18") no cuentan como fecha.
    """
    texto = _normalizar(" ".join(str(m.content) for m in mensajes if getattr(m, "type", None) == "human"))
    return bool(_PATRON_FECHA.search(texto))


MENSAJE_FALTA_FECHA = (
    "El cliente todavia NO ha dicho para que dia quiere la cita. NO elijas ni supongas una fecha: "
    "preguntale para que dia le gustaria (ej: hoy, mañana, o una fecha) y espera su respuesta."
)


def _formato_precio_cop(precio) -> str:
    """Formatea un precio como '$15.000' (separador de miles con punto, sin decimales)."""
    try:
        return f"${int(round(float(precio))):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "$0"


def _json_compacto(dato) -> str:
    """
    Serializa el resultado de una tool para el modelo: JSON sin espacios en vez
    del repr de Python (str(dict)). Es mas corto, y sus booleanos (true/false)
    coinciden con como los nombra el prompt. Se reenvia en cada llamada
    posterior del turno, asi que cada caracter cuenta (T1.3).
    """
    return json.dumps(dato, ensure_ascii=False, separators=(",", ":"), default=str)


def _numero_limpio(valor):
    """15000.0 -> 15000 (sin decimales inutiles); cualquier otra cosa queda igual."""
    try:
        return int(valor) if float(valor).is_integer() else valor
    except (TypeError, ValueError):
        return valor


def _servicio_para_modelo(servicio: dict, domicilios_activos: bool) -> dict:
    """
    Solo los campos de un servicio que el modelo usa. La fila de la base trae
    columnas internas (business_id, created_at, active, campos de abono...) que
    se pagaban en cada llamada sin servirle de nada.
    """
    fila = {
        "id": servicio.get("id"),
        "name": servicio.get("name"),
        "duration_minutes": servicio.get("duration_minutes"),
        "price": _numero_limpio(servicio.get("price")),
    }
    # Domicilios desactivados: el modelo no debe ver el campo, asi no ofrece domicilio.
    if domicilios_activos and "offers_home_visit" in servicio:
        fila["offers_home_visit"] = bool(servicio["offers_home_visit"])
    if servicio.get("requires_payment"):
        fila["requires_payment"] = True
        for campo in ("payment_type", "payment_percentage", "payment_fixed_amount_cents"):
            if servicio.get(campo) is not None:
                fila[campo] = _numero_limpio(servicio[campo])
    return fila


def _formato_telefono_visible(client_phone: str | None) -> str:
    """
    client_phone en el estado ya viene normalizado con indicativo de pais
    (ej. 573001234567, lo que necesita la API de WhatsApp para enviar
    mensajes) - para mostrarselo AL CLIENTE en un resumen, se le quita el
    indicativo para que se vea como el mismo lo escribio originalmente
    (ej. 3001234567), en vez de un formato que nunca tecleo.
    """
    if client_phone and client_phone.startswith("57") and len(client_phone) == 12:
        return client_phone[2:]
    return client_phone or ""


def _notificar_negocio_escalamiento(business_id: str, session_id: str, nombre_cliente: str) -> None:
    """
    Avisa al negocio que un cliente necesita que un humano siga la
    conversacion (el bot no entendio, o el cliente lo pidio
    explicitamente). Manda push (si el negocio tiene fcm_token) y un
    evento en tiempo real por el WebSocket del panel (si hay una pestaña
    conectada) — cualquiera de los dos que llegue le da al dueño el link
    a esta conversacion (business_id + session_id).
    """
    from services.conversation_tracking import marcar_conversacion

    marcar_conversacion(business_id, session_id, handed_off=True)

    business = get_business_by_id(business_id)
    fcm_token = business.get("fcm_token") if business else None
    if fcm_token:
        enviar_notificacion_escalamiento(fcm_token, nombre_cliente, business_id, session_id)

    gestor_tiempo_real.emitir(
        business_id,
        {
            "type": "chat.escalated",
            "business_id": business_id,
            "session_id": session_id,
            "client_name": nombre_cliente,
        },
    )


@tool
def registrar_telefono_cliente(
    numero_whatsapp: Annotated[str, Field(description="Numero de WhatsApp que dio el cliente, en cualquier formato (con o sin indicativo +57).")],
    tool_call_id: Annotated[str, InjectedToolCallId],
    mensajes: Annotated[list, InjectedState("messages")],
) -> Command:
    """
    Guarda el numero de WhatsApp del cliente para poder identificarlo en
    consultas, cancelaciones, reprogramaciones o para agendar una cita
    nueva, y para poder enviarle la confirmacion de la cita por WhatsApp.
    Es la UNICA forma de identificar al cliente: nunca pidas cedula ni
    ningun otro documento, ni un telefono fijo — tiene que ser un numero
    de WhatsApp. Usa esta tool apenas el cliente te de su numero, antes de
    intentar cualquier otra accion que lo requiera.
    """
    numero_normalizado = normalizar_numero_whatsapp(numero_whatsapp)

    if numero_normalizado is None:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=(
                            "Ese numero no parece un WhatsApp colombiano valido (celular de 10 digitos que empieza "
                            "en 3, con o sin el indicativo 57 adelante). Pidele al cliente que lo escriba de nuevo."
                        ),
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )

    # Guardia contra alucinaciones: el modelo llego a inventar un numero
    # (ej. 3001234567) que el cliente nunca escribio. Solo se acepta si esos
    # digitos aparecen en algun mensaje del propio cliente.
    digitos_cliente = "".join(
        c for m in mensajes if getattr(m, "type", None) == "human" for c in str(m.content) if c.isdigit()
    )
    if numero_normalizado[2:] not in digitos_cliente:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=(
                            "El cliente todavia no ha escrito ese numero. NO lo inventes ni lo deduzcas: "
                            "pidele que te escriba su numero de WhatsApp."
                        ),
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )

    return Command(
        update={
            "client_phone": numero_normalizado,
            "messages": [
                ToolMessage(
                    content=f"Numero de WhatsApp del cliente registrado: {numero_normalizado}", tool_call_id=tool_call_id
                )
            ],
        }
    )


@tool
def consultar_empleados_disponibles(
    business_id: BusinessId,
    tool_call_id: Annotated[str, InjectedToolCallId],
    fecha: Annotated[
        Optional[str],
        Field(
            description=(
                "Fecha en formato YYYY-MM-DD. Si ya sabes para que dia quiere la cita el cliente, "
                "pasala aqui: filtra la lista a solo los empleados que SI trabajan ese dia de la "
                "semana (segun su propio horario). Si todavia no sabes la fecha, omite este "
                "parametro y se listan todos los empleados activos."
            )
        ),
    ] = None,
) -> Command:
    """
    Lista los empleados activos del negocio (opcionalmente filtrados a los
    que trabajan un dia especifico) con los servicios que cada uno ofrece.
    REGLA CRITICA: siempre debes usar esta tool y preguntarle al cliente con
    quien prefiere la cita ANTES de mostrar servicios, horas disponibles, o
    agendar, salvo que el empleado ya venga fijo (enlace de chat propio de
    un empleado) o el negocio solo tenga un empleado activo.
    """
    empleados = get_employees(business_id)

    if fecha:
        try:
            fecha_dt = datetime.fromisoformat(fecha)
            dia_codigo = DIAS_MAP[fecha_dt.weekday()]
            empleados = [e for e in empleados if (obtener_horario_dia(e["id"], dia_codigo) or {}).get("is_open")]
        except ValueError:
            pass  # fecha con formato invalido: mejor listar todos que fallar la tool

    resultado = {
        "empleados": [
            {
                "id": e["id"],
                "nombre": e.get("name") or "Sin nombre",
                "servicios": [s["name"] for s in get_employee_services(e["id"])],
            }
            for e in empleados
        ]
    }

    return Command(
        update={
            # Opciones de seleccion rapida para el widget de chat: el cliente
            # puede tocar un boton en vez de escribir el nombre del empleado.
            "ultimas_opciones": [
                {"label": e["nombre"], "value": e["nombre"]} for e in resultado["empleados"]
            ],
            "messages": [
                ToolMessage(
                    content=_json_compacto(resultado), name="consultar_empleados_disponibles", tool_call_id=tool_call_id
                )
            ],
        }
    )


@tool
def seleccionar_empleado(
    employee_id: Annotated[str, Field(description="El id del empleado elegido (de consultar_empleados_disponibles).")],
    business_id: BusinessId,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """
    Guarda con que empleado va a ser la cita, despues de que el cliente lo
    elija (o cuando solo hay un empleado y lo eliges automaticamente sin
    preguntar). A partir de aqui, servicios y horas disponibles se
    consultan para ese empleado especifico.
    """
    empleado = get_employee_by_id(employee_id)
    if not empleado or empleado["business_id"] != business_id or not empleado.get("active"):
        return Command(
            update={
                "messages": [
                    ToolMessage(content="Ese empleado no existe o ya no esta disponible.", tool_call_id=tool_call_id)
                ]
            }
        )

    return Command(
        update={
            "employee_id": employee_id,
            "messages": [
                ToolMessage(
                    content=f"Empleado seleccionado: {empleado.get('name') or employee_id}",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )


@tool
def consultar_servicios_disponibles(
    business_id: BusinessId,
    employee_id: EmployeeId,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """
    Consulta los servicios activos que ofrece el negocio (o, si ya se
    selecciono un empleado, solo los que ESE empleado ofrece), con precio
    y duracion.
    """
    servicios = get_employee_services(employee_id) if employee_id else get_services(business_id)
    domicilios_activos = _domicilios_activos(business_id)
    resultado = {"servicios": [_servicio_para_modelo(s, domicilios_activos) for s in servicios]}

    return Command(
        update={
            # Opciones de seleccion rapida: el cliente toca el servicio en vez
            # de escribir su nombre exacto.
            "ultimas_opciones": [
                {
                    "label": f"{s['name']} · {s['duration_minutes']} min · {_formato_precio_cop(s['price'])}",
                    "value": s["name"],
                }
                for s in servicios
            ],
            "messages": [
                ToolMessage(
                    content=_json_compacto(resultado), name="consultar_servicios_disponibles", tool_call_id=tool_call_id
                )
            ],
        }
    )


@tool
def seleccionar_servicio(
    service_id: Annotated[str, Field(description="El id del servicio elegido (de consultar_servicios_disponibles).")],
    business_id: BusinessId,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """
    Confirma con que servicio va a ser la cita, en cuanto identifiques
    cual quiere el cliente (por nombre exacto o boton de seleccion
    rapida). Llamala INMEDIATAMENTE despues de resolver el service_id
    (en el mismo turno en que llames consultar_servicios_disponibles si
    hace falta para encontrarlo) — evita que se le vuelva a mostrar al
    cliente la lista completa de servicios como si tuviera que elegir de
    nuevo, aunque despues vuelvas a consultar el precio/duracion.
    """
    servicios = get_services(business_id)
    servicio = next((s for s in servicios if s["id"] == service_id), None)
    if not servicio or not servicio.get("active"):
        return Command(
            update={
                "messages": [
                    ToolMessage(content="Ese servicio no existe o ya no esta activo.", tool_call_id=tool_call_id)
                ]
            }
        )

    aviso_domicilio = (
        " Este servicio SI ofrece domicilio (offers_home_visit=true). ANTES de consultar horas "
        "disponibles o pedir el dia, pregúntale EXPLICITAMENTE al cliente si prefiere la cita en el "
        "local o a domicilio — no asumas el local por defecto ni sigas el flujo sin preguntar esto."
        if servicio.get("offers_home_visit")
        else ""
    )

    return Command(
        update={
            "service_id": service_id,
            "messages": [
                ToolMessage(
                    content=(
                        f"Servicio seleccionado: {servicio['name']} "
                        f"({servicio['duration_minutes']} min, {_formato_precio_cop(servicio['price'])})."
                        f"{aviso_domicilio}"
                    ),
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )


@tool
def consultar_horas_disponibles(
    fecha: Annotated[str, Field(description="Fecha en formato YYYY-MM-DD (la hora se ignora, usa 00:00:00).")],
    business_id: BusinessId,
    employee_id: EmployeeId,
    tool_call_id: Annotated[str, InjectedToolCallId],
    mensajes: Annotated[list, InjectedState("messages")],
    servicio_nombre: Annotated[
        Optional[str], Field(description="Nombre del servicio, para calcular la duracion correcta. Opcional.")
    ] = None,
) -> Command:
    """
    Consulta las horas realmente disponibles (libres) de un dia especifico
    PARA EL EMPLEADO YA SELECCIONADO, considerando su horario de trabajo,
    su almuerzo y sus citas ya agendadas (la agenda de cada empleado es
    independiente). Usa esto cuando el cliente pregunte que horas hay
    disponibles, o antes de sugerir una hora para agendar. Si todavia no
    hay un empleado seleccionado, esta tool devuelve un error: primero usa
    consultar_empleados_disponibles y seleccionar_empleado.
    """
    if not employee_id:
        error = {"error": "Primero hay que saber con que empleado es la cita. Usa consultar_empleados_disponibles y seleccionar_empleado."}
        return Command(update={"messages": [ToolMessage(content=str(error), tool_call_id=tool_call_id)]})

    if not _cliente_dio_fecha(mensajes):
        return Command(update={"messages": [ToolMessage(content=MENSAJE_FALTA_FECHA, tool_call_id=tool_call_id)]})

    duracion = 30
    if servicio_nombre:
        servicios = get_employee_services(employee_id)
        servicio = next((s for s in servicios if s["name"].lower() == servicio_nombre.lower()), None)
        if servicio:
            duracion = servicio.get("duration_minutes", 30)

    fecha_iso = f"{fecha}T00:00:00"
    fecha_dt = datetime.fromisoformat(fecha_iso)

    if fecha_dt.date() < ahora_local().date():
        error = {"error": "Esa fecha ya paso. Elige una fecha de hoy en adelante."}
        return Command(update={"messages": [ToolMessage(content=str(error), tool_call_id=tool_call_id)]})

    dias_map = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    dia_codigo = dias_map[fecha_dt.weekday()]
    horario_dia = obtener_horario_dia(employee_id, dia_codigo)
    negocio_abierto_ese_dia = bool(horario_dia and horario_dia.get("is_open"))

    horas_libres = generar_horas_disponibles(business_id, employee_id, fecha_iso, duracion)
    resultado = {
        "horas_disponibles": horas_libres,
        # Equivalencia am/pm -> 24h de CADA hora libre: evita que el modelo tenga que
        # convertir "12:30 am" (00:30) y la confunda con 12:30 (mediodia, almuerzo).
        "equivalencias_am_pm_a_24h": {_formato_hora_12h(h): h for h in horas_libres},
        # TODAS las horas libres ya agrupadas en Mañana/Tarde: el modelo las muestra
        # tal cual (las arma el codigo, para que no omita ni invente ninguna).
        "horas_por_franja": _horas_por_franja(horas_libres),
        # La IA necesita esto para poder explicarle al cliente POR QUE no hay
        # horas (el empleado no trabaja ese dia vs. simplemente todo ocupado),
        # en vez de adivinar o saltar de dia en silencio.
        "negocio_abierto_ese_dia": negocio_abierto_ese_dia,
        "dia_semana": DIAS_SEMANA_ES[dia_codigo],
    }

    return Command(
        update={
            # Opciones de seleccion rapida: el cliente toca la hora en vez de
            # escribirla. Se ofrecen todas las horas libres, las mismas que
            # lista el texto del mensaje.
            "ultimas_opciones": [
                {"label": _formato_hora_12h(h), "value": _formato_hora_12h(h)} for h in horas_libres
            ],
            "messages": [
                ToolMessage(
                    content=_json_compacto(resultado), name="consultar_horas_disponibles", tool_call_id=tool_call_id
                )
            ],
        }
    )


@tool
def pedir_confirmacion_cita(
    servicio_nombre: Annotated[str, Field(description="Nombre exacto del servicio a agendar.")],
    fecha_hora: Annotated[str, Field(description="Fecha y hora en formato ISO 8601, ej: 2026-06-22T15:00:00")],
    nombre_cliente: Annotated[str, Field(description="Nombre del cliente.")],
    business_id: BusinessId,
    employee_id: EmployeeId,
    client_phone: ClientPhone,
    session_id: SessionId,
    tool_call_id: Annotated[str, InjectedToolCallId],
    mensajes: Annotated[list, InjectedState("messages")],
    direccion: Annotated[
        Optional[str],
        Field(description="Direccion del cliente SOLO si la cita es a domicilio (el cliente la escribio). Omitela si es en el local."),
    ] = None,
    zona: Annotated[
        Optional[str],
        Field(description="Municipio/zona del domicilio (uno de los que devuelve consultar_zonas_domicilio). Solo si es a domicilio."),
    ] = None,
) -> Command:
    """
    Llama esta tool SIEMPRE que vayas a pedirle confirmacion al cliente
    antes de agendar (la primera vez, y de nuevo cada vez que el cliente
    corrija algun dato) - arma el resumen formateado de la cita Y le
    ofrece los botones de seleccion rapida Si/No, en un solo paso. NUNCA
    escribas tu mismo el resumen de confirmacion: esta tool es la UNICA
    forma de mostrarlo, relaya su resultado tal cual en tu respuesta (no
    lo repitas ni lo reformules). No llames crear_cita en este mismo
    turno — eso solo pasa despues de que el cliente confirme, en un
    mensaje aparte.
    """
    if not client_phone:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=(
                            "Falta el numero de WhatsApp del cliente y es OBLIGATORIO para agendar (a domicilio o en el local). "
                            "Pidelo y usa registrar_telefono_cliente antes de pedir la confirmacion."
                        ),
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )

    if not _cliente_dio_fecha(mensajes):
        return Command(update={"messages": [ToolMessage(content=MENSAJE_FALTA_FECHA, tool_call_id=tool_call_id)]})

    servicios = get_employee_services(employee_id) if employee_id else []
    servicio = next((s for s in servicios if s["name"].lower() == servicio_nombre.lower()), None)
    precio_texto = f" · {_formato_precio_cop(servicio['price'])}" if servicio else ""

    empleados_negocio = get_employees(business_id)
    empleado = get_employee_by_id(employee_id) if employee_id else None
    mostrar_empleado = bool(empleado) and len(empleados_negocio) > 1

    error_domicilio = _validar_domicilio(business_id, servicio, direccion, mensajes)
    zona_elegida = None
    if not error_domicilio:
        zona_elegida, error_domicilio = _resolver_zona(business_id, direccion, zona, mensajes, session_id, client_phone)
    if error_domicilio:
        return Command(update={"messages": [ToolMessage(content=error_domicilio, tool_call_id=tool_call_id)]})

    try:
        fecha_hora_dt = datetime.fromisoformat(fecha_hora)
        fecha_texto = formatear_fecha_natural(fecha_hora_dt)
    except ValueError:
        fecha_texto = fecha_hora
        fecha_hora_dt = None

    # Validacion determinista ANTES de pedir confirmacion: asi la unica fuente de
    # "esa hora no esta disponible" es el sistema, no el criterio del modelo.
    if fecha_hora_dt is not None and employee_id:
        duracion_cita = (servicio or {}).get("duration_minutes", 30)
        es_valida, mensaje_error = es_hora_valida(fecha_hora, employee_id, duracion_cita)
        if es_valida:
            hay_choque, mensaje_error = hay_choque_de_horario(business_id, employee_id, fecha_hora_dt, duracion_cita)
            es_valida = not hay_choque
        if not es_valida:
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            content=f"No se puede agendar esa hora: {mensaje_error} Ofrecele otras horas consultando consultar_horas_disponibles.",
                            tool_call_id=tool_call_id,
                        )
                    ]
                }
            )

    lineas = [
        "¡Perfecto! Confírmame estos datos por favor 📋",
        f"💇 Servicio: *{servicio_nombre}*{precio_texto}",
    ]
    if mostrar_empleado:
        lineas.append(f"🧑 Con: *{empleado.get('name') or 'el equipo'}*")
    lineas.append(f"📅 Cuándo: *{fecha_texto}*")
    if direccion and direccion.strip():
        lineas.append(f"🏠 Domicilio en: *{direccion.strip()}*")
        if zona_elegida and float(zona_elegida["fee"]) > 0:
            recargo = float(zona_elegida["fee"])
            total = float(servicio["price"]) + recargo if servicio else recargo
            lineas.append(f"🚚 Recargo por domicilio ({zona_elegida['name']}): *+{_formato_precio_cop(recargo)}*")
            lineas.append(f"💰 Total: *{_formato_precio_cop(total)}*")
    lineas.append(f"👤 Nombre: *{nombre_cliente}*")
    if client_phone:
        lineas.append(f"📱 Número: *{_formato_telefono_visible(client_phone)}*")
    lineas.append("¿Confirmo tu cita?")

    return Command(
        update={
            "ultimas_opciones": [
                {"label": "Si, confirmar", "value": "Si"},
                {"label": "No, cambiar algo", "value": "No"},
            ],
            "messages": [
                ToolMessage(content="\n".join(lineas), name="pedir_confirmacion_cita", tool_call_id=tool_call_id)
            ],
        }
    )


@tool
def crear_cita(
    servicio_nombre: Annotated[str, Field(description="Nombre exacto del servicio a agendar.")],
    fecha_hora: Annotated[str, Field(description="Fecha y hora en formato ISO 8601, ej: 2026-06-22T15:00:00")],
    nombre_cliente: Annotated[str, Field(description="Nombre del cliente.")],
    business_id: BusinessId,
    client_phone: ClientPhone,
    employee_id: EmployeeId,
    employee_fijo: EmployeeFijo,
    session_id: SessionId,
    mensajes: Annotated[list, InjectedState("messages")],
    direccion: Annotated[
        Optional[str],
        Field(description="Direccion del cliente SOLO si la cita es a domicilio (la misma que se confirmo). Omitela si es en el local."),
    ] = None,
    zona: Annotated[
        Optional[str],
        Field(description="Municipio/zona del domicilio (uno de los que devuelve consultar_zonas_domicilio). Solo si es a domicilio."),
    ] = None,
) -> dict:
    """
    Agenda la cita para el cliente, validando horario y disponibilidad del
    empleado seleccionado. Si el servicio requiere abono
    (payment_type/requires_payment del servicio), la cita AUN NO se crea:
    se genera un link de pago de Wompi y la cita se agenda automaticamente
    (sin que el negocio tenga que hacer nada) solo cuando Wompi confirme
    el pago - el resultado trae pago_pendiente=True en vez de cita_creada
    en ese caso. Revisa el campo pago_pendiente en el resultado para saber
    cual de los dos casos ocurrio.
    """
    if not client_phone:
        return {"error": "Antes de agendar necesito el numero de WhatsApp del cliente. Pidelo y usa registrar_telefono_cliente."}
    if not employee_id:
        return {"error": "Antes de agendar necesito saber con que empleado es la cita. Usa consultar_empleados_disponibles y seleccionar_empleado."}

    servicios = get_employee_services(employee_id)
    servicio = next((s for s in servicios if s["name"].lower() == servicio_nombre.lower()), None)
    if not servicio:
        return {"error": f"No encontre el servicio '{servicio_nombre}' para ese empleado. Servicios disponibles: {[s['name'] for s in servicios]}"}

    duracion = servicio.get("duration_minutes", 30)

    error_domicilio = _validar_domicilio(business_id, servicio, direccion, mensajes)
    zona_elegida = None
    if not error_domicilio:
        zona_elegida, error_domicilio = _resolver_zona(business_id, direccion, zona, mensajes, session_id, client_phone)
    if error_domicilio:
        return {"error": error_domicilio}

    es_valida, mensaje_error = es_hora_valida(fecha_hora, employee_id, duracion)
    if not es_valida:
        return {"error": mensaje_error}

    fecha_hora_dt = datetime.fromisoformat(fecha_hora)

    hay_choque, mensaje_choque = hay_choque_de_horario(business_id, employee_id, fecha_hora_dt, duracion)
    if hay_choque:
        return {"error": mensaje_choque}

    direccion_limpia = direccion.strip() if direccion and direccion.strip() else None
    zona_nombre = zona_elegida["name"] if zona_elegida else None
    zona_fee = float(zona_elegida["fee"]) if zona_elegida else 0

    if servicio.get("requires_payment"):
        # Se agenda de inmediato como "pending_payment": bloquea el cupo
        # desde ya (cuenta para el choque de horario y para el indice
        # unico de appointments_pending_payment.sql), aunque todavia no es
        # una cita confirmada. Pasa a "confirmed" solo cuando Wompi
        # apruebe el pago (ver confirmar_cita_desde_pago en
        # services/wompi_payment_requests.py); si el link vence sin pagar,
        # se cancela y libera el cupo (expirar_solicitudes_vencidas).
        from services.appointment_confirmation import crear_cita_pendiente_pago
        from services.wompi_payment_requests import crear_solicitud_pago

        cita_pendiente = crear_cita_pendiente_pago(
            business_id=business_id,
            client_phone=client_phone,
            client_name=nombre_cliente,
            service_id=servicio["id"],
            scheduled_at=fecha_hora,
            employee_id=employee_id,
            address=direccion_limpia,
            home_visit_zone=zona_nombre,
            home_visit_fee=zona_fee,
        )

        if not cita_pendiente:
            # El indice unico rechazo el insert: alguien mas tomo ese
            # horario en el mismo instante (carrera real de concurrencia).
            return {
                "error": "Esa hora se acaba de ocupar (otro cliente la tomo justo ahora). Ofrecele consultar_horas_disponibles de nuevo para elegir otra."
            }

        try:
            solicitud = crear_solicitud_pago(
                business_id=business_id,
                servicio=servicio,
                session_id=session_id,
                client_phone=client_phone,
                appointment_id=cita_pendiente["id"],
                # Solo si el chat era el enlace propio de un empleado: el
                # localStorage del widget guarda el session_id bajo una
                # clave con employee_id SOLO en ese caso (ver
                # chat.page.ts), asi que enviar el employee_id cuando el
                # cliente esta en el chat general haria que el redirect lo
                # mande a una URL donde el widget no encuentra su sesion.
                employee_id=employee_id if employee_fijo else None,
                expira_en_horas=1.0,
            )
        except Exception as e:
            print(f"No se pudo generar el link de pago de Wompi para la cita {cita_pendiente['id']}: {e}")
            solicitud = None

        if not solicitud:
            # No se pudo generar el link: no dejar el cupo bloqueado sin
            # forma de pagarlo, se libera de inmediato.
            from services.db import update_appointment_status

            update_appointment_status(cita_pendiente["id"], "cancelled", actor="system")
            return {
                "error": (
                    "Este servicio requiere un abono para agendar, pero no fue posible generar el link de pago "
                    "en este momento (problema tecnico o el negocio no tiene Wompi configurado). Explicaselo al "
                    "cliente y ofrece transferir_a_equipo si insiste en agendar ahora."
                )
            }

        from services.conversation_tracking import marcar_conversacion

        # El cupo ya esta reservado (aunque falte el pago): la conversacion
        # termino en cita. Solo se marca si el link de pago si se genero.
        marcar_conversacion(business_id, session_id, ended_in_appointment=True, appointment_id=cita_pendiente["id"])

        return {
            "pago_pendiente": True,
            "link_pago": solicitud["checkout_url"],
            "monto_abono_cents": solicitud["amount_in_cents"],
            "descripcion_abono": solicitud["description"],
        }

    from services.appointment_confirmation import finalizar_creacion_cita

    cita = finalizar_creacion_cita(
        business_id=business_id,
        client_phone=client_phone,
        client_name=nombre_cliente,
        service_id=servicio["id"],
        service_name=servicio["name"],
        scheduled_at=fecha_hora,
        employee_id=employee_id,
        address=direccion_limpia,
        home_visit_zone=zona_nombre,
        home_visit_fee=zona_fee,
    )

    if cita:
        from services.conversation_tracking import marcar_conversacion

        marcar_conversacion(business_id, session_id, ended_in_appointment=True, appointment_id=cita.get("id"))

    return {"cita_creada": [cita] if cita else []}


# El cliente pidio ver, consultar, cancelar o mover una cita que YA tiene. Mensajes sueltos
# como "turno", "cita" o "quiero una cita" son intencion de AGENDAR una nueva: no cuentan.
_PATRON_PIDIO_SUS_CITAS = re.compile(
    r"\b(?:cancel\w*|reprogram\w*|reagend\w*|mover|moverla|cambiar|cambio|consult\w*|"
    r"mis? (?:cita|citas|turno|turnos|reserva|reservas|agenda)|"
    r"(?:mi|la|las|esa|esas) (?:ultima|proxima|anterior|siguiente) (?:cita|reserva|turno)|"
    r"(?:ultima|proxima|anterior) (?:cita|reserva|turno)|"
    r"(?:tengo|tenia|tuve) (?:una |alguna |agendad\w+ |algun )?(?:cita|citas|turno|reserva)|"
    r"ya tengo|agendada|reservada|"
    r"cuando (?:es|fue|era|tengo)|a que hora (?:es|era|tengo|fue)|"
    r"que citas|cuales citas|ver (?:mi|mis|la|las)|revisar (?:mi|mis|la|las)|"
    r"la del|el del|esa cita|esa reserva)\b"
)

# Cuantos mensajes recientes del cliente se miran: a una pregunta del bot ("¿cual cita?")
# el cliente suele responder sin repetir la palabra ("la del viernes").
_MENSAJES_RECIENTES_A_REVISAR = 4

MENSAJE_NO_PIDIO_SUS_CITAS = (
    "El cliente NO ha pedido ver, consultar, cancelar ni mover una cita que ya tiene. No menciones citas anteriores "
    "ni sus fechas u horas. Si escribio algo suelto como \"turno\" o \"cita\", asume que quiere AGENDAR una nueva: "
    "preguntale para que dia o con que servicio. Si de verdad no queda claro, preguntale: \"¿Quieres agendar una "
    "nueva cita o consultar una que ya tienes?\""
)


def _cliente_pidio_sus_citas(mensajes: list | None) -> bool:
    """
    True si en los ultimos mensajes del cliente hay una peticion explicita sobre una cita
    que ya tiene. `mensajes=None` (la tool llamada sin estado) no bloquea nada.
    """
    if mensajes is None:
        return True
    humanos = [str(m.content) for m in mensajes if getattr(m, "type", None) == "human"]
    texto = _normalizar(" ".join(humanos[-_MENSAJES_RECIENTES_A_REVISAR:]))
    return bool(_PATRON_PIDIO_SUS_CITAS.search(texto))


@tool
def consultar_citas_cliente(
    business_id: BusinessId,
    client_phone: ClientPhone,
    mensajes: Annotated[list, InjectedState("messages")] = None,
) -> dict:
    """Consulta las citas confirmadas del cliente actual. Solo cuando el cliente pide ver, cancelar o mover una cita que ya tiene."""
    if not client_phone:
        return {"error": "Necesito el numero de WhatsApp del cliente para buscar sus citas. Pidelo y usa registrar_telefono_cliente."}
    if not _cliente_pidio_sus_citas(mensajes):
        # Privacidad: sin una peticion explicita no se leen ni se mencionan citas anteriores.
        return {"citas_no_consultadas": True, "instruccion": MENSAJE_NO_PIDIO_SUS_CITAS}
    return {"citas": get_client_appointments(business_id, client_phone)}


@tool
def cancelar_cita(
    appointment_id: Annotated[str, Field(description="ID de la cita a cancelar.")],
    business_id: BusinessId,
    client_phone: ClientPhone,
    session_id: SessionId,
    confirmar_a_pesar_del_pago: Annotated[
        bool,
        Field(
            description="Dejala en False la primera vez. Si la cita tiene un abono/pago ya realizado, esta tool "
            "no cancela nada la primera vez y en cambio te pide confirmar con el cliente. Solo si el cliente, "
            "viendo esa advertencia, confirma explicitamente que aun asi quiere cancelar, vuelve a llamar esta "
            "misma tool con este parametro en True para ejecutar la cancelacion real."
        ),
    ] = False,
) -> dict:
    """
    Cancela una cita existente del cliente. Si el servicio de esa cita
    requiere abono (ya fue pagado, porque una cita solo llega a estado
    confirmada pasando por el pago), esta tool NO cancela de inmediato:
    devuelve una advertencia que debes mostrarle al cliente tal cual,
    explicando que ya hizo un pago y que la cancelacion podria requerir un
    proceso de devolucion, y preguntandole si de verdad quiere continuar.
    Solo cancela la cita real cuando el cliente confirme explicitamente
    despues de leer esa advertencia (segundo llamado, confirmar_a_pesar_del_pago=True).
    """
    if not client_phone:
        return {"error": "Necesito el numero de WhatsApp del cliente para poder cancelar. Pidelo y usa registrar_telefono_cliente."}

    cita = get_appointment_by_id_and_phone(appointment_id, client_phone)
    if not cita:
        return {"error": "No encontre esa cita para este cliente."}

    servicio_info = cita.get("services") or {}
    tiene_pago = bool(servicio_info.get("requires_payment"))

    if tiene_pago and not confirmar_a_pesar_del_pago:
        return {
            "requiere_confirmacion_por_pago": True,
            "advertencia": (
                "⚠️ Esta cita tiene un pago/abono ya realizado. Cancelarla puede requerir un proceso de "
                "devolucion. ¿Estas seguro de que quieres cancelarla de todos modos?"
            ),
        }

    from services.db import cancel_appointment

    resultado = cancel_appointment(appointment_id, client_phone)

    if tiene_pago:
        # Cancelar una cita ya pagada siempre se avisa al negocio (no en
        # silencio como escalar_por_confusion): puede necesitar coordinar
        # una devolucion con el cliente, no es algo que el bot pueda
        # resolver solo.
        _notificar_negocio_escalamiento(business_id, session_id, cita.get("client_name") or client_phone)

    try:
        from services.push_notifications import enviar_notificacion_cita_cancelada
        from services.scheduling import formatear_fecha_natural
        from services.db import get_business_by_id

        business = get_business_by_id(business_id)
        fecha_hora_dt = datetime.fromisoformat(cita["scheduled_at"])
        nombre_servicio = servicio_info.get("name", "su cita")
        enviar_notificacion_cita_cancelada(
            fcm_token=business.get("fcm_token") if business else None,
            nombre_cliente=cita.get("client_name") or "Cliente",
            servicio=nombre_servicio,
            fecha_hora_texto=formatear_fecha_natural(fecha_hora_dt),
        )
    except Exception as e:
        print(f"No se pudo enviar la notificacion de cancelacion: {e}")

    try:
        from services.db import get_business_by_id as _negocio
        from services.notificaciones_cliente import datos_para_plantilla
        from services.whatsapp import enviar_cancelacion_cita_cliente

        negocio_cliente = _negocio(business_id)
        enviar_cancelacion_cita_cliente(
            client_phone=client_phone,
            nombre_cliente=cita.get("client_name") or "Cliente",
            nombre_negocio=(negocio_cliente or {}).get("name", "el negocio"),
            nombre_servicio=servicio_info.get("name", "tu cita"),
            datos_plantilla=datos_para_plantilla(negocio_cliente, datetime.fromisoformat(cita["scheduled_at"])),
        )
    except Exception as e:
        print(f"No se pudo avisar la cancelacion al cliente por WhatsApp: {e}")

    from services.realtime import emitir_evento_cita

    emitir_evento_cita("appointment.cancelled", business_id, appointment_id)

    return {"resultado": resultado}


@tool
def reprogramar_cita(
    appointment_id: Annotated[str, Field(description="ID de la cita a reprogramar.")],
    business_id: BusinessId,
    client_phone: ClientPhone,
    session_id: SessionId,
    mensajes: Annotated[list, InjectedState("messages")],
    nueva_fecha_hora: Annotated[
        Optional[str],
        Field(
            description="Nueva fecha y hora en formato ISO 8601, ej: 2026-07-01T17:00:00. "
            "Omitela si el cliente SOLO quiere cambiar entre local y domicilio sin cambiar la hora."
        ),
    ] = None,
    direccion: Annotated[
        Optional[str],
        Field(description="Direccion nueva SI el cliente quiere que la cita pase a ser (o siga siendo) a domicilio. NUNCA la inventes."),
    ] = None,
    zona: Annotated[
        Optional[str],
        Field(description="Municipio/zona del domicilio (uno de los que devuelve consultar_zonas_domicilio). Solo junto con direccion."),
    ] = None,
    volver_al_local: Annotated[
        bool,
        Field(description="True SOLO si una cita que estaba a domicilio debe volver a ser en el local (quita la direccion). No la actives si la cita ya es en el local."),
    ] = False,
) -> dict:
    """
    Cambia la fecha/hora y/o la modalidad (local <-> domicilio) de una cita
    existente del cliente, EN LA MISMA cita - NUNCA crea una cita nueva ni
    para esto ni cuando el cliente pide "cambiar la de local a domicilio"
    (eso tambien es reprogramar, no agendar de nuevo). Usa esta tool cuando
    el cliente pida mover, cambiar de hora, o cambiar la modalidad de una
    cita que ya tiene. Puedes pasar solo nueva_fecha_hora (misma modalidad),
    solo direccion+zona o volver_al_local (misma hora, cambia modalidad), o
    ambos a la vez. Primero usa consultar_citas_cliente si no sabes el
    appointment_id.
    """
    if not client_phone:
        return {"error": "Necesito el numero de WhatsApp del cliente para reprogramar. Pidelo y usa registrar_telefono_cliente."}

    cita_actual = get_appointment_by_id_and_phone(appointment_id, client_phone)
    if not cita_actual:
        return {"error": "No encontre esa cita para este cliente."}

    empleado_id_cita = cita_actual["employee_id"]
    fecha_hora_anterior = datetime.fromisoformat(cita_actual["scheduled_at"])
    duracion = cita_actual.get("services", {}).get("duration_minutes", 30) if cita_actual.get("services") else 30
    nombre_servicio = cita_actual.get("services", {}).get("name", "tu cita") if cita_actual.get("services") else "tu cita"

    # La modalidad (local/domicilio) solo se toca si el cliente pidio
    # cambiarla explicitamente (direccion nueva, o volver_al_local); si no
    # dijo nada de eso, la cita se reprograma tal cual estaba.
    cambia_modalidad = bool((direccion and direccion.strip()) or volver_al_local)
    is_home_visit = None
    direccion_final = None
    zona_nombre = None
    zona_fee = 0.0

    if cambia_modalidad and not volver_al_local:
        servicios_empleado = get_employee_services(empleado_id_cita)
        servicio = next((s for s in servicios_empleado if s["id"] == cita_actual.get("service_id")), None)

        error_domicilio = _validar_domicilio(business_id, servicio, direccion, mensajes)
        if error_domicilio:
            return {"error": error_domicilio}

        zona_elegida, error_zona = _resolver_zona(business_id, direccion, zona, mensajes, session_id, client_phone)
        if error_zona:
            return {"error": error_zona}

        is_home_visit = True
        direccion_final = direccion.strip()
        zona_nombre = zona_elegida["name"] if zona_elegida else None
        zona_fee = float(zona_elegida["fee"]) if zona_elegida else 0.0
    elif volver_al_local:
        is_home_visit = False

    nueva_fecha_hora_final = nueva_fecha_hora or cita_actual["scheduled_at"]

    es_valida, mensaje_error = es_hora_valida(nueva_fecha_hora_final, empleado_id_cita, duracion)
    if not es_valida:
        return {"error": mensaje_error}

    nueva_fecha_hora_dt = datetime.fromisoformat(nueva_fecha_hora_final)

    hay_choque, mensaje_choque = hay_choque_de_horario(
        business_id, empleado_id_cita, nueva_fecha_hora_dt, duracion, ignorar_appointment_id=appointment_id
    )
    if hay_choque:
        return {"error": mensaje_choque}

    update_appointment_schedule(
        appointment_id,
        nueva_fecha_hora_final,
        is_home_visit=is_home_visit,
        address=direccion_final,
        home_visit_zone=zona_nombre,
        home_visit_fee=zona_fee,
    )

    try:
        from services.push_notifications import enviar_notificacion_cita_reprogramada
        from services.scheduling import formatear_fecha_natural
        from services.db import get_business_by_id

        business = get_business_by_id(business_id)
        enviar_notificacion_cita_reprogramada(
            fcm_token=business.get("fcm_token") if business else None,
            nombre_cliente=cita_actual.get("client_name") or "Cliente",
            servicio=nombre_servicio,
            fecha_anterior_texto=formatear_fecha_natural(fecha_hora_anterior),
            fecha_nueva_texto=formatear_fecha_natural(nueva_fecha_hora_dt),
        )
    except Exception as e:
        print(f"No se pudo enviar la notificacion de reprogramacion: {e}")

    try:
        from services.db import get_business_by_id as _negocio
        from services.notificaciones_cliente import datos_para_plantilla
        from services.whatsapp import enviar_reprogramacion_cita_cliente

        business_cliente = _negocio(business_id)
        enviar_reprogramacion_cita_cliente(
            client_phone=client_phone,
            nombre_cliente=cita_actual.get("client_name") or "Cliente",
            nombre_negocio=(business_cliente or {}).get("name", "el negocio"),
            datos_plantilla=datos_para_plantilla(business_cliente, nueva_fecha_hora_dt),
        )
    except Exception as e:
        print(f"No se pudo avisar la reprogramacion al cliente por WhatsApp: {e}")

    from services.realtime import emitir_evento_cita

    emitir_evento_cita("appointment.updated", business_id, appointment_id)

    resultado = {"cita_reprogramada": True, "nueva_fecha_hora": nueva_fecha_hora_final}
    if cambia_modalidad:
        resultado["es_domicilio_ahora"] = bool(is_home_visit)
        if is_home_visit:
            resultado["direccion"] = direccion_final
            resultado["zona"] = zona_nombre
            resultado["recargo_domicilio_cents"] = int(zona_fee * 100)
    return resultado


@tool
def transferir_a_equipo(
    business_id: BusinessId,
    session_id: SessionId,
    client_phone: ClientPhone,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """
    Usa esta tool INMEDIATAMENTE cuando el cliente pida explicitamente
    hablar con una persona real, con el equipo, con el dueño del negocio, o
    con alguien por su nombre propio, en cualquier momento de la
    conversacion (no solo al inicio). Esto detiene al bot para que un
    humano tome la conversacion.
    """
    _notificar_negocio_escalamiento(business_id, session_id, client_phone or "Un cliente")

    return Command(
        update={
            "transferido": True,
            "messages": [
                ToolMessage(content="Cliente transferido a un humano del equipo.", tool_call_id=tool_call_id)
            ],
        }
    )


@tool
def escalar_por_confusion(
    business_id: BusinessId,
    session_id: SessionId,
    client_phone: ClientPhone,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """
    Usa esta tool cuando, despues de intentar aclarar la solicitud del
    cliente al menos una vez con una pregunta directa, sigues sin entender
    que necesita, o cuando ninguna de tus otras herramientas te permite
    ayudarlo con lo que pide. Avisa al negocio para que un humano siga la
    conversacion (el cliente NUNCA se entera de este aviso: no le
    menciones que vas a notificar a nadie ni le ofrezcas hablar con una
    persona). NO la uses en la primera pregunta ambigua (primero intenta
    aclarar tu mismo), ni la repitas si el estado de la conversacion
    indica que ya se notifico antes, a menos que el cliente pida
    explicitamente hablar con alguien (en ese caso usa transferir_a_equipo).
    """
    _notificar_negocio_escalamiento(business_id, session_id, client_phone or "Un cliente")

    mensaje = "Dejame confirmar eso con el equipo, en un momento seguimos por aqui mismo 🙏"

    return Command(
        update={
            "escalado": True,
            "messages": [ToolMessage(content=mensaje, tool_call_id=tool_call_id)],
        }
    )


@tool
def consultar_zonas_domicilio(
    business_id: BusinessId,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """
    Consulta en que municipios/zonas el negocio hace domicilios y el recargo
    de cada una. Usala cuando el cliente pregunte donde llegan o cuanto
    cuesta el domicilio, o antes de pedirle la direccion. Si la lista esta
    vacia, el negocio no restringe zonas ni cobra recargo.
    """
    if not _domicilios_activos(business_id):
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content="Este negocio no hace domicilios: todas las citas son en el local.",
                        name="consultar_zonas_domicilio",
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )

    zonas = get_home_visit_zones(business_id)
    resultado = {
        "zonas": [{"nombre": z["name"], "recargo": _formato_precio_cop(z["fee"])} for z in zonas],
        "sin_restriccion": not zonas,
    }
    return Command(
        update={
            "messages": [
                ToolMessage(content=_json_compacto(resultado), name="consultar_zonas_domicilio", tool_call_id=tool_call_id)
            ]
        }
    )


TOOLS = [
    registrar_telefono_cliente,
    consultar_empleados_disponibles,
    seleccionar_empleado,
    consultar_servicios_disponibles,
    seleccionar_servicio,
    consultar_horas_disponibles,
    consultar_zonas_domicilio,
    pedir_confirmacion_cita,
    crear_cita,
    consultar_citas_cliente,
    cancelar_cita,
    reprogramar_cita,
    transferir_a_equipo,
    escalar_por_confusion,
]
