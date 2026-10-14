import os
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from services.auth import requiere_super_admin
from services.db import supabase
from services.invitation_codes import crear_codigo_invitacion
from services.seed_super_admin import _buscar_usuario_por_email

router = APIRouter(prefix="/admin", tags=["super-admin"])


class BusinessCreate(BaseModel):
    name: str
    business_type: str | None = None


@router.post("/businesses")
def crear_negocio(data: BusinessCreate, user_id: str = Depends(requiere_super_admin)):
    """
    Crea un negocio SIN dueño (lo pone el super admin). El dueño real
    queda ligado despues, cuando valide el codigo de invitacion tipo
    'admin' que se genera para este negocio (ver POST
    /admin/businesses/{business_id}/invitation-codes).
    """
    nombre = data.name.strip()
    if not nombre:
        raise HTTPException(status_code=400, detail="El nombre del negocio es obligatorio")

    nuevo = {"name": nombre, "reminder_hours_before": 24}
    if data.business_type and data.business_type.strip():
        nuevo["business_type"] = data.business_type.strip()

    response = supabase.table("businesses").insert(nuevo).execute()
    if not response.data:
        raise HTTPException(status_code=500, detail="No se pudo crear el negocio")
    business = response.data[0]

    # Horario general por defecto (lunes a sabado 9am-7pm, domingo cerrado);
    # el horario real de agendamiento lo define cada empleado cuando se crea.
    try:
        filas_horario = [
            {
                "business_id": business["id"],
                "day": dia,
                "is_open": True,
                "opening_time": "09:00",
                "closing_time": "19:00",
            }
            for dia in ["mon", "tue", "wed", "thu", "fri", "sat"]
        ]
        filas_horario.append({"business_id": business["id"], "day": "sun", "is_open": False})
        supabase.table("business_hours").insert(filas_horario).execute()
    except Exception as e:
        print(f"No se pudo crear el horario por defecto del negocio {business['id']}: {e}")

    return {"business": business}


@router.get("/businesses")
def listar_negocios(user_id: str = Depends(requiere_super_admin)):
    """Lista todos los negocios, para que el super admin los administre y los bloquee si hace falta."""
    response = supabase.table("businesses").select("*").order("created_at", desc=True).execute()
    return {"businesses": response.data}


class BlockedUpdate(BaseModel):
    blocked: bool


@router.put("/businesses/{business_id}/blocked")
def actualizar_bloqueo_negocio(business_id: str, data: BlockedUpdate, user_id: str = Depends(requiere_super_admin)):
    """
    Bloquea o desbloquea un negocio: mientras esta bloqueado, su chat
    publico deja de agendar y el dueño no puede administrarlo desde la
    app (ver services/auth.py:verificar_dueno).
    """
    response = supabase.table("businesses").update({"blocked": data.blocked}).eq("id", business_id).execute()
    if not response.data:
        raise HTTPException(status_code=404, detail="Negocio no encontrado")
    return {"business": response.data[0]}


@router.post("/businesses/{business_id}/invitation-codes")
def crear_codigo_admin(business_id: str, user_id: str = Depends(requiere_super_admin)):
    """
    Genera un codigo de invitacion para que el futuro dueño de este
    negocio lo valide (POST /invitation-codes/redeem) y quede ligado como
    su administrador. Se puede llamar varias veces si el codigo se pierde
    o vence sin usarse; cada llamada genera uno nuevo.
    """
    negocio = supabase.table("businesses").select("id").eq("id", business_id).execute()
    if not negocio.data:
        raise HTTPException(status_code=404, detail="Negocio no encontrado")

    codigo = crear_codigo_invitacion(business_id, role="admin", created_by=user_id)
    return {"invitation_code": codigo}


# ---------------------------------------------------------------------
# Super admins: cuentas de administracion de GoAgenda
# ---------------------------------------------------------------------

_EMAIL_VALIDO = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_LARGO_PASSWORD = 8


class SuperAdminCreate(BaseModel):
    email: str
    password: str


def _emails_por_usuario(user_ids: set[str]) -> dict[str, str]:
    """Busca el email de cada user_id en Supabase Auth (pagina hasta encontrarlos todos)."""
    encontrados: dict[str, str] = {}
    pagina = 1
    while len(encontrados) < len(user_ids):
        usuarios = supabase.auth.admin.list_users(page=pagina, per_page=200)
        if not usuarios:
            break
        for usuario in usuarios:
            if usuario.id in user_ids and usuario.email:
                encontrados[usuario.id] = usuario.email
        if len(usuarios) < 200:
            break
        pagina += 1
    return encontrados


def _es_cuenta_principal(email: str | None) -> bool:
    """
    La cuenta de SUPER_ADMIN_EMAIL se vuelve a sembrar en cada arranque
    (services/seed_super_admin.py), asi que revocarla desde aqui solo duraria
    hasta el siguiente reinicio: se protege para no dar una falsa sensacion.
    """
    principal = (os.getenv("SUPER_ADMIN_EMAIL") or "").strip().lower()
    return bool(principal and email and email.lower() == principal)


@router.get("/super-admins")
def listar_super_admins(user_id: str = Depends(requiere_super_admin)):
    """Lista las cuentas con rol de super admin, con su email, la mas reciente primero."""
    filas = supabase.table("super_admins").select("user_id, created_at").order("created_at", desc=True).execute().data or []
    emails = _emails_por_usuario({f["user_id"] for f in filas})
    return {
        "super_admins": [
            {
                "user_id": f["user_id"],
                "email": emails.get(f["user_id"]),
                "created_at": f.get("created_at"),
                "is_me": f["user_id"] == user_id,
                "is_primary": _es_cuenta_principal(emails.get(f["user_id"])),
            }
            for f in filas
        ]
    }


@router.post("/super-admins")
def crear_super_admin(data: SuperAdminCreate, user_id: str = Depends(requiere_super_admin)):
    """
    Crea una cuenta nueva en Supabase Auth (email ya confirmado, para que
    pueda iniciar sesion de inmediato) y la marca como super admin. No
    promueve cuentas existentes: si el email ya esta registrado (por ejemplo
    como dueño de un negocio) responde 409, para no convertir sin querer a
    un dueño en administrador de toda la plataforma.
    """
    email = data.email.strip().lower()
    if not _EMAIL_VALIDO.match(email):
        raise HTTPException(status_code=400, detail="El email no es valido")
    if len(data.password) < MIN_LARGO_PASSWORD:
        raise HTTPException(status_code=400, detail=f"La contraseña debe tener al menos {MIN_LARGO_PASSWORD} caracteres")

    if _buscar_usuario_por_email(email) is not None:
        raise HTTPException(status_code=409, detail="Ya existe una cuenta con ese email")

    try:
        respuesta = supabase.auth.admin.create_user({"email": email, "password": data.password, "email_confirm": True})
    except Exception as e:
        print(f"No se pudo crear el usuario super admin {email}: {e}")
        raise HTTPException(status_code=400, detail="No se pudo crear la cuenta (revisa el email y la contraseña)")

    nuevo = respuesta.user
    try:
        fila = supabase.table("super_admins").upsert({"user_id": nuevo.id}).execute()
    except Exception as e:
        # No dejar una cuenta huerfana (creada pero sin rol) que luego
        # bloquearia reintentar con el mismo email.
        print(f"No se pudo marcar como super admin a {email}: {e}")
        try:
            supabase.auth.admin.delete_user(nuevo.id)
        except Exception as limpieza:
            print(f"Tampoco se pudo limpiar la cuenta {nuevo.id}: {limpieza}")
        raise HTTPException(status_code=500, detail="No se pudo crear el super admin")

    created_at = fila.data[0].get("created_at") if fila.data else None
    return {"super_admin": {"user_id": nuevo.id, "email": email, "created_at": created_at, "is_me": False, "is_primary": False}}


@router.delete("/super-admins/{target_id}")
def revocar_super_admin(target_id: str, user_id: str = Depends(requiere_super_admin)):
    """
    Quita el rol de super admin a una cuenta. Solo revoca el rol: la cuenta
    de Supabase Auth se conserva (puede seguir existiendo como dueño o
    empleado de un negocio), pero deja de poder usar cualquier endpoint de
    /admin de inmediato, porque el rol se verifica en cada peticion.

    Reglas: nadie puede revocarse a si mismo (asi siempre queda al menos un
    super admin: quien hace la peticion), y la cuenta principal definida en
    SUPER_ADMIN_EMAIL no se puede revocar porque se re-crea al reiniciar.
    """
    if target_id == user_id:
        raise HTTPException(status_code=400, detail="No puedes revocar tu propio acceso")

    existente = supabase.table("super_admins").select("user_id").eq("user_id", target_id).execute()
    if not existente.data:
        raise HTTPException(status_code=404, detail="Esa cuenta no es super admin")

    if _es_cuenta_principal(_emails_por_usuario({target_id}).get(target_id)):
        raise HTTPException(
            status_code=400,
            detail="Esta es la cuenta principal de GoAgenda (definida en la configuracion del servidor) y no se puede revocar",
        )

    supabase.table("super_admins").delete().eq("user_id", target_id).execute()
    return {"revoked": True, "user_id": target_id}


# ---------------------------------------------------------------------
# Presupuesto mensual de IA por negocio
# ---------------------------------------------------------------------

PRESUPUESTO_IA_MAXIMO_USD = 1000


class AiBudgetUpdate(BaseModel):
    monthly_ai_budget_usd: float = Field(gt=0, le=PRESUPUESTO_IA_MAXIMO_USD)


@router.get("/ai-budgets")
def listar_presupuestos_ia(user_id: str = Depends(requiere_super_admin)):
    """
    Presupuesto mensual de IA y gasto del mes de cada negocio, para que el
    super admin los revise y los ajuste (PUT /admin/businesses/{id}/ai-budget).
    `presupuesto_efectivo_usd` es el que realmente aplica: si el negocio no
    tiene uno guardado, rige el valor por defecto.
    """
    from core.settings import AI_PRESUPUESTO_CORTE_DURO
    from services.ai_usage_tracking import PRESUPUESTO_POR_DEFECTO_USD, obtener_gasto_mensual_de_todos

    negocios = supabase.table("businesses").select("id, name, monthly_ai_budget_usd").order("name").execute().data or []
    gasto = obtener_gasto_mensual_de_todos()

    filas = []
    for n in negocios:
        guardado = n.get("monthly_ai_budget_usd")
        efectivo = guardado or PRESUPUESTO_POR_DEFECTO_USD
        gastado = gasto.get(n["id"], 0.0)
        filas.append(
            {
                "business_id": n["id"],
                "name": n.get("name"),
                "presupuesto_guardado_usd": guardado,
                "presupuesto_efectivo_usd": efectivo,
                "gasto_mes_usd": gastado,
                "porcentaje_usado": round(gastado * 100 / efectivo, 1) if efectivo else None,
            }
        )

    return {
        "presupuestos": filas,
        "presupuesto_por_defecto_usd": PRESUPUESTO_POR_DEFECTO_USD,
        "corte_duro_activo": AI_PRESUPUESTO_CORTE_DURO,
    }


@router.put("/businesses/{business_id}/ai-budget")
def actualizar_presupuesto_ia(business_id: str, data: AiBudgetUpdate, user_id: str = Depends(requiere_super_admin)):
    """Fija el presupuesto mensual de IA (USD) de un negocio. Aplica de inmediato al corte duro."""
    from services.ai_usage_tracking import invalidar_cache_presupuesto

    response = (
        supabase.table("businesses")
        .update({"monthly_ai_budget_usd": round(data.monthly_ai_budget_usd, 2)})
        .eq("id", business_id)
        .execute()
    )
    if not response.data:
        raise HTTPException(status_code=404, detail="Negocio no encontrado")

    invalidar_cache_presupuesto(business_id)
    return {"business_id": business_id, "monthly_ai_budget_usd": response.data[0]["monthly_ai_budget_usd"]}
