"""
Politica de retencion de las conversaciones del chat publico.

Lo que contiene datos personales (nombre y WhatsApp del cliente) es el HISTORIAL de cada
conversacion, que vive en los checkpoints de LangGraph (tablas checkpoints,
checkpoint_blobs y checkpoint_writes de Postgres, un hilo por `<business_id>:<session_id>`).
Este modulo elimina cada hilo cuando pasan CHAT_RETENCION_DIAS dias desde su ULTIMO
movimiento. Es por conversacion: la de un cliente se borra sin tocar la de otro.

NO se borra nada mas: ni citas, ni reservas, ni pagos, ni la tabla chat_sessions (que solo
tiene contadores y fechas, sin contenido, y alimenta las metricas de conversion).

Efecto para el cliente: si vuelve despues del plazo con el mismo navegador, su historial
aparece vacio y el asistente empieza de cero (vuelve a pedirle el WhatsApp).
"""

from datetime import datetime, timedelta, timezone

from core.settings import CHAT_RETENCION_DIAS


def _a_utc(valor) -> datetime | None:
    """ISO o datetime -> datetime con zona UTC; None si no se puede interpretar."""
    if isinstance(valor, str):
        try:
            valor = datetime.fromisoformat(valor.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(valor, datetime):
        return None
    return valor.replace(tzinfo=timezone.utc) if valor.tzinfo is None else valor.astimezone(timezone.utc)


def _es_postgres(checkpointer) -> bool:
    return type(checkpointer).__module__.startswith("langgraph.checkpoint.postgres")


def ultima_actividad_por_hilo() -> dict[str, datetime]:
    """
    {thread_id: momento del ultimo checkpoint}. En produccion (Postgres) se agrega en la base
    sin traer el contenido de ninguna conversacion; con otro checkpointer (pruebas) se recorre
    list(), que trae lo mismo con mas costo.
    """
    from agent import graph

    ultimos: dict[str, datetime] = {}
    if _es_postgres(graph._checkpointer):
        with graph._pool.connection() as conexion:
            filas = conexion.execute(
                "SELECT thread_id, max((checkpoint->>'ts')::timestamptz) AS ultima_actividad "
                "FROM checkpoints GROUP BY thread_id"
            ).fetchall()
        for fila in filas:
            momento = _a_utc(fila["ultima_actividad"])
            if momento:
                ultimos[str(fila["thread_id"])] = momento
        return ultimos

    for tupla in graph._checkpointer.list(None):
        hilo = str(tupla.config["configurable"]["thread_id"])
        momento = _a_utc((tupla.checkpoint or {}).get("ts"))
        if momento and (hilo not in ultimos or momento > ultimos[hilo]):
            ultimos[hilo] = momento
    return ultimos


def eliminar_hilo(thread_id: str) -> None:
    from agent import graph

    graph._checkpointer.delete_thread(thread_id)


def limpiar_conversaciones_vencidas(dias: int | None = None, ahora: datetime | None = None, dry_run: bool = False) -> dict:
    """
    Elimina las conversaciones cuya ultima actividad supero `dias` (por defecto
    CHAT_RETENCION_DIAS). Con dry_run=True solo cuenta, sin borrar. Una falla al borrar un
    hilo se registra y no detiene a los demas. Devuelve el resumen de la corrida.
    """
    dias = CHAT_RETENCION_DIAS if dias is None else dias
    if dias <= 0:
        return {"activa": False, "retencion_dias": dias, "dry_run": dry_run, "conversaciones": 0,
                "vencidas": 0, "eliminadas": 0, "fallidas": 0}

    limite = (_a_utc(ahora) if ahora else datetime.now(timezone.utc)) - timedelta(days=dias)
    ultimos = ultima_actividad_por_hilo()
    vencidos = sorted(hilo for hilo, momento in ultimos.items() if momento < limite)

    eliminadas = fallidas = 0
    if not dry_run:
        for hilo in vencidos:
            try:
                eliminar_hilo(hilo)
                eliminadas += 1
            except Exception as e:
                fallidas += 1
                print(f"[retencion] no se pudo eliminar la conversacion {hilo}: {e}", flush=True)

    resumen = {"activa": True, "retencion_dias": dias, "dry_run": dry_run, "conversaciones": len(ultimos),
               "vencidas": len(vencidos), "eliminadas": eliminadas, "fallidas": fallidas}
    print(f"[retencion] {resumen}", flush=True)
    return resumen
