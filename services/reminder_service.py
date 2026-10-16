from datetime import datetime, timedelta
from services.db import supabase
from services.notificaciones_cliente import datos_para_plantilla, nombre_del_empleado
from services.whatsapp import enviar_recordatorio_cita_template
from services.scheduling import ahora_local, formatear_fecha_natural


def revisar_y_enviar_recordatorios():
    """
    Se ejecuta periodicamente (cada pocos minutos). Busca citas confirmadas
    cuyo momento de recordatorio ya llego (segun reminder_hours_before del negocio)
    y que aun no se les ha enviado el recordatorio, y lo manda por WhatsApp.
    """
    # scheduled_at se guarda en hora de Colombia (naive) y el servidor corre en
    # UTC: comparar contra datetime.now() corria la ventana 5 horas.
    ahora = ahora_local()
    print(f"[recordatorios] inicio ciclo ahora={ahora:%Y-%m-%d %H:%M}", flush=True)

    # Trae todos los negocios con su configuracion de recordatorio
    negocios_response = supabase.table("businesses").select("id, name, reminder_hours_before, address, address_city").execute()
    negocios = negocios_response.data

    total_enviados = 0
    total_candidatas = 0

    for negocio in negocios:
        business_id = negocio["id"]
        nombre_negocio = negocio["name"]
        horas_antes = negocio.get("reminder_hours_before") or 24

        # Ventana: citas que caen entre AHORA y AHORA + horas_antes,
        # que aun no han recibido recordatorio, y siguen confirmadas.
        limite_superior = ahora + timedelta(hours=horas_antes)

        try:
            citas_response = (
                supabase.table("appointments")
                .select("*, services(name)")
                .eq("business_id", business_id)
                .eq("status", "confirmed")
                .eq("reminder_sent", False)
                .gte("scheduled_at", ahora.isoformat())
                .lte("scheduled_at", limite_superior.isoformat())
                .execute()
            )
        except Exception as e:
            print(f"[recordatorios] error consultando citas de {nombre_negocio}: {e}", flush=True)
            continue

        total_candidatas += len(citas_response.data)

        for cita in citas_response.data:
            if not cita.get("client_phone"):
                # Cita manual de un cliente sin WhatsApp (ver
                # routes/manual_appointments.py) - no hay a donde mandar el
                # recordatorio. Se deja reminder_sent=False (no se "envio"
                # nada de verdad) pero se salta sin llamar la API de
                # WhatsApp ni contarla como fallo.
                continue
            try:
                fecha_cita = datetime.fromisoformat(cita["scheduled_at"])
                nombre_cliente = cita.get("client_name") or "Cliente"
                nombre_servicio = cita.get("services", {}).get("name", "tu cita") if cita.get("services") else "tu cita"

                resultado_envio = enviar_recordatorio_cita_template(
                    to=cita["client_phone"],
                    nombre_cliente=nombre_cliente,
                    nombre_negocio=nombre_negocio,
                    nombre_servicio=nombre_servicio,
                    fecha_hora_texto=formatear_fecha_natural(fecha_cita),
                    datos_plantilla=datos_para_plantilla(negocio, fecha_cita, nombre_del_empleado(cita.get("employee_id"))),
                )

                # Solo marcamos como enviado si WhatsApp confirmo el envio (sin campo "error")
                if isinstance(resultado_envio, dict) and "error" in resultado_envio:
                    print(f"Fallo el envio de recordatorio para cita {cita['id']}: {resultado_envio['error']}")
                    continue

                supabase.table("appointments").update({"reminder_sent": True}).eq("id", cita["id"]).execute()

                total_enviados += 1
                print(f"Recordatorio enviado a {cita['client_phone']} para cita {cita['id']}")

            except Exception as e:
                print(f"Error enviando recordatorio para cita {cita.get('id')}: {e}")

    print(f"[recordatorios] ahora={ahora:%Y-%m-%d %H:%M} negocios={len(negocios)} citas_en_ventana={total_candidatas} enviados={total_enviados}")

    if total_enviados > 0:
        print(f"Revision de recordatorios completada. Enviados: {total_enviados}")

    return total_enviados