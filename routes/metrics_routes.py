from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from services import metrics
from services.auth import requiere_super_admin

router = APIRouter(prefix="/admin/metrics", tags=["metricas"])


def _periodo(desde: date | None, hasta: date | None) -> tuple[date, date]:
    try:
        return metrics.resolver_periodo(desde, hasta)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/global")
def metricas_globales(
    desde: date | None = Query(default=None, description="Inicio del periodo (YYYY-MM-DD). Default: hace 30 dias."),
    hasta: date | None = Query(default=None, description="Fin del periodo, inclusive. Default: hoy (hora de Colombia)."),
    group_by: Literal["day", "week", "month"] = "day",
    comparar: bool = Query(default=False, description="Compara contra el periodo inmediatamente anterior de igual duracion."),
    inactividad_dias: int = Query(default=metrics.INACTIVIDAD_DIAS_POR_DEFECTO, ge=1, le=365),
    refrescar: bool = Query(default=False, description="Ignora la cache de 60 s."),
    user_id: str = Depends(requiere_super_admin),
):
    """
    Panel global: metricas consolidadas de todos los negocios, adopcion de la
    plataforma, detalle por negocio y alertas. Solo super admin.
    """
    desde, hasta = _periodo(desde, hasta)
    return metrics.metricas_globales(desde, hasta, group_by, comparar, inactividad_dias, refrescar)


@router.get("/businesses/{business_id}")
def metricas_de_negocio(
    business_id: str,
    desde: date | None = None,
    hasta: date | None = None,
    group_by: Literal["day", "week", "month"] = "day",
    comparar: bool = False,
    inactividad_dias: int = Query(default=metrics.INACTIVIDAD_DIAS_POR_DEFECTO, ge=1, le=365),
    refrescar: bool = False,
    user_id: str = Depends(requiere_super_admin),
):
    """Panel individual: solo las metricas del negocio pedido. Solo super admin."""
    desde, hasta = _periodo(desde, hasta)
    resultado = metrics.metricas_negocio(business_id, desde, hasta, group_by, comparar, inactividad_dias, refrescar)
    if resultado is None:
        raise HTTPException(status_code=404, detail="Negocio no encontrado")
    return resultado
