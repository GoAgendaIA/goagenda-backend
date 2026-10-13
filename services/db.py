import os
from datetime import datetime, timedelta, timezone
import httpx
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

SUPABASE_SERVER_KEY = SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY

if not SUPABASE_URL or not SUPABASE_SERVER_KEY:
    raise RuntimeError(
        "Faltan variables de Supabase: define SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY (o SUPABASE_KEY)."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVER_KEY)


class _TransporteConReintento(httpx.HTTPTransport):
    """
    Supabase/Cloudflare cierra conexiones keep-alive ociosas; si el cliente
    reutiliza una ya cerrada falla con "Server disconnected"
    (httpx.RemoteProtocolError). Se reintenta UNA vez, solo metodos
    idempotentes, para no duplicar inserts (POST).
    """

    _IDEMPOTENTES = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return super().handle_request(request)
        except (httpx.RemoteProtocolError, httpx.ReadError):
            if request.method not in self._IDEMPOTENTES:
                raise
            return super().handle_request(request)


def _endurecer_cliente_postgrest(cliente: Client) -> None:
    """
    Reemplaza la sesion HTTP de PostgREST: keep-alive corto (evita reutilizar
    conexiones ya cerradas por el servidor), timeout de 20s en vez de los
    120s por defecto (una consulta colgada bloqueaba el worker de recordatorios
    minutos) y el reintento de arriba.
    """
    anterior = cliente.postgrest.session
    cliente.postgrest.session = httpx.Client(
        base_url=anterior.base_url,
        headers=anterior.headers,
        timeout=httpx.Timeout(20.0, connect=10.0),
        limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=5.0),
        transport=_TransporteConReintento(limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=5.0)),
        follow_redirects=True,
    )
    anterior.close()


_endurecer_cliente_postgrest(supabase)


def get_business_by_phone(phone_number: str):
    """Busca un negocio por su número de WhatsApp (el número del negocio, no del cliente)."""
    response = supabase.table("businesses").select("*").eq("phone_number", phone_number).execute()
    if response.data:
        return response.data[0]
    return None


def get_business_by_id(business_id: str):
    """Obtiene la informacion completa de un negocio (nombre, horario, etc)."""
    response = supabase.table("businesses").select("*").eq("id", business_id).execute()
    if response.data:
        return response.data[0]
    return None


def get_confirmed_appointments_for_day(business_id: str, fecha: str, employee_id: str | None = None):
    """
    Trae las citas de un negocio para un dia especifico (fecha 'YYYY-MM-DD')
    que ocupan un cupo: 'confirmed' y 'pending_payment' (un abono en
    espera de pago bloquea el horario igual que una confirmada, ver
    appointments_pending_payment.sql - si no contara aqui, el chat seguiria
    ofreciendo esa hora a otro cliente mientras el primero esta pagando).
    Junto con la duracion de su servicio, para detectar choques de horario.
    Si se pasa employee_id, solo trae las citas de ese empleado (cada uno
    tiene su propia agenda, asi que dos empleados pueden tener una cita a
    la misma hora sin que sea un choque real).
    """
    inicio_dia = f"{fecha}T00:00:00"
    fin_dia = f"{fecha}T23:59:59"

    query = (
        supabase.table("appointments")
        .select("*, services(duration_minutes)")
        .eq("business_id", business_id)
        .in_("status", ["confirmed", "pending_payment"])
        .gte("scheduled_at", inicio_dia)
        .lte("scheduled_at", fin_dia)
    )
    if employee_id:
        query = query.eq("employee_id", employee_id)
    response = query.execute()
    return response.data


def get_services(business_id: str):
    """Lista los servicios ACTIVOS disponibles de un negocio."""
    response = (
        supabase.table("services")
        .select("*")
        .eq("business_id", business_id)
        .eq("active", True)
        .execute()
    )
    return response.data


def get_home_visit_zones(business_id: str, solo_activas: bool = True):
    """Zonas/municipios donde el negocio hace domicilios, con su recargo."""
    query = supabase.table("home_visit_zones").select("*").eq("business_id", business_id)
    if solo_activas:
        query = query.eq("active", True)
    return query.order("name").execute().data


def create_appointment(
    business_id: str,
    client_phone: str,
    client_name: str,
    service_id: str,
    scheduled_at: str,
    employee_id: str,
    address: str | None = None,
    home_visit_zone: str | None = None,
    home_visit_fee: float = 0,
    status: str = "confirmed",
    source: str = "manual",
):
    """
    Crea una nueva cita, ligada al empleado que la atiende. Si trae
    `address`, la cita es a domicilio (las columnas is_home_visit/address
    solo se envian en ese caso). `status` es "confirmed" por defecto;
    los servicios con abono la crean como "pending_payment" (bloquea el
    cupo igual que una confirmada, ver appointments_pending_payment.sql)
    hasta que Wompi confirme el pago - ver
    services/appointment_confirmation.py. El indice unico parcial de esa
    migracion hace que este insert falle (postgrest.exceptions.APIError,
    code "23505") si alguien mas ya tiene ese mismo horario reservado;
    el caller debe atrapar ese caso.

    `source` registra quien origino la cita ("ai" cuando la agenda el
    asistente del chat, "manual" cuando la crea el negocio desde el panel);
    es la base de las metricas de IA vs. manual (services/metrics.py).
    """
    fila = {
        "business_id": business_id,
        "client_phone": client_phone,
        "client_name": client_name,
        "service_id": service_id,
        "scheduled_at": scheduled_at,
        "employee_id": employee_id,
        "status": status,
        "source": source,
    }
    if address:
        fila["is_home_visit"] = True
        fila["address"] = address
        if home_visit_zone:
            fila["home_visit_zone"] = home_visit_zone
            fila["home_visit_fee"] = home_visit_fee
    response = supabase.table("appointments").insert(fila).execute()
    return response.data


def update_appointment_status(appointment_id: str, status: str, actor: str | None = None) -> dict | None:
    """
    Cambia el estado de una cita existente (ej. "pending_payment" ->
    "confirmed" cuando Wompi aprueba el abono, o -> "cancelled" si el
    link de pago vence sin pagar). Ver services/appointment_confirmation.py.
    `actor` ("ai" | "system") queda en cancelled_by cuando status es
    "cancelled", para las metricas de cancelaciones por la IA.
    """
    campos = {"status": status}
    if status == "cancelled" and actor:
        campos["cancelled_by"] = actor
    response = supabase.table("appointments").update(campos).eq("id", appointment_id).execute()
    return response.data[0] if response.data else None

def get_business_by_whatsapp_phone_id(phone_number_id: str):
    """
    Busca el negocio que tiene este phone_number_id de WhatsApp asociado.
    Se usa en el webhook para identificar a que negocio pertenece cada
    mensaje entrante, permitiendo multiples negocios con numeros distintos.
    Retorna None si no se encuentra ningun negocio con ese numero.
    """
    response = (
        supabase.table("businesses")
        .select("*")
        .eq("whatsapp_phone_number_id", phone_number_id)
        .limit(1)
        .execute()
    )
    if response.data:
        return response.data[0]
    return None

def cancel_appointment(appointment_id: str, client_phone: str):
    """
    Cancela una cita existente, validando que pertenezca al cliente que la solicita
    (filtra por client_phone para evitar que se cancele la cita de otro cliente).
    """
    response = (
        supabase.table("appointments")
        .update({"status": "cancelled", "cancelled_by": "ai"})
        .eq("id", appointment_id)
        .eq("client_phone", client_phone)
        .execute()
    )

    if not response.data:
        return {"error": "No se encontro esa cita para este cliente, no se cancelo nada."}

    return response.data

def esta_chat_excluido(business_id: str, client_phone: str) -> bool:
    """
    Revisa si un numero de cliente esta en la lista de exclusion de un
    negocio (ej. familiares, proveedores u otros contactos que le escriben
    al mismo WhatsApp del negocio y no deben recibir respuestas del bot).
    """
    response = (
        supabase.table("excluded_chats")
        .select("phone_number")
        .eq("business_id", business_id)
        .eq("phone_number", client_phone)
        .limit(1)
        .execute()
    )
    return bool(response.data)


def listar_chats_excluidos(business_id: str):
    """Lista los numeros excluidos del bot para un negocio."""
    response = (
        supabase.table("excluded_chats")
        .select("*")
        .eq("business_id", business_id)
        .order("created_at", desc=True)
        .execute()
    )
    return response.data


def agregar_chat_excluido(business_id: str, phone_number: str):
    """Agrega (o reafirma) un numero a la lista de exclusion de un negocio."""
    response = (
        supabase.table("excluded_chats")
        .upsert(
            {"business_id": business_id, "phone_number": phone_number},
            on_conflict="business_id,phone_number",
        )
        .execute()
    )
    return response.data[0] if response.data else None


def eliminar_chat_excluido(business_id: str, phone_number: str):
    """Quita un numero de la lista de exclusion (el bot vuelve a responderle)."""
    supabase.table("excluded_chats").delete().eq("business_id", business_id).eq(
        "phone_number", phone_number
    ).execute()


def get_client_appointments(business_id: str, client_phone: str):
    """Obtiene las citas activas de un cliente en un negocio."""
    response = (
        supabase.table("appointments")
        .select("*")
        .eq("business_id", business_id)
        .eq("client_phone", client_phone)
        .eq("status", "confirmed")
        .execute()
    )
    return response.data


def get_appointment_by_id_and_phone(appointment_id: str, client_phone: str):
    """
    Trae una cita (con nombre, duracion y si requiere abono del servicio)
    validando que pertenezca al telefono dado, para cancelaciones y
    reprogramaciones. requires_payment se usa para saber si la cita ya
    tiene un pago real detras (ver agent/tools.py:cancelar_cita): una cita
    "confirmed" de un servicio con requires_payment=True solo llega a ese
    estado despues de que Wompi confirmo el pago, nunca antes. Retorna None
    si no existe o no es de ese cliente.
    """
    response = (
        supabase.table("appointments")
        .select("*, services(name, duration_minutes, requires_payment)")
        .eq("id", appointment_id)
        .eq("client_phone", client_phone)
        .execute()
    )
    if response.data:
        return response.data[0]
    return None


def get_appointment_full(appointment_id: str):
    """
    Trae una cita con los joins de servicio y empleado resueltos, en la
    misma forma que devuelve GET /appointments. La usa
    services/realtime.py para armar el payload de los eventos de citas en
    tiempo real (WebSocket) sin importar por que via se creo/modifico la
    cita. Retorna None si no existe.
    """
    response = (
        supabase.table("appointments")
        .select("*, services(name, price, duration_minutes), employees(name)")
        .eq("id", appointment_id)
        .execute()
    )
    if response.data:
        return response.data[0]
    return None


def update_appointment_schedule(
    appointment_id: str,
    nueva_fecha_hora: str,
    is_home_visit: bool | None = None,
    address: str | None = None,
    home_visit_zone: str | None = None,
    home_visit_fee: float | None = None,
):
    """
    Actualiza la fecha/hora de una cita existente (reprogramacion). Los
    parametros de domicilio son opcionales: se pasan cuando la
    reprogramacion tambien cambia la modalidad (local <-> domicilio) de la
    MISMA cita, en vez de dejarla igual - ver agent/tools.py:reprogramar_cita.
    Si is_home_visit es None, la modalidad actual de la cita no se toca.
    """
    campos = {"scheduled_at": nueva_fecha_hora}
    # Solo la IA llama a esta funcion (las ediciones del panel van directo a
    # Supabase): se suma al contador de reprogramaciones hechas por la IA.
    # El contador total lo lleva el trigger de metricas_analitica.sql.
    actual = (
        supabase.table("appointments")
        .select("rescheduled_by_ai_count")
        .eq("id", appointment_id)
        .execute()
    )
    if actual.data:
        campos["rescheduled_by_ai_count"] = (actual.data[0].get("rescheduled_by_ai_count") or 0) + 1
    if is_home_visit is not None:
        campos["is_home_visit"] = is_home_visit
        campos["address"] = address
        campos["home_visit_zone"] = home_visit_zone
        campos["home_visit_fee"] = home_visit_fee or 0

    response = (
        supabase.table("appointments")
        .update(campos)
        .eq("id", appointment_id)
        .execute()
    )
    return response.data


DIAS_VALIDOS_HORARIO = ["mon", "tue", "wed", "thu", "fri", "sat"]


def get_employees(business_id: str, solo_activos: bool = True):
    """Lista los empleados de un negocio (el dueño incluido, role='owner')."""
    query = supabase.table("employees").select("*").eq("business_id", business_id)
    if solo_activos:
        query = query.eq("active", True)
    response = query.order("created_at").execute()
    return response.data


def get_employee_by_id(employee_id: str):
    response = supabase.table("employees").select("*").eq("id", employee_id).execute()
    if response.data:
        return response.data[0]
    return None


def create_employee(business_id: str, user_id: str, name: str | None, role: str) -> dict:
    """
    Crea un empleado (role='owner' para el dueño al reclamar su negocio, o
    'staff' para un empleado normal) y le siembra un horario para que ya
    pueda recibir citas. Regla de negocio: el empleado principal (owner)
    hereda SIEMPRE el horario del negocio (business_hours) - ver
    sincronizar_horario_empleado_principal. Un empleado normal arranca con
    un horario por defecto (lunes a sabado 9am-7pm, domingo cerrado) que
    el dueño ajusta despues desde la app.
    """
    response = (
        supabase.table("employees")
        .insert({"business_id": business_id, "user_id": user_id, "name": name, "role": role})
        .execute()
    )
    empleado = response.data[0]

    if role == "owner" and sincronizar_horario_empleado_principal(business_id):
        return empleado

    try:
        filas_horario = [
            {
                "employee_id": empleado["id"],
                "day": dia,
                "is_open": True,
                "opening_time": "09:00",
                "closing_time": "19:00",
            }
            for dia in DIAS_VALIDOS_HORARIO
        ]
        filas_horario.append({"employee_id": empleado["id"], "day": "sun", "is_open": False})
        supabase.table("employee_hours").insert(filas_horario).execute()
    except Exception as e:
        print(f"No se pudo crear el horario por defecto del empleado {empleado['id']}: {e}")

    return empleado


def get_owner_employee(business_id: str) -> dict | None:
    """El empleado principal (role='owner') de un negocio, o None si todavia no tiene."""
    response = (
        supabase.table("employees")
        .select("*")
        .eq("business_id", business_id)
        .eq("role", "owner")
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


_CAMPOS_HORARIO = ["is_open", "opening_time", "closing_time", "lunch_start", "lunch_end"]


def sincronizar_horario_empleado_principal(business_id: str, dias: list[str] | None = None) -> bool:
    """
    Regla de negocio: el empleado principal (owner) hereda el horario del
    negocio. Copia business_hours -> employee_hours del owner (todos los
    dias, o solo los de `dias`). Hace falta porque la disponibilidad del
    chat y de las citas manuales se calcula con employee_hours, mientras
    que la pestaña "Horario" del panel edita business_hours: si se
    desincronizan, el chat deja de ofrecer horas que el dueño si
    configuro. Devuelve False si el negocio no tiene owner o no tiene
    horario del negocio que copiar (el caller decide el fallback).
    """
    owner = get_owner_employee(business_id)
    if not owner:
        return False

    dias_a_sincronizar = dias or (DIAS_VALIDOS_HORARIO + ["sun"])
    horario_negocio = (
        supabase.table("business_hours")
        .select("*")
        .eq("business_id", business_id)
        .in_("day", dias_a_sincronizar)
        .execute()
        .data
    )
    if not horario_negocio:
        return False

    # Un dia sin fila en business_hours se muestra como cerrado en el panel
    # (y asi lo describe el prompt del chat): el principal lo hereda cerrado,
    # en vez de quedarse con lo que tuviera antes.
    por_dia = {fila["day"]: fila for fila in horario_negocio}
    filas = [
        {
            "employee_id": owner["id"],
            "day": dia,
            **(
                {c: por_dia[dia].get(c) for c in _CAMPOS_HORARIO}
                if dia in por_dia
                else {c: (False if c == "is_open" else None) for c in _CAMPOS_HORARIO}
            ),
        }
        for dia in dias_a_sincronizar
    ]
    supabase.table("employee_hours").upsert(filas, on_conflict="employee_id,day").execute()
    return True


def update_employee(employee_id: str, campos: dict) -> dict | None:
    response = supabase.table("employees").update(campos).eq("id", employee_id).execute()
    return response.data[0] if response.data else None


def get_employee_hours(employee_id: str):
    response = supabase.table("employee_hours").select("*").eq("employee_id", employee_id).execute()
    return response.data


def get_employee_hours_for_day(employee_id: str, dia_codigo: str):
    response = (
        supabase.table("employee_hours")
        .select("*")
        .eq("employee_id", employee_id)
        .eq("day", dia_codigo)
        .execute()
    )
    if response.data:
        return response.data[0]
    return None


def upsert_employee_hours(employee_id: str, dia: str, campos: dict) -> dict | None:
    fila = {"employee_id": employee_id, "day": dia, **campos}
    response = supabase.table("employee_hours").upsert(fila, on_conflict="employee_id,day").execute()
    return response.data[0] if response.data else None


def get_employee_services(employee_id: str):
    """Servicios activos a los que esta ligado un empleado."""
    response = (
        supabase.table("employee_services")
        .select("service_id, services(*)")
        .eq("employee_id", employee_id)
        .execute()
    )
    return [fila["services"] for fila in response.data if fila.get("services") and fila["services"].get("active")]


def set_employee_services(employee_id: str, service_ids: list[str]):
    """Reemplaza por completo la lista de servicios ligados a un empleado."""
    supabase.table("employee_services").delete().eq("employee_id", employee_id).execute()
    if service_ids:
        filas = [{"employee_id": employee_id, "service_id": sid} for sid in service_ids]
        supabase.table("employee_services").insert(filas).execute()


def asignar_servicio_a_empleado_principal(business_id: str, service_id: str):
    """
    Liga un servicio recien creado al empleado principal (role='owner') del
    negocio, para que el dueño no tenga que hacerlo a mano cada vez. Es
    idempotente (upsert) y no debe tumbar la creacion del servicio.
    """
    try:
        owner = (
            supabase.table("employees")
            .select("id")
            .eq("business_id", business_id)
            .eq("role", "owner")
            .limit(1)
            .execute()
        )
        if not owner.data:
            return
        supabase.table("employee_services").upsert(
            {"employee_id": owner.data[0]["id"], "service_id": service_id},
            on_conflict="employee_id,service_id",
        ).execute()
    except Exception as e:
        print(f"No se pudo asignar el servicio {service_id} al empleado principal: {e}")


def registrar_mensaje_entrante_whatsapp(business_id: str, client_phone: str):
    """
    Registra que un cliente le escribio a un negocio, actualizando el
    momento del ultimo mensaje entrante. Es lo que usa
    cliente_dentro_de_ventana_24h para saber si un envio posterior (ej. un
    recordatorio de cita) puede ir como texto libre o necesita un message
    template aprobado por Meta.
    """
    ahora = datetime.now(timezone.utc).isoformat()
    supabase.table("whatsapp_client_contacts").upsert(
        {"business_id": business_id, "client_phone": client_phone, "last_inbound_at": ahora},
        on_conflict="business_id,client_phone",
    ).execute()


def cliente_dentro_de_ventana_24h(business_id: str, client_phone: str) -> bool:
    """
    True si este cliente le escribio a este negocio en las ultimas 24
    horas (la ventana de conversacion de Meta dentro de la cual se puede
    mandar texto libre); False si nunca ha escrito o si paso mas de 24h,
    en cuyo caso hace falta un message template aprobado.
    """
    response = (
        supabase.table("whatsapp_client_contacts")
        .select("last_inbound_at")
        .eq("business_id", business_id)
        .eq("client_phone", client_phone)
        .limit(1)
        .execute()
    )
    if not response.data or not response.data[0].get("last_inbound_at"):
        return False

    last_inbound_at = datetime.fromisoformat(response.data[0]["last_inbound_at"])
    return datetime.now(timezone.utc) - last_inbound_at < timedelta(hours=24)