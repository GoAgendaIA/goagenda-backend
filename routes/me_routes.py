from fastapi import APIRouter, Depends

from services.auth import es_super_admin, obtener_usuario_actual
from services.db import supabase

router = APIRouter(tags=["auth"])


@router.get("/me")
def obtener_mi_sesion(user_id: str = Depends(obtener_usuario_actual)):
    """
    Punto de entrada para que la app sepa quien es el usuario recien
    logueado: si es super admin, y en que negocio(s) tiene un registro de
    empleado (el dueño tambien aparece aqui, con role='owner', porque es
    su "empleado principal"). Se llama justo despues de iniciar sesion
    para decidir a que pantalla mandarlo (super admin, completar registro
    con un codigo, panel de dueño, o vista de empleado).
    """
    empleos = (
        supabase.table("employees")
        .select("*, businesses(*)")
        .eq("user_id", user_id)
        .execute()
    )
    return {
        "user_id": user_id,
        "is_super_admin": es_super_admin(user_id),
        "employments": [
            {
                "employee_id": e["id"],
                "business_id": e["business_id"],
                "business_name": (e.get("businesses") or {}).get("name"),
                "business_blocked": (e.get("businesses") or {}).get("blocked"),
                "business_onboarding_completed": (e.get("businesses") or {}).get("onboarding_completed", True),
                "business_onboarding_step": (e.get("businesses") or {}).get("onboarding_step", 1),
                "business_home_visits_enabled": bool((e.get("businesses") or {}).get("home_visits_enabled", False)),
                # True si ya vio el aviso de estadisticas. Sin la columna (estadisticas_intro.sql sin
                # correr) se asume visto, para no mostrar un aviso que no se podria recordar.
                "stats_intro_seen": bool(e.get("stats_intro_seen", True)),
                "name": e.get("name"),
                "role": e["role"],
                "active": e["active"],
            }
            for e in empleos.data
        ],
    }
