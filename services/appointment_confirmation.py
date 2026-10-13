"""
Creacion y confirmacion de citas, con los efectos que deben acompañarlas
(notificacion push al negocio, confirmacion por WhatsApp al cliente,
evento de tiempo real para el panel). Dos flujos:

- SIN abono (servicio no requiere pago): finalizar_creacion_cita crea la
  cita ya "confirmed" y dispara todo de una vez. Usada desde
  agent/tools.py:crear_cita.

- CON abono: la cita se crea de INMEDIATO como "pending_payment"
  (crear_cita_pendiente_pago) para bloquear el cupo desde que se genera
  el link de pago, no solo cuando Wompi confirma - asi el horario no
  queda "abierto" mientras el cliente paga. Las notificaciones se
  posponen hasta confirmar_cita_pendiente, que se llama cuando el pago
  se aprueba (ver services/wompi_payment_requests.py:confirmar_cita_desde_pago).
"""

from datetime import datetime

from postgrest.exceptions import APIError

from services.db import create_appointment, get_appointment_full, get_business_by_id, update_appointment_status
from services.push_notifications import enviar_notificacion_nueva_cita
from services.scheduling import formatear_fecha_natural
from services.whatsapp import enviar_confirmacion_cita_cliente

# Codigo SQLSTATE de Postgres para "unique_violation" (surge a traves de
# PostgREST cuando el indice unico parcial de appointments_pending_payment.sql
# rechaza un insert por choque de horario concurrente).
_CODIGO_VIOLACION_UNICA = "23505"


def finalizar_creacion_cita(
    business_id: str,
    client_phone: str,
    client_name: str,
    service_id: str,
    service_name: str,
    scheduled_at: str,
    employee_id: str,
    address: str | None = None,
    home_visit_zone: str | None = None,
    home_visit_fee: float = 0,
    source: str = "ai",
) -> dict | None:
    """Crea la cita y dispara sus notificaciones. Retorna la fila creada, o None si create_appointment no devolvio nada."""
    resultado = create_appointment(
        business_id=business_id,
        client_phone=client_phone,
        client_name=client_name,
        service_id=service_id,
        scheduled_at=scheduled_at,
        employee_id=employee_id,
        address=address,
        home_visit_zone=home_visit_zone,
        home_visit_fee=home_visit_fee,
        source=source,
    )

    if not resultado:
        return None

    fecha_hora_dt = datetime.fromisoformat(scheduled_at)
    business = get_business_by_id(business_id)

    try:
        enviar_notificacion_nueva_cita(
            fcm_token=business.get("fcm_token") if business else None,
            nombre_cliente=client_name,
            servicio=service_name,
            fecha_hora_texto=formatear_fecha_natural(fecha_hora_dt),
        )
    except Exception as e:
        print(f"No se pudo enviar la notificacion push: {e}")

    try:
        enviar_confirmacion_cita_cliente(
            client_phone=client_phone,
            nombre_cliente=client_name,
            nombre_negocio=business.get("name", "el negocio") if business else "el negocio",
            nombre_servicio=service_name,
            fecha_hora_texto=formatear_fecha_natural(fecha_hora_dt),
        )
    except Exception as e:
        print(f"No se pudo enviar la confirmacion de cita por WhatsApp al cliente: {e}")

    try:
        from services.realtime import emitir_evento_cita

        emitir_evento_cita("appointment.created", business_id, resultado[0]["id"])
    except Exception as e:
        print(f"No se pudo emitir el evento de tiempo real de la cita: {e}")

    return resultado[0]


def crear_cita_pendiente_pago(
    business_id: str,
    client_phone: str,
    client_name: str,
    service_id: str,
    scheduled_at: str,
    employee_id: str,
    address: str | None = None,
    home_visit_zone: str | None = None,
    home_visit_fee: float = 0,
    source: str = "ai",
) -> dict | None:
    """
    Crea la cita en estado "pending_payment": bloquea el cupo (cuenta para
    el choque de horario y para el indice unico de la base de datos) pero
    NO dispara notificaciones todavia - esas se posponen hasta que el pago
    se confirme (ver confirmar_cita_pendiente). Tampoco emite el evento de
    tiempo real "appointment.created": el panel no debe mostrar "nueva
    cita" (con su sonido/toast) por algo que el cliente ni siquiera ha
    pagado.

    Retorna None si alguien mas ya tiene ese horario reservado (choque de
    concurrencia real: dos clientes pidiendo el mismo cupo casi al mismo
    tiempo) - el indice unico parcial de appointments_pending_payment.sql
    rechaza el segundo insert, y aqui se atrapa ese error especifico para
    que el caller pueda ofrecerle otro horario al cliente en vez de fallar
    con un error generico.
    """
    try:
        resultado = create_appointment(
            business_id=business_id,
            client_phone=client_phone,
            client_name=client_name,
            service_id=service_id,
            scheduled_at=scheduled_at,
            employee_id=employee_id,
            address=address,
            home_visit_zone=home_visit_zone,
            home_visit_fee=home_visit_fee,
            status="pending_payment",
            source=source,
        )
    except APIError as e:
        if getattr(e, "code", None) == _CODIGO_VIOLACION_UNICA:
            return None
        raise

    return resultado[0] if resultado else None


def confirmar_cita_pendiente(appointment_id: str) -> dict | None:
    """
    Pasa una cita de "pending_payment" a "confirmed" (el abono ya se
    verifico) y dispara recien AHORA las notificaciones que se posponen
    mientras el pago esta en curso: push al negocio, confirmacion por
    WhatsApp al cliente, y el evento de tiempo real "appointment.created"
    del panel (semanticamente "nace" aqui para el negocio, aunque la fila
    ya existiera desde antes como hold). Retorna None si la cita ya no
    existe.
    """
    actualizada = update_appointment_status(appointment_id, "confirmed")
    if not actualizada:
        return None

    cita = get_appointment_full(appointment_id) or actualizada
    fecha_hora_dt = datetime.fromisoformat(cita["scheduled_at"])
    nombre_servicio = (cita.get("services") or {}).get("name", "tu cita")
    business = get_business_by_id(cita["business_id"])

    try:
        enviar_notificacion_nueva_cita(
            fcm_token=business.get("fcm_token") if business else None,
            nombre_cliente=cita.get("client_name") or "Cliente",
            servicio=nombre_servicio,
            fecha_hora_texto=formatear_fecha_natural(fecha_hora_dt),
        )
    except Exception as e:
        print(f"No se pudo enviar la notificacion push: {e}")

    try:
        enviar_confirmacion_cita_cliente(
            client_phone=cita["client_phone"],
            nombre_cliente=cita.get("client_name") or "Cliente",
            nombre_negocio=business.get("name", "el negocio") if business else "el negocio",
            nombre_servicio=nombre_servicio,
            fecha_hora_texto=formatear_fecha_natural(fecha_hora_dt),
        )
    except Exception as e:
        print(f"No se pudo enviar la confirmacion de cita por WhatsApp al cliente: {e}")

    try:
        from services.realtime import emitir_evento_cita

        emitir_evento_cita("appointment.created", cita["business_id"], appointment_id)
    except Exception as e:
        print(f"No se pudo emitir el evento de tiempo real de la cita: {e}")

    return cita
