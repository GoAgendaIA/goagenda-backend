"""
Centro de metricas y analitica de GoAgenda (panel global del super admin y
panel individual por negocio). Ver docs/METRICAS_ANALITICA.md.

Diseño: se trae UNA vez (paginado) el historial de citas del alcance pedido y
todo se calcula en Python sobre esas filas. El panel global y el individual
usan las mismas funciones, asi que "global = suma de los negocios" se cumple
por construccion y no hay dos formulas que puedan divergir.

Definiciones (las que el PDF pide dejar claras):
- Por que fecha se filtra: lo que mide USO de la plataforma (citas creadas,
  origen IA/manual, estados, evolucion, clientes nuevos, conversaciones, costo
  de IA) se filtra por `created_at` (fecha en que se creo la cita). Lo que mide
  RENDIMIENTO del negocio (servicios, dias/horas de demanda, domicilios,
  ingresos) se filtra por `scheduled_at` (fecha en que la cita ocurre).
- Dia/semana/mes se calculan en hora de Colombia (TZ_NEGOCIO), no en UTC.
- Cliente unico: el telefono normalizado (solo digitos, sin el 57 inicial).
- Una reprogramacion es la MISMA cita (misma fila): no suma a "citas
  creadas"; se cuenta aparte con rescheduled_count > 0.
- Citas con status 'cancelled' y cancelled_by 'system' son bloqueos de pago
  vencidos (el cliente nunca pago): no son citas reales y se reportan aparte
  en `citas.pagos_vencidos`, fuera de totales, clientes y tasas.
- Citas anteriores a la migracion tienen source 'unknown': se muestran como
  "desconocido", nunca se asumen manuales ni de la IA.
"""

import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from services.db import supabase
from services.scheduling import TZ_NEGOCIO, ahora_local

PAGINA = 1000
DIAS_POR_DEFECTO = 30
MAX_DIAS_RANGO = 366
INACTIVIDAD_DIAS_POR_DEFECTO = 14
TTL_CACHE_SEGUNDOS = 60

ESTADOS = ["pending", "pending_payment", "confirmed", "completed", "cancelled"]
ORIGENES = ["ai", "manual", "web", "unknown"]
DIAS_SEMANA = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]

# Umbrales de las alertas de la Fase 3 (ajustables en un solo lugar).
UMBRAL_CANCELACION = 0.30
UMBRAL_CONVERSION_BAJA = 0.10
MIN_CITAS_PARA_TASA = 10
MIN_CONVERSACIONES_PARA_TASA = 10
DIAS_SIN_CONFIGURAR = 7

_cache: dict[tuple, tuple[float, dict]] = {}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _traer_todo(construir_consulta) -> list[dict]:
    """
    PostgREST corta cada respuesta en 1000 filas. Pagina con range() hasta
    agotar el resultado; sin esto las metricas se truncaban en silencio.
    """
    filas: list[dict] = []
    desde = 0
    while True:
        pagina = construir_consulta().range(desde, desde + PAGINA - 1).execute().data or []
        filas.extend(pagina)
        if len(pagina) < PAGINA:
            return filas
        desde += PAGINA


def _a_local(valor) -> datetime | None:
    """Convierte un timestamp de la BD a datetime naive en hora de Colombia."""
    if not valor:
        return None
    try:
        dt = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(TZ_NEGOCIO).replace(tzinfo=None)
    return dt


def _clave_cliente(telefono: str | None) -> str | None:
    """Telefono normalizado: solo digitos y sin el indicativo 57, para no duplicar clientes."""
    digitos = "".join(c for c in (telefono or "") if c.isdigit())
    if len(digitos) > 10 and digitos.startswith("57"):
        digitos = digitos[2:]
    return digitos or None


def _porcentaje(parte: int | float, total: int | float) -> float | None:
    return round(parte * 100 / total, 1) if total else None


def _es_pago_vencido(cita: dict) -> bool:
    return cita.get("status") == "cancelled" and cita.get("cancelled_by") == "system"


def _bucket(dt: datetime, group_by: str) -> str:
    if group_by == "month":
        return dt.strftime("%Y-%m")
    if group_by == "week":
        lunes = dt.date() - timedelta(days=dt.weekday())
        return lunes.isoformat()
    return dt.date().isoformat()


def resolver_periodo(desde: date | None, hasta: date | None) -> tuple[date, date]:
    hoy = ahora_local().date()
    hasta = hasta or hoy
    desde = desde or (hasta - timedelta(days=DIAS_POR_DEFECTO - 1))
    if desde > hasta:
        raise ValueError("'desde' no puede ser posterior a 'hasta'")
    if (hasta - desde).days + 1 > MAX_DIAS_RANGO:
        raise ValueError(f"El rango maximo es de {MAX_DIAS_RANGO} dias")
    return desde, hasta


def _periodo_anterior(desde: date, hasta: date) -> tuple[date, date]:
    largo = (hasta - desde).days + 1
    return desde - timedelta(days=largo), desde - timedelta(days=1)


def _en_rango(dt: datetime | None, desde: date, hasta: date) -> bool:
    return dt is not None and desde <= dt.date() <= hasta


# ---------------------------------------------------------------------------
# Lectura de datos
# ---------------------------------------------------------------------------

_COLUMNAS_CITA = (
    "id, business_id, client_phone, service_id, employee_id, status, source, created_at, "
    "scheduled_at, is_home_visit, home_visit_fee, rescheduled_count, rescheduled_by_ai_count, cancelled_by"
)


def _citas(business_id: str | None) -> list[dict]:
    def consulta():
        q = supabase.table("appointments").select(_COLUMNAS_CITA).order("created_at")
        return q.eq("business_id", business_id) if business_id else q

    return _traer_todo(consulta)


def _conversaciones(business_id: str | None) -> list[dict]:
    def consulta():
        q = supabase.table("chat_sessions").select(
            "business_id, session_id, started_at, ended_in_appointment, handed_off"
        ).order("started_at")
        return q.eq("business_id", business_id) if business_id else q

    return _traer_todo(consulta)


def _uso_ia(business_id: str | None) -> list[dict]:
    def consulta():
        # select("*"): las columnas de tokens (tokens_uso_ia.sql) pueden no existir todavia.
        q = supabase.table("ai_usage_log").select("*").order("created_at")
        return q.eq("business_id", business_id) if business_id else q

    return _traer_todo(consulta)


def _servicios(business_id: str | None) -> list[dict]:
    def consulta():
        q = supabase.table("services").select("id, business_id, name, price, active").order("name")
        return q.eq("business_id", business_id) if business_id else q

    return _traer_todo(consulta)


# ---------------------------------------------------------------------------
# Calculo por periodo
# ---------------------------------------------------------------------------

def _resumen_periodo(
    citas: list[dict],
    conversaciones: list[dict],
    uso_ia: list[dict],
    servicios: dict[str, dict],
    desde: date,
    hasta: date,
    group_by: str,
    inactividad_dias: int,
) -> dict:
    """Todas las metricas de un negocio (o de toda la plataforma) para un periodo."""
    ahora = ahora_local()
    vencidos = [c for c in citas if _es_pago_vencido(c)]
    reales = [c for c in citas if not _es_pago_vencido(c)]

    # --- Uso de la plataforma: por fecha de creacion -----------------------
    del_periodo = [c for c in reales if _en_rango(_a_local(c.get("created_at")), desde, hasta)]
    por_estado = Counter(c.get("status") for c in del_periodo)
    total = len(del_periodo)
    cancelaciones = por_estado.get("cancelled", 0)
    completadas = por_estado.get("completed", 0)

    por_origen = Counter(c.get("source") or "unknown" for c in del_periodo)
    origen = {
        o: {"total": por_origen.get(o, 0), "porcentaje": _porcentaje(por_origen.get(o, 0), total)}
        for o in ORIGENES
    }
    citas_ia = por_origen.get("ai", 0)

    evolucion_total: dict[str, int] = defaultdict(int)
    evolucion_ia: dict[str, int] = defaultdict(int)
    evolucion_manual: dict[str, int] = defaultdict(int)
    for c in del_periodo:
        clave = _bucket(_a_local(c["created_at"]), group_by)
        evolucion_total[clave] += 1
        if c.get("source") == "ai":
            evolucion_ia[clave] += 1
        elif c.get("source") == "manual":
            evolucion_manual[clave] += 1
    evolucion = [
        {"periodo": k, "total": evolucion_total[k], "ia": evolucion_ia[k], "manual": evolucion_manual[k]}
        for k in sorted(evolucion_total)
    ]

    # --- Clientes ----------------------------------------------------------
    por_cliente: dict[str, list[dict]] = defaultdict(list)
    for c in reales:
        clave = _clave_cliente(c.get("client_phone"))
        if clave:
            por_cliente[clave].append(c)

    nuevos = 0
    recurrentes = 0
    sin_volver = 0
    corte_inactividad = ahora - timedelta(days=inactividad_dias)
    clientes_del_periodo = set()
    for clave, lista in por_cliente.items():
        creadas = [_a_local(c.get("created_at")) for c in lista]
        creadas = [d for d in creadas if d]
        if creadas and _en_rango(min(creadas), desde, hasta):
            nuevos += 1
        validas = [c for c in lista if c.get("status") != "cancelled"]
        if len(validas) >= 2:
            recurrentes += 1
        if any(_en_rango(_a_local(c.get("created_at")), desde, hasta) for c in lista):
            clientes_del_periodo.add(clave)
        programadas = [_a_local(c.get("scheduled_at")) for c in validas]
        programadas = [d for d in programadas if d]
        # "No volvio": ya tuvo citas, la ultima paso hace mas de N dias y no tiene una futura.
        if programadas and max(programadas) < corte_inactividad:
            sin_volver += 1

    # --- IA: conversaciones y costo ---------------------------------------
    convs = [s for s in conversaciones if _en_rango(_a_local(s.get("started_at")), desde, hasta)]
    con_cita = sum(1 for s in convs if s.get("ended_in_appointment"))
    transferidas = sum(1 for s in convs if s.get("handed_off"))
    uso_todo = [u for u in uso_ia if _en_rango(_a_local(u.get("created_at")), desde, hasta)]
    # Las respuestas sin modelo (T4.1) dejan una fila de 0 tokens: no son llamadas al modelo.
    sin_modelo = sum(1 for u in uso_todo if str(u.get("call_type") or "").startswith("sin_modelo"))
    uso_periodo = [u for u in uso_todo if not str(u.get("call_type") or "").startswith("sin_modelo")]
    costo = round(sum(float(u.get("estimated_cost_usd") or 0) for u in uso_periodo), 6)
    tokens_entrada = sum(int(u.get("prompt_tokens") or 0) for u in uso_periodo)
    tokens_cacheados = sum(int(u.get("cached_tokens") or 0) for u in uso_periodo)
    tokens_salida = sum(int(u.get("completion_tokens") or 0) for u in uso_periodo)
    re_llamadas = sum(1 for u in uso_periodo if u.get("call_type") == "re_llamada_anti_alucinacion")

    # --- Rendimiento: por fecha programada --------------------------------
    programadas_periodo = [c for c in reales if _en_rango(_a_local(c.get("scheduled_at")), desde, hasta)]
    activas = [c for c in programadas_periodo if c.get("status") != "cancelled"]
    por_servicio: Counter = Counter(c.get("service_id") for c in activas)
    top_servicios = [
        {
            "service_id": sid,
            "nombre": (servicios.get(sid) or {}).get("name", "Servicio eliminado"),
            "total": n,
            "porcentaje": _porcentaje(n, len(activas)),
        }
        for sid, n in por_servicio.most_common(10)
    ]
    por_dia = [0] * 7
    por_hora: Counter = Counter()
    for c in activas:
        dt = _a_local(c.get("scheduled_at"))
        if dt:
            por_dia[dt.weekday()] += 1
            por_hora[dt.hour] += 1
    domicilios = sum(1 for c in activas if c.get("is_home_visit"))
    ingresos_completadas = 0.0
    recargos_domicilio = 0.0
    # Nadie marca las citas como "completed" (no hay endpoint ni flujo que lo haga), asi que
    # el estado no sirve para saber si una cita ya se realizo. Se decide por la FECHA: una cita
    # no cancelada cuya hora ya paso es un ingreso realizado (de las del periodo elegido).
    for c in programadas_periodo:
        if c.get("status") == "cancelled":
            continue
        # programadas_periodo ya descarto las fechas vacias o invalidas.
        if c.get("status") == "completed" or _a_local(c.get("scheduled_at")) < ahora:
            ingresos_completadas += float((servicios.get(c.get("service_id")) or {}).get("price") or 0)
            recargos_domicilio += float(c.get("home_visit_fee") or 0)

    # Por realizar: TODAS las citas futuras (confirmadas, pendientes o con pago pendiente),
    # sin importar el periodo elegido. Es lo que esta por venir a partir de ahora; si se
    # limitara al periodo, un rango que termina hoy (el habitual) mostraba siempre $0.
    ingresos_por_realizar = 0.0
    citas_por_realizar = 0
    for c in reales:
        if c.get("status") not in ("confirmed", "pending", "pending_payment"):
            continue
        programada = _a_local(c.get("scheduled_at"))
        if programada is None or programada < ahora:
            continue
        citas_por_realizar += 1
        ingresos_por_realizar += float((servicios.get(c.get("service_id")) or {}).get("price") or 0)

    return {
        "tarjetas": {
            "citas_creadas": total,
            "clientes_unicos": len(clientes_del_periodo),
            "citas_ia": citas_ia,
            "citas_manuales": por_origen.get("manual", 0),
        },
        "clientes": {
            "total_registrados": len(por_cliente),
            "nuevos_en_periodo": nuevos,
            "recurrentes": recurrentes,
            "sin_volver": {"dias": inactividad_dias, "total": sin_volver},
        },
        "citas": {
            "total": total,
            "por_estado": {e: por_estado.get(e, 0) for e in ESTADOS},
            "reprogramadas": sum(1 for c in del_periodo if (c.get("rescheduled_count") or 0) > 0),
            "tasa_cancelacion": _porcentaje(cancelaciones, total),
            "porcentaje_completadas": _porcentaje(completadas, total),
            "pagos_vencidos": sum(1 for c in vencidos if _en_rango(_a_local(c.get("created_at")), desde, hasta)),
            "evolucion": evolucion,
        },
        "ia": {
            "por_origen": origen,
            "porcentaje_citas_ia": _porcentaje(citas_ia, total),
            "canceladas_por_ia": sum(1 for c in del_periodo if c.get("status") == "cancelled" and c.get("cancelled_by") == "ai"),
            "reprogramadas_por_ia": sum(1 for c in del_periodo if (c.get("rescheduled_by_ai_count") or 0) > 0),
            "conversaciones": {
                "iniciadas": len(convs),
                "con_cita": con_cita,
                "transferidas_a_humano": transferidas,
                "conversion": _porcentaje(con_cita, len(convs)),
            },
            "costo_usd": costo,
            "costo_por_cita_ia_usd": round(costo / citas_ia, 4) if citas_ia else None,
            # Optimizacion de tokens (T0.1): linea base para medir cada ahorro.
            "tokens": {
                "llamadas": len(uso_periodo),
                "entrada": tokens_entrada,
                "cacheados": tokens_cacheados,
                "salida": tokens_salida,
                "porcentaje_cache": _porcentaje(tokens_cacheados, tokens_entrada),
                "re_llamadas_anti_alucinacion": re_llamadas,
                "respuestas_sin_modelo": sin_modelo,
                "llamadas_por_conversacion": round(len(uso_periodo) / len(convs), 1) if convs else None,
                "tokens_entrada_por_conversacion": round(tokens_entrada / len(convs)) if convs else None,
                "costo_por_conversacion_usd": round(costo / len(convs), 4) if convs else None,
            },
        },
        "rendimiento": {
            "servicios_mas_agendados": top_servicios,
            "citas_por_dia_semana": [{"dia": DIAS_SEMANA[i], "total": n} for i, n in enumerate(por_dia)],
            "citas_por_hora": [{"hora": h, "total": por_hora[h]} for h in sorted(por_hora)],
            "domicilios": {"total": domicilios, "porcentaje": _porcentaje(domicilios, len(activas))},
            "ingresos_estimados": {
                "completadas": round(ingresos_completadas, 2),
                "por_realizar": round(ingresos_por_realizar, 2),
                "citas_por_realizar": citas_por_realizar,
                "recargos_domicilio": round(recargos_domicilio, 2),
            },
        },
    }


def _comparar(actual: dict, anterior: dict) -> dict:
    """Deltas simples entre dos resumenes (valor anterior, valor actual, variacion absoluta y %)."""

    def delta(a, b):
        if a is None or b is None:
            return {"actual": a, "anterior": b, "variacion": None, "variacion_pct": None}
        pct = round((a - b) * 100 / b, 1) if b else None
        return {"actual": a, "anterior": b, "variacion": round(a - b, 2), "variacion_pct": pct}

    return {
        "citas_creadas": delta(actual["tarjetas"]["citas_creadas"], anterior["tarjetas"]["citas_creadas"]),
        "clientes_unicos": delta(actual["tarjetas"]["clientes_unicos"], anterior["tarjetas"]["clientes_unicos"]),
        "citas_ia": delta(actual["tarjetas"]["citas_ia"], anterior["tarjetas"]["citas_ia"]),
        "citas_manuales": delta(actual["tarjetas"]["citas_manuales"], anterior["tarjetas"]["citas_manuales"]),
        "tasa_cancelacion": delta(actual["citas"]["tasa_cancelacion"], anterior["citas"]["tasa_cancelacion"]),
        "conversion": delta(
            actual["ia"]["conversaciones"]["conversion"], anterior["ia"]["conversaciones"]["conversion"]
        ),
        "costo_ia_usd": delta(actual["ia"]["costo_usd"], anterior["ia"]["costo_usd"]),
    }


# ---------------------------------------------------------------------------
# Actividad de negocios (adopcion) y alertas
# ---------------------------------------------------------------------------

def _estado_de_negocio(ultima_actividad: datetime | None, ahora: datetime, inactividad_dias: int) -> str:
    if ultima_actividad is None:
        return "sin_uso"
    if ultima_actividad >= ahora - timedelta(days=inactividad_dias):
        return "activo"
    return "inactivo"


def _fila_negocio(
    negocio: dict,
    citas: list[dict],
    convs: list[dict],
    servicios_activos: int,
    tiene_horario: bool,
    resumen: dict,
    inactividad_dias: int,
) -> dict:
    ahora = ahora_local()
    reales = [c for c in citas if not _es_pago_vencido(c)]
    fechas = [d for d in (_a_local(c.get("created_at")) for c in reales) if d]
    fechas += [d for d in (_a_local(s.get("started_at")) for s in convs) if d]
    ultima = max(fechas) if fechas else None
    registro = _a_local(negocio.get("created_at"))

    primera_cita = min((d for d in (_a_local(c.get("created_at")) for c in reales) if d), default=None)
    dias_hasta_primera = (primera_cita - registro).days if primera_cita and registro else None

    hace_4_semanas = ahora - timedelta(days=28)
    por_semana: Counter = Counter()
    for d in (_a_local(c.get("created_at")) for c in reales):
        if d and d >= hace_4_semanas:
            por_semana[(ahora - d).days // 7] += 1

    ia_total = sum(1 for c in reales if c.get("source") == "ai")
    usa_ia = ia_total > 0 or len(convs) > 0

    return {
        "business_id": negocio["id"],
        "nombre": negocio.get("name"),
        "registrado_en": negocio.get("created_at"),
        "bloqueado": bool(negocio.get("blocked")),
        "onboarding_completado": negocio.get("onboarding_completed", True),
        "estado": _estado_de_negocio(ultima, ahora, inactividad_dias),
        "ultima_actividad": ultima.isoformat() if ultima else None,
        "configuracion": {"servicios_activos": servicios_activos, "tiene_horario": tiene_horario},
        "usa_ia": usa_ia,
        "citas_en_periodo": resumen["citas"]["total"],
        "citas_ia_en_periodo": resumen["tarjetas"]["citas_ia"],
        "citas_manuales_en_periodo": resumen["tarjetas"]["citas_manuales"],
        "porcentaje_citas_ia": resumen["ia"]["porcentaje_citas_ia"],
        "tasa_cancelacion": resumen["citas"]["tasa_cancelacion"],
        "conversion": resumen["ia"]["conversaciones"]["conversion"],
        "costo_ia_usd": resumen["ia"]["costo_usd"],
        "dias_hasta_primera_cita": dias_hasta_primera,
        # Semana 0 = ultimos 7 dias, 1 = los 7 anteriores, etc. (uso semanal)
        "citas_por_semana": [por_semana.get(i, 0) for i in range(4)],
        "alertas": _alertas_de_negocio(negocio, registro, ahora, ultima, servicios_activos, tiene_horario, len(reales), resumen, inactividad_dias),
    }


def _alertas_de_negocio(
    negocio: dict,
    registro: datetime | None,
    ahora: datetime,
    ultima: datetime | None,
    servicios_activos: int,
    tiene_horario: bool,
    citas_historicas: int,
    resumen: dict,
    inactividad_dias: int,
) -> list[dict]:
    """Fase 3: reglas simples y explicables, cada una con la accion recomendada."""
    alertas: list[dict] = []
    antiguo = registro is not None and registro < ahora - timedelta(days=DIAS_SIN_CONFIGURAR)

    def alerta(tipo: str, severidad: str, mensaje: str, recomendacion: str):
        alertas.append({"tipo": tipo, "severidad": severidad, "mensaje": mensaje, "recomendacion": recomendacion})

    if antiguo and (servicios_activos == 0 or not tiene_horario):
        alerta(
            "sin_configurar", "alta",
            "Lleva mas de una semana registrado y no termino de configurar servicios u horarios.",
            "Contactar al negocio para acompañarlo en la configuracion inicial.",
        )
    elif antiguo and citas_historicas == 0:
        alerta(
            "sin_primera_cita", "media",
            "Esta configurado pero nunca ha recibido una cita.",
            "Ayudarle a compartir su enlace de chat con sus clientes.",
        )
    elif ultima is not None and ultima < ahora - timedelta(days=inactividad_dias):
        alerta(
            "inactivo", "alta",
            f"Sin actividad hace mas de {inactividad_dias} dias.",
            "Contactar al negocio para entender si dejo de usar GoAgenda y por que.",
        )

    total = resumen["citas"]["total"]
    tasa = resumen["citas"]["tasa_cancelacion"]
    if total >= MIN_CITAS_PARA_TASA and tasa is not None and tasa / 100 >= UMBRAL_CANCELACION:
        alerta(
            "cancelacion_alta", "media",
            f"{tasa}% de las citas del periodo se cancelaron.",
            "Revisar recordatorios y politica de abonos; considerar exigir pago para los servicios mas cancelados.",
        )

    convs = resumen["ia"]["conversaciones"]
    if (
        convs["iniciadas"] >= MIN_CONVERSACIONES_PARA_TASA
        and convs["conversion"] is not None
        and convs["conversion"] / 100 < UMBRAL_CONVERSION_BAJA
    ):
        alerta(
            "conversion_baja", "media",
            f"Solo {convs['conversion']}% de las conversaciones terminan en cita.",
            "Revisar servicios, horarios disponibles y el flujo del asistente para este negocio.",
        )

    if total >= MIN_CITAS_PARA_TASA and resumen["tarjetas"]["citas_ia"] == 0 and resumen["tarjetas"]["citas_manuales"] > 0:
        alerta(
            "solo_manual", "baja",
            "Agenda solo de forma manual: no esta aprovechando el asistente.",
            "Mostrarle como compartir el enlace del chat para que sus clientes agenden solos.",
        )

    if resumen["ia"]["costo_usd"] > 0 and resumen["tarjetas"]["citas_ia"] == 0:
        alerta(
            "ia_sin_citas", "media",
            "Genera costo de IA sin producir citas en el periodo.",
            "Revisar las conversaciones del negocio: pueden ser consultas sin intencion de agendar o fallas del flujo.",
        )

    return alertas


# ---------------------------------------------------------------------------
# API publica del modulo
# ---------------------------------------------------------------------------

def _cacheado(clave: tuple, refrescar: bool, calcular) -> dict:
    ahora = time.monotonic()
    if not refrescar and clave in _cache and ahora - _cache[clave][0] < TTL_CACHE_SEGUNDOS:
        return {**_cache[clave][1], "cache": True}
    resultado = calcular()
    _cache[clave] = (ahora, resultado)
    return {**resultado, "cache": False}


def limpiar_cache() -> None:
    _cache.clear()


def _encabezado(desde: date, hasta: date, group_by: str, comparar: bool) -> dict:
    return {
        "periodo": {
            "desde": desde.isoformat(),
            "hasta": hasta.isoformat(),
            "anterior": dict(zip(("desde", "hasta"), map(date.isoformat, _periodo_anterior(desde, hasta)))) if comparar else None,
            "agrupado_por": group_by,
        },
        "criterios": {
            "uso": "fecha de creacion de la cita",
            "rendimiento": "fecha programada de la cita",
            "zona_horaria": "America/Bogota",
            "reprogramaciones": "misma cita; se cuenta aparte, no como cita nueva",
            "pagos_vencidos": "excluidos de totales, clientes y tasas",
        },
        "generado_en": datetime.now(timezone.utc).isoformat(),
        "actualizacion_segundos": TTL_CACHE_SEGUNDOS,
    }


def _dict_por_id(filas: list[dict]) -> dict[str, dict]:
    return {f["id"]: f for f in filas}


def metricas_negocio(
    business_id: str,
    desde: date,
    hasta: date,
    group_by: str = "day",
    comparar: bool = False,
    inactividad_dias: int = INACTIVIDAD_DIAS_POR_DEFECTO,
    refrescar: bool = False,
) -> dict | None:
    """Metricas de UN negocio. Retorna None si el negocio no existe."""

    negocio = supabase.table("businesses").select("*").eq("id", business_id).execute().data
    if not negocio:
        return None

    def calcular():
        citas = _citas(business_id)
        convs = _conversaciones(business_id)
        uso = _uso_ia(business_id)
        servicios = _dict_por_id(_servicios(business_id))
        horario = (
            supabase.table("business_hours").select("is_open").eq("business_id", business_id).eq("is_open", True).execute().data
        )
        resumen = _resumen_periodo(citas, convs, uso, servicios, desde, hasta, group_by, inactividad_dias)
        resultado = {
            **_encabezado(desde, hasta, group_by, comparar),
            "negocio": _fila_negocio(
                negocio[0], citas, convs,
                sum(1 for s in servicios.values() if s.get("active")), bool(horario),
                resumen, inactividad_dias,
            ),
            **resumen,
        }
        if comparar:
            d2, h2 = _periodo_anterior(desde, hasta)
            anterior = _resumen_periodo(citas, convs, uso, servicios, d2, h2, group_by, inactividad_dias)
            resultado["comparacion"] = _comparar(resumen, anterior)
        return resultado

    return _cacheado(("negocio", business_id, desde, hasta, group_by, comparar, inactividad_dias), refrescar, calcular)


def metricas_globales(
    desde: date,
    hasta: date,
    group_by: str = "day",
    comparar: bool = False,
    inactividad_dias: int = INACTIVIDAD_DIAS_POR_DEFECTO,
    refrescar: bool = False,
) -> dict:
    """Metricas consolidadas de toda la plataforma + adopcion + detalle por negocio + alertas."""

    def calcular():
        negocios = _traer_todo(lambda: supabase.table("businesses").select("*").order("created_at"))
        citas = _citas(None)
        convs = _conversaciones(None)
        uso = _uso_ia(None)
        servicios_todos = _servicios(None)
        servicios = _dict_por_id(servicios_todos)
        horarios = _traer_todo(
            lambda: supabase.table("business_hours").select("business_id, is_open").eq("is_open", True)
        )
        negocios_con_horario = {h["business_id"] for h in horarios}

        resumen = _resumen_periodo(citas, convs, uso, servicios, desde, hasta, group_by, inactividad_dias)

        def por_negocio(filas):
            agrupado = defaultdict(list)
            for f in filas:
                agrupado[f.get("business_id")].append(f)
            return agrupado

        citas_n, convs_n, uso_n = por_negocio(citas), por_negocio(convs), por_negocio(uso)
        servicios_n = por_negocio(servicios_todos)

        detalle = []
        for n in negocios:
            bid = n["id"]
            svc = {s["id"]: s for s in servicios_n.get(bid, [])}
            r = _resumen_periodo(citas_n.get(bid, []), convs_n.get(bid, []), uso_n.get(bid, []), svc, desde, hasta, group_by, inactividad_dias)
            detalle.append(
                _fila_negocio(
                    n, citas_n.get(bid, []), convs_n.get(bid, []),
                    sum(1 for s in svc.values() if s.get("active")), bid in negocios_con_horario,
                    r, inactividad_dias,
                )
            )

        estados = Counter(f["estado"] for f in detalle)
        tiempos = sorted(f["dias_hasta_primera_cita"] for f in detalle if f["dias_hasta_primera_cita"] is not None)
        mitad = len(tiempos) // 2
        mediana = None
        if tiempos:
            mediana = tiempos[mitad] if len(tiempos) % 2 else round((tiempos[mitad - 1] + tiempos[mitad]) / 2, 1)

        registrados = len(detalle)
        adopcion = {
            "negocios_registrados": registrados,
            "negocios_activos": estados.get("activo", 0),
            "negocios_inactivos": estados.get("inactivo", 0),
            "negocios_sin_uso": estados.get("sin_uso", 0),
            "porcentaje_activos": _porcentaje(estados.get("activo", 0), registrados),
            "con_servicios": sum(1 for f in detalle if f["configuracion"]["servicios_activos"] > 0),
            "con_horario": sum(1 for f in detalle if f["configuracion"]["tiene_horario"]),
            "usan_ia": sum(1 for f in detalle if f["usa_ia"]),
            "solo_agendamiento_manual": sum(
                1 for f in detalle if not f["usa_ia"] and f["estado"] != "sin_uso"
            ),
            "dias_hasta_primera_cita": {"mediana": mediana, "negocios_medidos": len(tiempos)},
            "dejaron_de_usar": estados.get("inactivo", 0),
            "inactividad_dias": inactividad_dias,
        }

        alertas = [
            {"business_id": f["business_id"], "nombre": f["nombre"], **a}
            for f in detalle
            if not f["bloqueado"]
            for a in f["alertas"]
        ]
        orden = {"alta": 0, "media": 1, "baja": 2}
        alertas.sort(key=lambda a: orden.get(a["severidad"], 3))

        resultado = {
            **_encabezado(desde, hasta, group_by, comparar),
            "adopcion": adopcion,
            "negocios": detalle,
            "alertas": alertas,
            **resumen,
        }
        if comparar:
            d2, h2 = _periodo_anterior(desde, hasta)
            anterior = _resumen_periodo(citas, convs, uso, servicios, d2, h2, group_by, inactividad_dias)
            resultado["comparacion"] = _comparar(resumen, anterior)
        return resultado

    return _cacheado(("global", desde, hasta, group_by, comparar, inactividad_dias), refrescar, calcular)
