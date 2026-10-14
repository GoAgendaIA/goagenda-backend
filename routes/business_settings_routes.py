from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from services.db import supabase
from services.auth import obtener_usuario_actual, verificar_acceso_negocio, verificar_dueno

router = APIRouter(tags=["business-settings"])


class BusinessInfoUpdate(BaseModel):
    business_id: str
    name: str
    phone_number: str | None = None


@router.put("/business-settings/info")
def update_business_info(data: BusinessInfoUpdate, user_id: str = Depends(obtener_usuario_actual)):
    """Actualiza el nombre y telefono del negocio."""
    verificar_dueno(data.business_id, user_id)
    update_fields = {"name": data.name}
    if data.phone_number is not None:
        update_fields["phone_number"] = data.phone_number

    response = (
        supabase.table("businesses")
        .update(update_fields)
        .eq("id", data.business_id)
        .execute()
    )
    if not response.data:
        raise HTTPException(status_code=404, detail="Negocio no encontrado")
    return {"business": response.data[0]}


DIRECCION_MAX_CARACTERES = 300
TIPOS_DE_DIRECCION = ("formal", "referencias")


class BusinessAddressUpdate(BaseModel):
    business_id: str
    address: str
    address_kind: str = "formal"
    address_city: str | None = None
    address_notes: str | None = None
    address_lat: float | None = None
    address_lng: float | None = None


def _texto_opcional(valor: str | None, maximo: int, campo: str) -> str | None:
    limpio = (valor or "").strip()
    if len(limpio) > maximo:
        raise HTTPException(status_code=400, detail=f"{campo} no puede pasar de {maximo} caracteres")
    return limpio or None


@router.put("/business-settings/address")
def update_business_address(data: BusinessAddressUpdate, user_id: str = Depends(obtener_usuario_actual)):
    """
    Guarda la direccion del negocio. Es texto libre a proposito: solo se exige que
    no este vacia. Se acepta con nomenclatura ("Calle 10 # 5-20 local 3") o por
    referencias ("Vereda La Cita, despues del puente"), sin validar contra ningun
    mapa; las coordenadas del pin son opcionales y van en pareja.
    """
    verificar_dueno(data.business_id, user_id)

    direccion = (data.address or "").strip()
    if not direccion:
        raise HTTPException(status_code=400, detail="La direccion del negocio es obligatoria")
    if len(direccion) > DIRECCION_MAX_CARACTERES:
        raise HTTPException(status_code=400, detail=f"La direccion no puede pasar de {DIRECCION_MAX_CARACTERES} caracteres")
    if data.address_kind not in TIPOS_DE_DIRECCION:
        raise HTTPException(status_code=400, detail="El tipo de direccion debe ser 'formal' o 'referencias'")
    if (data.address_lat is None) != (data.address_lng is None):
        raise HTTPException(status_code=400, detail="La ubicacion del mapa necesita latitud y longitud juntas")
    if data.address_lat is not None and not (-90 <= data.address_lat <= 90 and -180 <= data.address_lng <= 180):
        raise HTTPException(status_code=400, detail="La ubicacion del mapa esta fuera de rango")

    campos = {
        "address": direccion,
        "address_kind": data.address_kind,
        "address_city": _texto_opcional(data.address_city, 120, "El municipio o barrio"),
        "address_notes": _texto_opcional(data.address_notes, DIRECCION_MAX_CARACTERES, "Las indicaciones"),
        "address_lat": data.address_lat,
        "address_lng": data.address_lng,
    }
    response = supabase.table("businesses").update(campos).eq("id", data.business_id).execute()
    if not response.data:
        raise HTTPException(status_code=404, detail="Negocio no encontrado")
    return {"business": response.data[0]}


class FcmTokenUpdate(BaseModel):
    business_id: str
    fcm_token: str


@router.put("/business-settings/fcm-token")
def update_fcm_token(data: FcmTokenUpdate, user_id: str = Depends(obtener_usuario_actual)):
    """Guarda o actualiza el token de notificaciones push del dispositivo del negocio."""
    verificar_dueno(data.business_id, user_id)
    response = (
        supabase.table("businesses")
        .update({"fcm_token": data.fcm_token})
        .eq("id", data.business_id)
        .execute()
    )
    if not response.data:
        raise HTTPException(status_code=404, detail="Negocio no encontrado")
    return {"business": response.data[0]}


class ReminderConfigUpdate(BaseModel):
    business_id: str
    reminder_hours_before: float


@router.get("/business-settings")
def get_business_settings(business_id: str, user_id: str = Depends(obtener_usuario_actual)):
    """
    Trae la configuracion general del negocio (incluye horas de recordatorio).
    Lectura permitida al dueño y a cualquier empleado activo (ej. para que un
    empleado vea el WhatsApp del negocio en su propio enlace de chat).
    """
    verificar_acceso_negocio(business_id, user_id)
    response = supabase.table("businesses").select("*").eq("id", business_id).execute()
    if not response.data:
        raise HTTPException(status_code=404, detail="Negocio no encontrado")
    return {"business": response.data[0]}


@router.put("/business-settings/reminder")
def update_reminder_config(data: ReminderConfigUpdate, user_id: str = Depends(obtener_usuario_actual)):
    """Actualiza cuantas horas antes de la cita se debe enviar el recordatorio."""
    verificar_dueno(data.business_id, user_id)
    if data.reminder_hours_before < 0 or data.reminder_hours_before > 168:
        raise HTTPException(status_code=400, detail="Las horas deben estar entre 0 y 168 (1 semana)")

    response = (
        supabase.table("businesses")
        .update({"reminder_hours_before": data.reminder_hours_before})
        .eq("id", data.business_id)
        .execute()
    )
    if not response.data:
        raise HTTPException(status_code=404, detail="Negocio no encontrado")
    return {"business": response.data[0]}


class OnboardingStepUpdate(BaseModel):
    business_id: str
    step: int


@router.put("/business-settings/onboarding-step")
def update_onboarding_step(data: OnboardingStepUpdate, user_id: str = Depends(obtener_usuario_actual)):
    """Guarda en que paso del onboarding va el negocio, para poder retomarlo si cierra la sesion antes de terminar."""
    verificar_dueno(data.business_id, user_id)
    response = (
        supabase.table("businesses")
        .update({"onboarding_step": data.step})
        .eq("id", data.business_id)
        .execute()
    )
    if not response.data:
        raise HTTPException(status_code=404, detail="Negocio no encontrado")
    return {"business": response.data[0]}


class OnboardingCompleteInput(BaseModel):
    business_id: str


@router.post("/business-settings/onboarding/complete")
def complete_onboarding(data: OnboardingCompleteInput, user_id: str = Depends(obtener_usuario_actual)):
    """
    Activa el negocio al terminar el onboarding: habilita el chat publico y
    desbloquea el resto del panel. Valida server-side (no confia solo en el
    estado del wizard en el frontend) que ya haya al menos un servicio
    activo, al menos un dia de horario activo y la direccion del negocio,
    igual que exigen los pasos del wizard.
    """
    verificar_dueno(data.business_id, user_id)

    tiene_servicio = (
        supabase.table("services")
        .select("id", count="exact")
        .eq("business_id", data.business_id)
        .eq("active", True)
        .limit(1)
        .execute()
    )
    if not tiene_servicio.data:
        raise HTTPException(status_code=400, detail="Falta registrar al menos un servicio antes de activar el negocio")

    tiene_horario = (
        supabase.table("business_hours")
        .select("id", count="exact")
        .eq("business_id", data.business_id)
        .eq("is_open", True)
        .limit(1)
        .execute()
    )
    if not tiene_horario.data:
        raise HTTPException(status_code=400, detail="Falta activar al menos un dia de horario antes de activar el negocio")

    negocio = supabase.table("businesses").select("*").eq("id", data.business_id).execute()
    if negocio.data and not str(negocio.data[0].get("address") or "").strip():
        raise HTTPException(status_code=400, detail="Falta la direccion del negocio antes de activarlo")

    response = (
        supabase.table("businesses")
        .update({"onboarding_completed": True})
        .eq("id", data.business_id)
        .execute()
    )
    if not response.data:
        raise HTTPException(status_code=404, detail="Negocio no encontrado")
    return {"business": response.data[0]}


class HomeVisitsToggle(BaseModel):
    business_id: str
    enabled: bool


@router.put("/business-settings/home-visits")
def update_home_visits_enabled(data: HomeVisitsToggle, user_id: str = Depends(obtener_usuario_actual)):
    """Activa o desactiva los domicilios del negocio (con el flag apagado todo funciona como antes)."""
    verificar_dueno(data.business_id, user_id)
    response = (
        supabase.table("businesses")
        .update({"home_visits_enabled": data.enabled})
        .eq("id", data.business_id)
        .execute()
    )
    if not response.data:
        raise HTTPException(status_code=404, detail="Negocio no encontrado")
    return {"business": response.data[0]}
