"""
Estadisticas del propio negocio para su dueño (panel «Estadisticas»).

Reutiliza el calculo del Centro de metricas (services/metrics.py) pero entrega solo lo que le
sirve al dueño para decidir: citas, ingresos, clientes, cancelaciones, servicios y horas de
demanda, y lo que aporta el asistente. NO incluye datos internos de la plataforma (costo de IA,
tokens, presupuesto, adopcion). El acceso es solo del dueño del negocio.
"""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from services import metrics
from services.auth import obtener_usuario_actual, verificar_dueno
from services.db import supabase

router = APIRouter(tags=["estadisticas"])

# Lo unico de la comparacion que sale al dueño: el costo de IA es un dato interno de GoAgenda.
_COMPARACION_PUBLICA = ("citas_creadas", "clientes_unicos", "citas_ia", "citas_manuales", "tasa_cancelacion", "conversion")


def _para_el_dueno(resultado: dict) -> dict:
    """Lista blanca de lo que se entrega: lo que no esta aqui no sale, aunque el calculo lo traiga."""
    ia = resultado["ia"]
    comparacion = resultado.get("comparacion")
    return {
        "periodo": resultado["periodo"],
        "generado_en": resultado["generado_en"],
        "tarjetas": resultado["tarjetas"],
        "clientes": resultado["clientes"],
        "citas": resultado["citas"],
        "asistente": {
            "porcentaje_citas_ia": ia["porcentaje_citas_ia"],
            "por_origen": ia["por_origen"],
            "canceladas_por_ia": ia["canceladas_por_ia"],
            "reprogramadas_por_ia": ia["reprogramadas_por_ia"],
            "conversaciones": ia["conversaciones"],
        },
        "rendimiento": resultado["rendimiento"],
        "comparacion": (
            {clave: comparacion[clave] for clave in _COMPARACION_PUBLICA if clave in comparacion} if comparacion else None
        ),
    }


@router.get("/business-stats")
def estadisticas_del_negocio(
    business_id: str,
    desde: date | None = Query(default=None, description="Inicio del periodo (YYYY-MM-DD). Default: hace 30 dias."),
    hasta: date | None = Query(default=None, description="Fin del periodo, inclusive. Default: hoy (hora de Colombia)."),
    group_by: Literal["day", "week", "month"] = "day",
    comparar: bool = Query(default=False, description="Compara contra el periodo anterior de igual duracion."),
    refrescar: bool = False,
    user_id: str = Depends(obtener_usuario_actual),
):
    """Estadisticas del negocio para su dueño. Solo el dueño (un empleado recibe 403)."""
    verificar_dueno(business_id, user_id)
    try:
        desde, hasta = metrics.resolver_periodo(desde, hasta)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # verificar_dueno ya garantizo que el negocio existe (404 si no), asi que aqui nunca es None.
    resultado = metrics.metricas_negocio(business_id, desde, hasta, group_by, comparar, refrescar=refrescar)
    return _para_el_dueno(resultado)


class StatsIntroSeen(BaseModel):
    business_id: str


@router.put("/business-settings/stats-intro-seen")
def marcar_intro_de_estadisticas_vista(data: StatsIntroSeen, user_id: str = Depends(obtener_usuario_actual)):
    """
    Marca que este usuario ya vio el aviso de «Ahora podras tomar el control de tu negocio con el
    panel de estadisticas». Se guarda en el servidor (no solo en el navegador) para que no vuelva a
    salir en otro dispositivo. Best-effort: si la columna aun no existe (estadisticas_intro.sql sin
    correr), responde guardado=false y el aviso simplemente no se mostrara.
    """
    verificar_dueno(data.business_id, user_id)
    try:
        supabase.table("employees").update({"stats_intro_seen": True}).eq("business_id", data.business_id).eq(
            "user_id", user_id
        ).execute()
    except Exception as e:
        print(f"No se pudo guardar que el aviso de estadisticas ya se vio ({data.business_id}): {e}")
        return {"stats_intro_seen": True, "guardado": False}
    return {"stats_intro_seen": True, "guardado": True}
