import time
from datetime import datetime
from services.db import supabase
from services.push_notifications import enviar_notificacion_nueva_cita, enviar_notificacion_presupuesto_agotado

# Precios por millon de tokens (entrada, entrada cacheada, salida). Los de la
# familia gpt-4.1 son los publicados al elegir el modelo; la pagina de precios
# actual de OpenAI no mostro estas filas, asi que conviene confirmarlos en
# https://platform.openai.com/docs/pricing y agregar aqui cualquier modelo nuevo.
PRECIOS_POR_MILLON = {
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1": (2.00, 0.50, 8.00),
    "gpt-4.1-nano": (0.10, 0.025, 0.40),
}
MODELO_PRECIO_POR_DEFECTO = "gpt-4.1-mini"
_modelos_sin_precio_avisados: set[str] = set()


def _precios(modelo: str | None) -> tuple[float, float, float]:
    if modelo is None:
        from core.settings import CHAT_MODEL_NAME

        modelo = CHAT_MODEL_NAME
    if modelo in PRECIOS_POR_MILLON:
        return PRECIOS_POR_MILLON[modelo]
    if modelo not in _modelos_sin_precio_avisados:
        _modelos_sin_precio_avisados.add(modelo)
        print(f"[ai_usage] sin precio para el modelo '{modelo}': se usa el de {MODELO_PRECIO_POR_DEFECTO}. Agregalo a PRECIOS_POR_MILLON.")
    return PRECIOS_POR_MILLON[MODELO_PRECIO_POR_DEFECTO]


def calcular_costo(prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0, modelo: str | None = None) -> float:
    """
    Costo estimado de una llamada. prompt_tokens es el TOTAL de entrada (como lo
    reporta OpenAI); de ahi, cached_tokens se cobran a la tarifa de cache y el
    resto a la tarifa normal.
    """
    precio_entrada, precio_cache, precio_salida = _precios(modelo)
    cacheados = max(0, min(cached_tokens, prompt_tokens))
    costo_entrada = ((prompt_tokens - cacheados) / 1_000_000) * precio_entrada
    costo_cache = (cacheados / 1_000_000) * precio_cache
    costo_salida = (completion_tokens / 1_000_000) * precio_salida
    return round(costo_entrada + costo_cache + costo_salida, 6)


def registrar_uso(
    business_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    cached_tokens: int = 0,
    tipo: str = "turno",
    session_id: str | None = None,
    modelo: str | None = None,
) -> float:
    """
    Guarda en ai_usage_log el consumo de una llamada al modelo de IA.
    Retorna el costo estimado de esta llamada especifica.

    Ademas de los tokens, guarda cuantos fueron cacheados, el tipo de llamada
    ("turno" o "re_llamada_anti_alucinacion"), la conversacion y el modelo: es
    lo que permite medir costo por conversacion y por cita (T0.1). Si la
    migracion tokens_uso_ia.sql aun no se corrio, cae al formato anterior en
    vez de perder el registro.
    """
    if modelo is None:
        from core.settings import CHAT_MODEL_NAME

        modelo = CHAT_MODEL_NAME
    costo = calcular_costo(prompt_tokens, completion_tokens, cached_tokens, modelo)

    fila = {
        "business_id": business_id,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": costo,
    }
    try:
        supabase.table("ai_usage_log").insert(
            {**fila, "cached_tokens": cached_tokens, "call_type": tipo, "session_id": session_id, "model": modelo}
        ).execute()
    except Exception:
        try:
            supabase.table("ai_usage_log").insert(fila).execute()
        except Exception as e:
            print(f"Error guardando registro de uso de IA: {e}")

    _verificar_presupuesto(business_id)
    return costo


_cache_agotado: dict[str, tuple[float, bool]] = {}
TTL_PRESUPUESTO_SEGUNDOS = 60


def presupuesto_agotado(business: dict) -> bool:
    """
    True si el negocio ya gasto su presupuesto mensual de IA y el corte duro
    esta activo (AI_PRESUPUESTO_CORTE_DURO). El resultado se cachea 60 s por
    negocio para no sumar ai_usage_log en cada mensaje.
    """
    from core.settings import AI_PRESUPUESTO_CORTE_DURO

    if not AI_PRESUPUESTO_CORTE_DURO:
        return False

    business_id = business["id"]
    ahora = time.monotonic()
    guardado = _cache_agotado.get(business_id)
    if guardado and ahora - guardado[0] < TTL_PRESUPUESTO_SEGUNDOS:
        return guardado[1]

    presupuesto = business.get("monthly_ai_budget_usd") or 5.00
    gasto = obtener_gasto_mensual(business_id)
    agotado = gasto >= presupuesto
    _cache_agotado[business_id] = (ahora, agotado)
    if agotado:
        _avisar_corte_al_dueno(business, gasto, presupuesto)
    return agotado


_cortes_avisados: set[tuple[str, str]] = set()


def _avisar_corte_al_dueno(business: dict, gasto: float, presupuesto: float) -> None:
    """
    Push al dueño, UNA vez por mes, cuando el asistente se pausa por presupuesto.
    La marca persistente vive en businesses.ai_budget_cutoff_sent_month
    (tokens_uso_ia.sql); si esa columna aun no existe, se usa un registro en
    memoria (como maximo un aviso por reinicio y por mes) en vez de fallar.
    """
    mes = datetime.now().strftime("%Y-%m")
    business_id = business["id"]
    if business.get("ai_budget_cutoff_sent_month") == mes or (business_id, mes) in _cortes_avisados:
        return
    _cortes_avisados.add((business_id, mes))

    try:
        enviar_notificacion_presupuesto_agotado(
            business.get("fcm_token"), f"US$ {gasto:.2f}", f"US$ {presupuesto:.2f}"
        )
    except Exception as e:
        print(f"Error avisando del corte de presupuesto a {business_id}: {e}")

    try:
        supabase.table("businesses").update({"ai_budget_cutoff_sent_month": mes}).eq("id", business_id).execute()
    except Exception as e:
        print(f"No se pudo guardar la marca del aviso de corte ({business_id}): {e}")


def limpiar_cache_presupuesto() -> None:
    _cache_agotado.clear()
    _cortes_avisados.clear()


def obtener_gasto_mensual(business_id: str) -> float:
    """Suma el costo estimado de todas las llamadas del mes actual para un negocio."""
    inicio_mes = datetime.now().strftime("%Y-%m-01")
    response = (
        supabase.table("ai_usage_log")
        .select("estimated_cost_usd")
        .eq("business_id", business_id)
        .gte("created_at", inicio_mes)
        .execute()
    )
    return round(sum(row["estimated_cost_usd"] or 0 for row in response.data), 4)


PRESUPUESTO_POR_DEFECTO_USD = 5.00


def obtener_gasto_mensual_de_todos() -> dict[str, float]:
    """
    Gasto estimado del mes actual por negocio, en una sola pasada (para el panel
    del super admin). Pagina de a 1000 filas: PostgREST corta ahi cada respuesta.
    """
    inicio_mes = datetime.now().strftime("%Y-%m-01")
    gasto: dict[str, float] = {}
    desde = 0
    while True:
        filas = (
            supabase.table("ai_usage_log")
            .select("business_id, estimated_cost_usd")
            .gte("created_at", inicio_mes)
            .order("created_at")
            .range(desde, desde + 999)
            .execute()
            .data
            or []
        )
        for fila in filas:
            gasto[fila["business_id"]] = gasto.get(fila["business_id"], 0.0) + float(fila.get("estimated_cost_usd") or 0)
        if len(filas) < 1000:
            return {k: round(v, 4) for k, v in gasto.items()}
        desde += 1000


def invalidar_cache_presupuesto(business_id: str) -> None:
    """Para que un cambio de presupuesto desde el panel aplique de inmediato (sin esperar el TTL)."""
    _cache_agotado.pop(business_id, None)


def _verificar_presupuesto(business_id: str):
    """
    Si el gasto del mes ya supera el 80% del presupuesto configurado,
    envia una notificacion push UNA SOLA VEZ por mes al dueño del negocio.
    """
    business_response = (
        supabase.table("businesses")
        .select("monthly_ai_budget_usd, ai_budget_alert_sent_month, fcm_token, name")
        .eq("id", business_id)
        .execute()
    )
    if not business_response.data:
        return

    business = business_response.data[0]
    presupuesto = business.get("monthly_ai_budget_usd") or 5.00
    mes_actual = datetime.now().strftime("%Y-%m")

    if business.get("ai_budget_alert_sent_month") == mes_actual:
        return  # ya se avisó este mes, no repetir

    gasto_actual = obtener_gasto_mensual(business_id)

    if gasto_actual >= presupuesto * 0.8:
        fcm_token = business.get("fcm_token")
        if fcm_token:
            try:
                enviar_notificacion_nueva_cita(
                    fcm_token=fcm_token,
                    nombre_cliente="Alerta de presupuesto",
                    servicio=f"Ya gastaste ${gasto_actual} USD de tu presupuesto mensual de ${presupuesto} USD en IA",
                    fecha_hora_texto="este mes",
                )
            except Exception as e:
                print(f"Error enviando alerta de presupuesto: {e}")

        supabase.table("businesses").update({
            "ai_budget_alert_sent_month": mes_actual
        }).eq("id", business_id).execute()