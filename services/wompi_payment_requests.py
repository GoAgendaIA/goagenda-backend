"""
Solicitudes de pago (abonos) generadas desde el chat cuando un servicio
requiere abono para agendar (ver services.requires_payment,
wompi_pagos.sql). Cada solicitud genera un Link de Pago de Wompi
(services/wompi_client.py) y queda registrada en wompi_payment_requests;
se actualiza cuando llega el webhook de confirmacion
(routes/wompi_webhook_routes.py).
"""

import os
from datetime import datetime, timedelta, timezone

from services.db import get_business_by_id, supabase
from services.push_notifications import enviar_notificacion_pago_recibido
from services.realtime import gestor_tiempo_real
from services.wompi_client import WompiError, consultar_transaccion, construir_url_checkout, crear_link_de_pago
from services.wompi_credentials import obtener_credenciales_para_pago

HORAS_EXPIRACION_LINK = 24.0

# URL del frontend (widget de chat), para armar el redirect_url del link de
# pago: sin esto, el cliente paga en la pestaña de Wompi y no vuelve al
# chat, asi que nunca ve la confirmacion aunque el mensaje SI se inserte
# correctamente (el widget la recoge por polling, pero solo si la pestaña
# del chat sigue abierta). Ver .env.example.
FRONTEND_URL = os.getenv("GOAGENDA_FRONTEND_URL", "http://localhost:4200").rstrip("/")


def _construir_redirect_url(business_id: str, employee_id: str | None) -> str:
    """URL del chat a la que Wompi redirige al cliente despues de pagar (o cancelar el pago)."""
    if employee_id:
        return f"{FRONTEND_URL}/chat/{business_id}/{employee_id}"
    return f"{FRONTEND_URL}/chat/{business_id}"


def _formato_precio_cop(precio) -> str:
    """Formatea un precio (en pesos) como '$15.000'. Copia local de agent/tools.py:_formato_precio_cop."""
    try:
        return f"${int(round(float(precio))):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "$0"


def calcular_monto_abono_cents(servicio: dict) -> int | None:
    """
    Calcula el monto del abono en centavos segun la configuracion del
    servicio: 'percentage' es un % de services.price (que esta en pesos,
    no centavos - igual que en agent/tools.py:_formato_precio_cop), 'fixed'
    usa services.payment_fixed_amount_cents tal cual. Retorna None si el
    servicio no requiere pago o la configuracion esta incompleta.
    """
    if not servicio.get("requires_payment"):
        return None

    tipo = servicio.get("payment_type")
    if tipo == "percentage":
        porcentaje = servicio.get("payment_percentage")
        precio = servicio.get("price")
        if not porcentaje or precio is None:
            return None
        return round(float(precio) * float(porcentaje) / 100 * 100)
    if tipo == "fixed":
        monto = servicio.get("payment_fixed_amount_cents")
        return int(monto) if monto else None
    return None


def crear_solicitud_pago(
    business_id: str,
    servicio: dict,
    session_id: str | None,
    client_phone: str | None,
    appointment_id: str | None = None,
    employee_id: str | None = None,
    expira_en_horas: float = HORAS_EXPIRACION_LINK,
) -> dict | None:
    """
    Genera un link de pago de Wompi para el abono de un servicio y lo
    registra. Retorna None (nunca lanza excepcion) si el servicio no
    requiere pago, si el negocio no tiene Wompi configurado, o si algo
    falla al llamar a Wompi - el caller decide como degradar en ese caso.

    appointment_id debe ser el id de la cita ya creada como
    "pending_payment" (ver agent/tools.py:crear_cita y
    services/appointment_confirmation.py:crear_cita_pendiente_pago): el
    cupo ya esta bloqueado desde ese insert, este link solo cubre el pago.

    employee_id (si el chat era el de un empleado especifico) se usa solo
    para armar el redirect_url de vuelta al chat correcto despues de
    pagar - el widget guarda el session_id en localStorage bajo una clave
    que incluye el employee_id, asi que redirigir a la URL equivocada
    haria que el cliente "pierda" su conversacion y le abra una nueva.
    """
    monto_cents = calcular_monto_abono_cents(servicio)
    if not monto_cents or monto_cents <= 0:
        return None

    credenciales = obtener_credenciales_para_pago(business_id)
    if not credenciales:
        print(
            f"El servicio '{servicio.get('name')}' (negocio {business_id}) requiere abono pero "
            "el negocio no tiene Wompi configurado todavia (Ajustes > Pagos)."
        )
        return None

    descripcion = servicio.get("payment_description") or f"Abono para {servicio.get('name', 'tu cita')}"

    try:
        link = crear_link_de_pago(
            private_key=credenciales["private_key"],
            sandbox_mode=credenciales["sandbox_mode"],
            nombre=f"Abono - {servicio.get('name', 'Servicio')}"[:255],
            descripcion=descripcion,
            amount_in_cents=monto_cents,
            expira_en_horas=expira_en_horas,
            redirect_url=_construir_redirect_url(business_id, employee_id),
        )
    except WompiError as e:
        print(f"No se pudo crear el link de pago de Wompi para el negocio {business_id}: {e}")
        return None

    payment_link_id = link.get("id")
    if not payment_link_id:
        print(f"Wompi no devolvio un id de link de pago para el negocio {business_id}: {link}")
        return None

    checkout_url = construir_url_checkout(payment_link_id)
    expira_en = datetime.now(timezone.utc) + timedelta(hours=expira_en_horas)

    fila = {
        "business_id": business_id,
        "service_id": servicio.get("id"),
        "appointment_id": appointment_id,
        "session_id": session_id,
        "client_phone": client_phone,
        "amount_in_cents": monto_cents,
        "description": descripcion,
        "status": "pending",
        "wompi_payment_link_id": payment_link_id,
        "checkout_url": checkout_url,
        "expires_at": expira_en.isoformat(),
    }

    try:
        respuesta = supabase.table("wompi_payment_requests").insert(fila).execute()
        request_id = respuesta.data[0]["id"] if respuesta.data else None
    except Exception as e:
        # El link ya existe en Wompi aunque falle el guardado local; se
        # entrega igual al cliente (mejor eso que perder la solicitud de
        # pago), pero queda huerfano para la reconciliacion del webhook.
        print(f"El link de pago se creo en Wompi pero no se pudo guardar en la base de datos: {e}")
        request_id = None

    return {
        "request_id": request_id,
        "checkout_url": checkout_url,
        "amount_in_cents": monto_cents,
        "description": descripcion,
    }


def obtener_solicitud_por_payment_link(business_id: str, payment_link_id: str) -> dict | None:
    """Busca la solicitud de pago asociada a un link de pago de Wompi (para reconciliar el webhook)."""
    respuesta = (
        supabase.table("wompi_payment_requests")
        .select("*")
        .eq("business_id", business_id)
        .eq("wompi_payment_link_id", payment_link_id)
        .limit(1)
        .execute()
    )
    return respuesta.data[0] if respuesta.data else None


def actualizar_estado_transaccion(request_id: str, transaction_id: str, status: str) -> dict | None:
    """
    Actualiza una solicitud de pago con el resultado de una transaccion de
    Wompi. Solo pasa a status='paid' (con paid_at) cuando Wompi aprueba;
    otros estados (DECLINED, VOIDED, ERROR, PENDING) solo se reflejan en
    wompi_transaction_status para visibilidad - la solicitud sigue
    'pending' porque el cliente puede reintentar el pago en el mismo link.
    """
    campos = {
        "wompi_transaction_id": transaction_id,
        "wompi_transaction_status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if status == "APPROVED":
        campos["status"] = "paid"
        campos["paid_at"] = datetime.now(timezone.utc).isoformat()

    respuesta = supabase.table("wompi_payment_requests").update(campos).eq("id", request_id).execute()
    return respuesta.data[0] if respuesta.data else None


def obtener_solicitud_por_id(business_id: str, request_id: str) -> dict | None:
    """Trae una solicitud de pago puntual, validando que pertenezca a este negocio."""
    respuesta = (
        supabase.table("wompi_payment_requests")
        .select("*, services(name)")
        .eq("id", request_id)
        .eq("business_id", business_id)
        .execute()
    )
    return respuesta.data[0] if respuesta.data else None


def listar_solicitudes_pago(business_id: str, status: str | None = None, limit: int = 50) -> list[dict]:
    """
    Lista las solicitudes de pago de un negocio (mas recientes primero),
    con el nombre del servicio ya resuelto, para el panel de Ajustes > Pagos.
    """
    query = (
        supabase.table("wompi_payment_requests")
        .select("*, services(name)")
        .eq("business_id", business_id)
    )
    if status:
        query = query.eq("status", status)
    respuesta = query.order("created_at", desc=True).limit(limit).execute()
    return respuesta.data


def confirmar_cita_desde_pago(business_id: str, solicitud: dict) -> dict:
    """
    Se llama cuando Wompi confirma el pago de una solicitud de abono. La
    cita ya existe desde que se genero el link (estado "pending_payment",
    bloqueando el cupo - ver agent/tools.py:crear_cita y
    services/appointment_confirmation.py:crear_cita_pendiente_pago), asi
    que aqui solo falta pasarla a "confirmed" y disparar las
    notificaciones que se posponen mientras el pago esta en curso.

    No hace falta revalidar el horario: el indice unico de
    appointments_pending_payment.sql garantiza que nadie mas pudo tomar
    ese cupo mientras tanto. El unico caso de conflicto real es que el
    propio cliente (u otra via) haya CANCELADO esa cita mientras el pago
    estaba en camino - ahi no se puede confirmar y se marca conflicto=True
    para que el negocio lo resuelva a mano (el pago ya se hizo).

    Retorna {"cita": dict|None, "conflicto": bool}.
    """
    appointment_id = solicitud.get("appointment_id")
    if not appointment_id:
        return {"cita": None, "conflicto": False}

    from services.appointment_confirmation import confirmar_cita_pendiente
    from services.db import get_appointment_full

    cita_actual = get_appointment_full(appointment_id)
    if not cita_actual:
        return {"cita": None, "conflicto": True}

    if cita_actual.get("status") == "cancelled":
        return {"cita": cita_actual, "conflicto": True}

    if cita_actual.get("status") == "confirmed":
        # Ya estaba confirmada (webhook duplicado o reconciliacion manual repetida): no reprocesar.
        return {"cita": cita_actual, "conflicto": False}

    cita_confirmada = confirmar_cita_pendiente(appointment_id)
    return {"cita": cita_confirmada, "conflicto": cita_confirmada is None}


def notificar_pago_confirmado(
    business_id: str, solicitud: dict, amount_in_cents: int | None, cita: dict | None = None, conflicto: bool = False
) -> None:
    """
    Avisa al negocio (push + tiempo real) y confirma al cliente en el
    mismo chat que su abono llego, incluyendo los detalles de la cita
    cuando ya se creo (agendamiento con abono, ver
    confirmar_cita_desde_pago). Se usa tanto desde el webhook
    (routes/wompi_webhook_routes.py) como desde la reconciliacion manual
    (verificar_estado_solicitud, mas abajo), para no duplicar esta logica.
    """
    monto_texto = _formato_precio_cop((amount_in_cents or solicitud.get("amount_in_cents", 0)) / 100)
    session_id = solicitud.get("session_id")

    if session_id:
        try:
            # Import diferido: agent.graph importa (transitivamente) de este
            # mismo modulo via agent.tools, un import a nivel de archivo
            # aqui crearia un ciclo circular.
            from agent.graph import enviar_notificacion_sistema
            from services.scheduling import formatear_fecha_natural

            if conflicto:
                mensaje = (
                    f"✅ ¡Pago confirmado! Recibimos tu abono de *{monto_texto}*.\n\n"
                    "El horario que habias elegido ya no estaba disponible justo cuando se confirmo el pago. "
                    "Tu pago y tu cupo quedaron registrados: el negocio te va a escribir en breve para coordinar "
                    "el horario exacto 🙏"
                )
            elif cita:
                fecha_texto = formatear_fecha_natural(datetime.fromisoformat(cita["scheduled_at"]))
                servicio_nombre = (cita.get("services") or {}).get("name", "tu cita")
                mensaje = (
                    f"✅ ¡Pago confirmado! Recibimos tu abono de *{monto_texto}*.\n\n"
                    "Tu cita quedo agendada:\n"
                    f"💇 Servicio: *{servicio_nombre}*\n"
                    f"📅 Cuando: *{fecha_texto}*\n\n"
                    "¡Te esperamos! 🙌"
                )
            else:
                mensaje = f"✅ ¡Pago confirmado! Recibimos tu abono de *{monto_texto}*. Tu cita ya esta asegurada 🙌"

            enviar_notificacion_sistema(business_id, session_id, mensaje)
        except Exception as e:
            print(f"No se pudo insertar el mensaje de pago confirmado en el chat {session_id}: {e}")

    try:
        business = get_business_by_id(business_id)
        enviar_notificacion_pago_recibido(
            fcm_token=business.get("fcm_token") if business else None,
            monto_texto=monto_texto,
            descripcion=solicitud.get("description") or "Abono",
        )
    except Exception as e:
        print(f"No se pudo enviar la notificacion push de pago recibido: {e}")

    try:
        gestor_tiempo_real.emitir(
            business_id,
            {
                "type": "payment.received",
                "business_id": business_id,
                "session_id": session_id,
                "appointment_id": (cita or {}).get("id") or solicitud.get("appointment_id"),
                "amount_in_cents": amount_in_cents or solicitud.get("amount_in_cents"),
                "conflicto": conflicto,
            },
        )
    except Exception as e:
        print(f"No se pudo emitir el evento de tiempo real de pago: {e}")


def verificar_estado_solicitud(business_id: str, request_id: str) -> dict:
    """
    Reconciliacion manual: re-consulta el estado real en Wompi para una
    solicitud pendiente, por si el webhook nunca llego (ej. URL de eventos
    mal configurada en el Dashboard de Wompi, o el evento se perdio).

    LIMITACION IMPORTANTE: Wompi no expone un endpoint publico para listar
    las transacciones de un link de pago - solo se puede consultar una
    transaccion puntual por su id (GET /transactions/{id}), y ese id solo
    lo conocemos DESPUES de recibir al menos un webhook. Si todavia no
    llego ningun evento para esta solicitud, no hay nada que reconsultar:
    se informa explicitamente en vez de fingir que se verifico.
    """
    solicitud = obtener_solicitud_por_id(business_id, request_id)
    if not solicitud:
        raise ValueError("Solicitud de pago no encontrada")

    if solicitud["status"] == "paid":
        return {"solicitud": solicitud, "mensaje": "Este abono ya estaba confirmado."}

    if not solicitud.get("wompi_transaction_id"):
        return {
            "solicitud": solicitud,
            "mensaje": (
                "Todavia no se ha registrado ningun intento de pago para este link. Si el cliente ya pago, "
                "verifica que la URL de eventos este bien configurada en tu Dashboard de Wompi (Ajustes > Pagos)."
            ),
        }

    credenciales = obtener_credenciales_para_pago(business_id)
    if not credenciales:
        raise ValueError("Este negocio ya no tiene Wompi configurado.")

    try:
        transaccion = consultar_transaccion(
            credenciales["private_key"], credenciales["sandbox_mode"], solicitud["wompi_transaction_id"]
        )
    except WompiError as e:
        raise ValueError(str(e)) from e

    status_anterior = solicitud["status"]
    actualizada = actualizar_estado_transaccion(request_id, transaccion["id"], transaccion["status"])

    if transaccion["status"] == "APPROVED" and status_anterior != "paid":
        resultado_cita = confirmar_cita_desde_pago(business_id, solicitud)
        notificar_pago_confirmado(
            business_id,
            solicitud,
            transaccion.get("amount_in_cents"),
            cita=resultado_cita["cita"],
            conflicto=resultado_cita["conflicto"],
        )

    return {"solicitud": actualizada or solicitud, "mensaje": f"Estado actualizado: {transaccion['status']}."}


def expirar_solicitudes_vencidas() -> int:
    """
    Marca como 'expired' las solicitudes 'pending' cuyo expires_at ya
    paso, y CANCELA la cita "pending_payment" que tenian asociada (si el
    cliente nunca pago, el cupo se libera - deja de contar para el choque
    de horario y para el indice unico de appointments_pending_payment.sql,
    asi que otro cliente puede tomarlo). Pensada para correr
    periodicamente desde el scheduler de main.py, igual que
    services/reminder_service.py:revisar_y_enviar_recordatorios. Retorna
    cuantas se expiraron, solo para logging.
    """
    ahora = datetime.now(timezone.utc).isoformat()
    vencidas = (
        supabase.table("wompi_payment_requests")
        .select("id, appointment_id")
        .eq("status", "pending")
        .lt("expires_at", ahora)
        .execute()
    ).data or []

    if not vencidas:
        return 0

    ids = [v["id"] for v in vencidas]
    supabase.table("wompi_payment_requests").update({"status": "expired"}).in_("id", ids).execute()

    appointment_ids = [v["appointment_id"] for v in vencidas if v.get("appointment_id")]
    if appointment_ids:
        # Solo toca las que SIGUEN pending_payment: si ya se confirmaron o
        # se cancelaron por otra via mientras tanto, se dejan tal cual.
        supabase.table("appointments").update({"status": "cancelled", "cancelled_by": "system"}).in_(
            "id", appointment_ids
        ).eq("status", "pending_payment").execute()

    return len(vencidas)
