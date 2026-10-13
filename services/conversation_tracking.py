"""
Registro de conversaciones del chat publico (tabla chat_sessions, ver
metricas_analitica.sql), para poder medir conversaciones iniciadas, cuantas
terminaron en cita y cuantas necesitaron a un humano.

Todo es best-effort: la analitica nunca debe tumbar una conversacion, asi que
cualquier falla (incluida la tabla sin migrar) se traga y se imprime.
"""

from datetime import datetime, timezone

from services.db import supabase


def _ahora_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def registrar_turno_conversacion(business_id: str, session_id: str) -> None:
    """Crea la conversacion en su primer mensaje y suma un mensaje en los siguientes."""
    try:
        existente = (
            supabase.table("chat_sessions")
            .select("message_count")
            .eq("business_id", business_id)
            .eq("session_id", session_id)
            .execute()
        )
        if existente.data:
            supabase.table("chat_sessions").update(
                {"message_count": (existente.data[0].get("message_count") or 0) + 1, "last_message_at": _ahora_utc()}
            ).eq("business_id", business_id).eq("session_id", session_id).execute()
        else:
            supabase.table("chat_sessions").insert(
                {"business_id": business_id, "session_id": session_id, "message_count": 1}
            ).execute()
    except Exception as e:
        print(f"No se pudo registrar el turno de la conversacion {business_id}:{session_id}: {e}")


def marcar_conversacion(business_id: str, session_id: str | None, **campos) -> None:
    """
    Marca un resultado de la conversacion (ended_in_appointment +
    appointment_id, o handed_off). Si la conversacion todavia no tiene fila
    (ej. el primer turno ya se perdio), la crea para no perder el resultado.
    """
    if not session_id:
        return
    try:
        actualizadas = (
            supabase.table("chat_sessions")
            .update(campos)
            .eq("business_id", business_id)
            .eq("session_id", session_id)
            .execute()
        )
        if not actualizadas.data:
            supabase.table("chat_sessions").insert(
                {"business_id": business_id, "session_id": session_id, **campos}
            ).execute()
    except Exception as e:
        print(f"No se pudo marcar la conversacion {business_id}:{session_id}: {e}")
