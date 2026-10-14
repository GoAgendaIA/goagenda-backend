"""
Ejecutor de la evaluacion del asistente.

Corre cada escenario contra el grafo REAL (checkpointer en memoria, tools reales,
Supabase en memoria) con el modelo que haya configurado, mide llamadas, tokens y
costo, evalua las expectativas y arma un reporte JSON comparable entre corridas.

No toca ningun servicio real: Supabase es un doble en memoria, WhatsApp, push,
Wompi y la geocodificacion se reemplazan (ver preparar_entorno).
"""

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from evaluacion import negocios

PATRON_HORA_24H = re.compile(r"\b(?:1[3-9]|2[0-3]):[0-5]\d\b(?!\s?[ap]\.?m)", re.IGNORECASE)
PATRON_PRECIO = re.compile(r"\$\s?(\d{1,3}(?:[.,]\d{3})+|\d+)")
DIRECTORIO_REPORTES = Path(__file__).resolve().parent / "reportes"


# ---------------------------------------------------------------------------------------------------------
# Medicion
# ---------------------------------------------------------------------------------------------------------

class Medidor:
    """Reemplaza a registrar_uso durante la evaluacion: acumula tokens y costo sin tocar la base."""

    def __init__(self, modelo: str | None = None):
        self.modelo = modelo
        self.llamadas = 0
        self.entrada = 0
        self.cacheados = 0
        self.salida = 0
        self.costo = 0.0

    def registrar(self, business_id, prompt_tokens, completion_tokens, total_tokens, cached_tokens=0,
                  tipo="turno", session_id=None, modelo=None):
        from services.ai_usage_tracking import calcular_costo

        costo = calcular_costo(prompt_tokens, completion_tokens, cached_tokens, self.modelo or modelo)
        self.llamadas += 1
        self.entrada += prompt_tokens
        self.cacheados += cached_tokens
        self.salida += completion_tokens
        self.costo += costo
        return costo

    def instantanea(self) -> tuple[int, int, int, int, float]:
        return self.llamadas, self.entrada, self.cacheados, self.salida, self.costo


# ---------------------------------------------------------------------------------------------------------
# Resultado y evaluacion
# ---------------------------------------------------------------------------------------------------------

@dataclass
class Resultado:
    escenario_id: str
    titulo: str
    categoria: str
    respuestas: list[str] = field(default_factory=list)
    herramientas: list[str] = field(default_factory=list)
    citas_nuevas: list[dict] = field(default_factory=list)
    citas: dict[str, dict] = field(default_factory=dict)  # estado final de las citas previas, por id
    llamadas_modelo: int = 0
    tokens_entrada: int = 0
    tokens_cacheados: int = 0
    tokens_salida: int = 0
    costo_usd: float = 0.0
    segundos: float = 0.0
    error: str | None = None
    omitido: bool = False
    fallos: list[str] = field(default_factory=list)

    @property
    def aprobado(self) -> bool:
        return not self.omitido and self.error is None and not self.fallos


def evaluar(escenario, resultado: Resultado, precios_validos: set[int]) -> list[str]:
    """Compara el resultado con lo esperado y devuelve la lista de incumplimientos (vacia = aprobado)."""
    esperar = escenario.esperar
    texto = "\n".join(resultado.respuestas)
    fallos: list[str] = []

    for herramienta in esperar.get("llama", []):
        if herramienta not in resultado.herramientas:
            fallos.append(f"debia llamar {herramienta}")
    for herramienta in esperar.get("no_llama", []):
        if herramienta in resultado.herramientas:
            fallos.append(f"no debia llamar {herramienta}")
    for patron in esperar.get("responde_con", []):
        if not re.search(patron, texto, re.IGNORECASE):
            fallos.append(f"la respuesta debia coincidir con /{patron}/")
    for patron in esperar.get("no_responde_con", []):
        if re.search(patron, texto, re.IGNORECASE):
            fallos.append(f"la respuesta no debia coincidir con /{patron}/")

    if "citas_nuevas" in esperar and len(resultado.citas_nuevas) != esperar["citas_nuevas"]:
        fallos.append(f"citas nuevas: {len(resultado.citas_nuevas)} (esperaba {esperar['citas_nuevas']})")

    if "cita" in esperar:
        if not resultado.citas_nuevas:
            fallos.append("debia crearse una cita y no se creo ninguna")
        else:
            cita = resultado.citas_nuevas[0]
            for campo, valor in esperar["cita"].items():
                real = cita.get(campo)
                coincide = str(real).startswith(valor) if campo == "scheduled_at" else real == valor
                if not coincide:
                    fallos.append(f"cita.{campo} = {real!r} (esperaba {valor!r})")

    for prefijo in esperar.get("sin_cita_nueva_en", []):
        if any(str(c.get("scheduled_at", "")).startswith(prefijo) for c in resultado.citas_nuevas):
            fallos.append(f"se creo una cita en {prefijo}, que no debia")

    for cita_id, estado in esperar.get("estado_cita", {}).items():
        real = (resultado.citas.get(cita_id) or {}).get("status")
        if real != estado:
            fallos.append(f"estado de {cita_id}: {real!r} (esperaba {estado!r})")

    for cita_id, prefijo in esperar.get("cita_programada_en", {}).items():
        real = (resultado.citas.get(cita_id) or {}).get("scheduled_at")
        if not str(real or "").startswith(prefijo):
            fallos.append(f"fecha de {cita_id}: {real!r} (esperaba {prefijo}...)")

    if "max_llamadas" in esperar and resultado.llamadas_modelo > esperar["max_llamadas"]:
        fallos.append(f"{resultado.llamadas_modelo} llamadas al modelo (maximo {esperar['max_llamadas']})")

    if escenario.invariantes:
        if PATRON_HORA_24H.search(texto):
            fallos.append("invariante: mostro una hora en formato 24 h")
        for crudo in PATRON_PRECIO.findall(texto):
            valor = int(re.sub(r"[.,]", "", crudo))
            if valor not in precios_validos:
                fallos.append(f"invariante: precio inventado ${valor:,}".replace(",", "."))

    return fallos


# ---------------------------------------------------------------------------------------------------------
# Entorno
# ---------------------------------------------------------------------------------------------------------

def preparar_entorno(monkeypatch) -> None:
    """
    Congela la hora y reemplaza todo lo que saldria a un servicio real. Debe
    llamarse dentro de una prueba (usa el fixture monkeypatch de pytest).
    """
    from agent import graph, tools
    from services import appointment_confirmation, realtime, scheduling, wompi_payment_requests

    reloj = lambda: negocios.AHORA  # noqa: E731
    monkeypatch.setattr(scheduling, "ahora_local", reloj)
    monkeypatch.setattr(tools, "ahora_local", reloj)
    monkeypatch.setattr(graph, "ahora_local", reloj)

    monkeypatch.setattr(tools, "geocodificar", lambda direccion: None)
    monkeypatch.setattr(tools, "enviar_notificacion_escalamiento", lambda *a, **k: None)
    monkeypatch.setattr(tools.gestor_tiempo_real, "emitir", lambda *a, **k: None)
    monkeypatch.setattr(realtime, "emitir_evento_cita", lambda *a, **k: None)
    monkeypatch.setattr(appointment_confirmation, "enviar_notificacion_nueva_cita", lambda *a, **k: None)
    monkeypatch.setattr(appointment_confirmation, "enviar_confirmacion_cita_cliente", lambda *a, **k: None)
    monkeypatch.setattr(
        wompi_payment_requests,
        "crear_solicitud_pago",
        lambda **kw: {"checkout_url": negocios.LINK_DE_PAGO, "amount_in_cents": negocios.ABONO_CENTAVOS,
                      "description": "Abono"},
    )


def _herramientas_de_la_sesion(business_id: str, session_id: str) -> list[str]:
    from langchain_core.messages import AIMessage

    from agent import graph

    estado = graph.GRAPH.get_state(graph._thread_config(business_id, session_id))
    mensajes = (estado.values or {}).get("messages", [])
    return [tc["name"] for m in mensajes if isinstance(m, AIMessage) for tc in (m.tool_calls or [])]


# ---------------------------------------------------------------------------------------------------------
# Ejecucion
# ---------------------------------------------------------------------------------------------------------

def ejecutar_escenario(escenario, db, enviar_mensaje, medidor: Medidor, herramientas_de=_herramientas_de_la_sesion) -> Resultado:
    """
    Siembra el negocio del escenario en `db`, escribe sus turnos con
    `enviar_mensaje(business_id, session_id, texto)` y devuelve el Resultado
    ya evaluado.
    """
    import uuid

    db.reset()
    datos = negocios.sembrar(db, escenario.preset)
    for previa in escenario.citas_previas:
        extra = {k: v for k, v in previa.items() if k not in ("id", "telefono", "nombre", "scheduled_at")}
        negocios.cita_previa(db, datos, id=previa["id"], telefono=previa["telefono"], nombre=previa["nombre"],
                             scheduled_at=previa["scheduled_at"], **extra)
    ids_previos = {c["id"] for c in db.filas("appointments")}

    resultado = Resultado(escenario_id=escenario.id, titulo=escenario.titulo, categoria=escenario.categoria)
    session_id = str(uuid.uuid4())
    antes = medidor.instantanea()
    inicio = time.monotonic()

    try:
        for turno in escenario.turnos:
            respuesta, _opciones = enviar_mensaje(datos["business_id"], session_id, turno)
            resultado.respuestas.append(respuesta)
        resultado.herramientas = herramientas_de(datos["business_id"], session_id)
    except Exception as e:  # una falla del modelo o de la red no debe tumbar toda la corrida
        resultado.error = f"{type(e).__name__}: {e}"

    despues = medidor.instantanea()
    resultado.segundos = round(time.monotonic() - inicio, 2)
    resultado.llamadas_modelo = despues[0] - antes[0]
    resultado.tokens_entrada = despues[1] - antes[1]
    resultado.tokens_cacheados = despues[2] - antes[2]
    resultado.tokens_salida = despues[3] - antes[3]
    resultado.costo_usd = round(despues[4] - antes[4], 6)

    todas = db.filas("appointments")
    resultado.citas_nuevas = [dict(c) for c in todas if c["id"] not in ids_previos]
    resultado.citas = {c["id"]: dict(c) for c in todas if c["id"] in ids_previos}

    if resultado.error is None:
        resultado.fallos = evaluar(escenario, resultado, datos["precios_validos"])
    return resultado


def tope_alcanzado(medidor: Medidor, tope_usd: float | None) -> bool:
    return tope_usd is not None and medidor.costo >= tope_usd


def ejecutar_todos(escenarios, db, enviar_mensaje, medidor: Medidor, tope_usd: float | None = None,
                   herramientas_de=_herramientas_de_la_sesion) -> list[Resultado]:
    """Corre los escenarios en orden y deja de gastar al alcanzar el tope (el resto queda como omitido)."""
    resultados = []
    for escenario in escenarios:
        if tope_alcanzado(medidor, tope_usd):
            resultados.append(omitido(escenario))
            continue
        resultados.append(ejecutar_escenario(escenario, db, enviar_mensaje, medidor, herramientas_de))
    return resultados


def omitido(escenario) -> Resultado:
    return Resultado(escenario_id=escenario.id, titulo=escenario.titulo, categoria=escenario.categoria, omitido=True)


# ---------------------------------------------------------------------------------------------------------
# Reporte
# ---------------------------------------------------------------------------------------------------------

def resumir(resultados: list[Resultado], etiqueta: str, modelo: str, configuracion: dict, tope_usd: float | None = None) -> dict:
    ejecutados = [r for r in resultados if not r.omitido]
    aprobados = [r for r in ejecutados if r.aprobado]
    entrada = sum(r.tokens_entrada for r in ejecutados)
    cacheados = sum(r.tokens_cacheados for r in ejecutados)
    return {
        "etiqueta": etiqueta,
        "generado_en": datetime.now().isoformat(timespec="seconds"),
        "modelo": modelo,
        "configuracion": configuracion,
        "tope_usd": tope_usd,
        "totales": {
            "escenarios": len(resultados),
            "ejecutados": len(ejecutados),
            "omitidos_por_tope": len(resultados) - len(ejecutados),
            "aprobados": len(aprobados),
            "acierto": round(len(aprobados) / len(ejecutados), 3) if ejecutados else None,
            "llamadas_modelo": sum(r.llamadas_modelo for r in ejecutados),
            "tokens_entrada": entrada,
            "tokens_cacheados": cacheados,
            "porcentaje_cache": round(cacheados * 100 / entrada, 1) if entrada else None,
            "tokens_salida": sum(r.tokens_salida for r in ejecutados),
            "costo_usd": round(sum(r.costo_usd for r in ejecutados), 4),
            "llamadas_por_escenario": round(sum(r.llamadas_modelo for r in ejecutados) / len(ejecutados), 1) if ejecutados else None,
        },
        "por_categoria": _por_categoria(ejecutados),
        "escenarios": [asdict(r) | {"aprobado": r.aprobado} for r in resultados],
    }


def _por_categoria(ejecutados: list[Resultado]) -> dict:
    categorias: dict[str, dict] = {}
    for r in ejecutados:
        c = categorias.setdefault(r.categoria, {"escenarios": 0, "aprobados": 0})
        c["escenarios"] += 1
        c["aprobados"] += 1 if r.aprobado else 0
    return categorias


def guardar_reporte(reporte: dict, directorio: Path = DIRECTORIO_REPORTES) -> Path:
    directorio.mkdir(parents=True, exist_ok=True)
    etiqueta = re.sub(r"[^A-Za-z0-9_-]+", "-", reporte["etiqueta"]) or "corrida"
    ruta = directorio / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{etiqueta}.json"
    ruta.write_text(json.dumps(reporte, ensure_ascii=False, indent=2), encoding="utf-8")
    return ruta


def comparar(base: dict, nuevo: dict) -> dict:
    """Diferencias entre dos reportes (p. ej. antes y despues de activar prompt v2)."""

    def pct(a, b):
        return round((b - a) * 100 / a, 1) if a else None

    tb, tn = base["totales"], nuevo["totales"]
    fallos_base = {e["escenario_id"] for e in base["escenarios"] if not e["aprobado"] and not e["omitido"]}
    fallos_nuevo = {e["escenario_id"] for e in nuevo["escenarios"] if not e["aprobado"] and not e["omitido"]}
    return {
        "acierto": {"antes": tb["acierto"], "despues": tn["acierto"]},
        "tokens_entrada": {"antes": tb["tokens_entrada"], "despues": tn["tokens_entrada"],
                           "variacion_pct": pct(tb["tokens_entrada"], tn["tokens_entrada"])},
        "costo_usd": {"antes": tb["costo_usd"], "despues": tn["costo_usd"], "variacion_pct": pct(tb["costo_usd"], tn["costo_usd"])},
        "llamadas_modelo": {"antes": tb["llamadas_modelo"], "despues": tn["llamadas_modelo"]},
        "regresiones": sorted(fallos_nuevo - fallos_base),
        "mejoras": sorted(fallos_base - fallos_nuevo),
    }
