from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from datetime import datetime

from services.db import get_home_visit_zones, get_services, create_appointment, get_business_by_id, get_employee_by_id, supabase
from services.scheduling import es_hora_valida, hay_choque_de_horario, formatear_fecha_natural, generar_horas_disponibles
from services.auth import obtener_usuario_actual, verificar_acceso_empleado, verificar_dueno
from services.notificaciones_cliente import datos_para_plantilla, nombre_del_empleado
from services.whatsapp import enviar_confirmacion_cita_cliente, normalizar_numero_whatsapp

router = APIRouter(tags=["appointments"])


@router.get("/appointments")
def listar_citas(
    business_id: str,
    employee_id: str | None = None,
    status: str | None = Query(default=None, pattern="^(pending|pending_payment|confirmed|completed|cancelled)$"),
    date_from: str | None = None,
    date_to: str | None = None,
    home_visit: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(obtener_usuario_actual),
):
    """
    Lista las citas de un negocio, con filtros opcionales (empleado, estado,
    rango de fechas sobre scheduled_at) y paginacion (limit/offset). Trae
    tambien el nombre del servicio y del empleado via join, para que el
    frontend no tenga que resolverlos aparte.

    Si se pasa employee_id, el acceso lo valida verificar_acceso_empleado
    (el dueño del negocio, o el propio empleado viendo sus citas). Sin
    employee_id (el dueño viendo todas las citas del negocio), requiere ser
    el dueño.
    """
    if employee_id:
        verificar_acceso_empleado(employee_id, user_id)
    else:
        verificar_dueno(business_id, user_id)

    query = (
        supabase.table("appointments")
        .select("*, services(name, price, duration_minutes), employees(name)", count="exact")
        .eq("business_id", business_id)
    )
    if employee_id:
        query = query.eq("employee_id", employee_id)
    if status:
        query = query.eq("status", status)
    if date_from:
        query = query.gte("scheduled_at", date_from)
    if date_to:
        query = query.lte("scheduled_at", date_to)
    if home_visit is not None:
        query = query.eq("is_home_visit", home_visit)

    response = query.order("scheduled_at", desc=False).range(offset, offset + limit - 1).execute()
    return {"appointments": response.data, "total": response.count, "limit": limit, "offset": offset}


@router.get("/appointments/available-slots")
def turnos_disponibles(
    business_id: str,
    employee_id: str,
    date: str,
    service_id: str | None = None,
    user_id: str = Depends(obtener_usuario_actual),
):
    """
    Calcula los horarios libres de un empleado en un dia dado, a partir del
    horario configurado, el almuerzo, las citas ya confirmadas y la
    duracion del servicio (o 30 min por defecto si no se pasa service_id).
    Reutiliza la misma logica que usa el agente de IA para ofrecer horas.
    """
    verificar_dueno(business_id, user_id)

    try:
        datetime.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Fecha invalida")

    duracion_minutos = 30
    if service_id:
        servicios = get_services(business_id)
        servicio = next((s for s in servicios if s["id"] == service_id), None)
        if not servicio:
            raise HTTPException(status_code=404, detail="Servicio no encontrado")
        duracion_minutos = servicio.get("duration_minutes", 30)

    available_slots = generar_horas_disponibles(business_id, employee_id, date, duracion_minutos)

    return {
        "business_id": business_id,
        "employee_id": employee_id,
        "date": date,
        "duration_minutes": duracion_minutos,
        "available_slots": available_slots,
    }


class CrearCitaManualInput(BaseModel):
    business_id: str
    employee_id: str
    client_name: str
    # Opcional: una cita manual puede ser de un cliente presencial sin
    # WhatsApp (ej. llego caminando, o solo dio un fijo). "" (no None) para
    # que encaje sin cambios con la columna client_phone de appointments,
    # que es NOT NULL - ver tambien services/reminder_service.py, que
    # salta estas citas en vez de intentar mandarles recordatorio.
    client_phone: str = ""
    service_id: str
    fecha_hora: str  # formato ISO 8601, ej: 2026-07-01T15:00:00
    address: str | None = None  # solo si la cita es a domicilio
    zone: str | None = None  # municipio/zona atendida (obligatoria si el negocio definio zonas)


@router.post("/appointments/manual")
def crear_cita_manual(data: CrearCitaManualInput, user_id: str = Depends(obtener_usuario_actual)):
    """
    Crea una cita directamente desde el panel (no desde WhatsApp), para
    cuando un cliente llega en persona o llama por telefono. Reutiliza
    las mismas validaciones de horario, almuerzo y choques que usa la IA
    (ahora por empleado), para que la agenda nunca quede inconsistente sin
    importar el canal por el que entro la cita.
    """
    verificar_dueno(data.business_id, user_id)

    empleado = get_employee_by_id(data.employee_id)
    if not empleado or empleado["business_id"] != data.business_id or not empleado.get("active"):
        raise HTTPException(status_code=404, detail="Empleado no encontrado")

    servicios = get_services(data.business_id)
    servicio = next((s for s in servicios if s["id"] == data.service_id), None)
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")

    duracion = servicio.get("duration_minutes", 30)

    direccion = (data.address or "").strip() or None
    negocio = get_business_by_id(data.business_id)
    if direccion and not (negocio or {}).get("home_visits_enabled"):
        raise HTTPException(status_code=400, detail="Este negocio no tiene los domicilios activados")
    if direccion and not servicio.get("offers_home_visit"):
        raise HTTPException(status_code=400, detail="Ese servicio no se presta a domicilio")

    zona_elegida = None
    if direccion:
        zonas = get_home_visit_zones(data.business_id)
        if zonas:
            zona_elegida = next((z for z in zonas if z["name"].strip().lower() == (data.zone or "").strip().lower()), None)
            if not zona_elegida:
                raise HTTPException(status_code=400, detail="Ese lugar esta fuera de las zonas donde se hacen domicilios")

    es_valida, mensaje_error = es_hora_valida(data.fecha_hora, data.employee_id, duracion)
    if not es_valida:
        raise HTTPException(status_code=400, detail=mensaje_error)

    fecha_hora_dt = datetime.fromisoformat(data.fecha_hora)

    hay_choque, mensaje_choque = hay_choque_de_horario(data.business_id, data.employee_id, fecha_hora_dt, duracion)
    if hay_choque:
        raise HTTPException(status_code=409, detail=mensaje_choque)

    resultado = create_appointment(
        business_id=data.business_id,
        client_phone=data.client_phone,
        client_name=data.client_name,
        service_id=data.service_id,
        scheduled_at=data.fecha_hora,
        employee_id=data.employee_id,
        address=direccion,
        home_visit_zone=zona_elegida["name"] if zona_elegida else None,
        home_visit_fee=float(zona_elegida["fee"]) if zona_elegida else 0,
    )

    if resultado:
        from services.realtime import emitir_evento_cita

        emitir_evento_cita("appointment.created", data.business_id, resultado[0]["id"])

    # El formulario del panel no obliga a que client_phone sea un WhatsApp
    # (a veces es un cliente presencial sin WhatsApp), asi que la
    # confirmacion se manda solo si el numero normaliza a un celular
    # colombiano valido; si no, simplemente no se envia (no bloquea la cita).
    numero_whatsapp = normalizar_numero_whatsapp(data.client_phone)
    if numero_whatsapp:
        try:
            business = get_business_by_id(data.business_id)
            enviar_confirmacion_cita_cliente(
                client_phone=numero_whatsapp,
                nombre_cliente=data.client_name,
                nombre_negocio=business.get("name", "el negocio") if business else "el negocio",
                nombre_servicio=servicio["name"],
                fecha_hora_texto=formatear_fecha_natural(fecha_hora_dt),
                datos_plantilla=datos_para_plantilla(business, fecha_hora_dt, nombre_del_empleado(data.employee_id)),
            )
        except Exception as e:
            print(f"No se pudo enviar la confirmacion de cita por WhatsApp al cliente: {e}")

    return {"cita_creada": resultado}