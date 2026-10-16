"""
Datos con los que se llenan las plantillas de WhatsApp que reciben los CLIENTES
(confirmacion, reprogramacion, recordatorio y cancelacion).

Las plantillas v2 (ver docs/PLANTILLAS_WHATSAPP.md) separan fecha y hora y suman
profesional, direccion del negocio y enlaces. Aqui se arman esos valores a partir
del negocio y de la cita, en un solo lugar, para que los cuatro envios digan lo
mismo y nunca manden una variable vacia (Meta rechaza los parametros vacios).
"""

import os
from datetime import datetime

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

FRONTEND_URL = os.getenv("GOAGENDA_FRONTEND_URL", "http://localhost:4200").rstrip("/")

# Valores cuando el dato no existe: Meta no acepta variables vacias, y un negocio
# anterior al campo de direccion (o un empleado sin nombre) no debe romper el envio.
DIRECCION_POR_DEFECTO = "Consulta la dirección con el negocio"
PROFESIONAL_POR_DEFECTO = "Nuestro equipo"


def fecha_larga(fecha_hora: datetime) -> str:
    """'miércoles 15 de octubre' (sin el año: es siempre una fecha cercana)."""
    return f"{DIAS[fecha_hora.weekday()]} {fecha_hora.day} de {MESES[fecha_hora.month - 1]}"


def hora_12h(fecha_hora: datetime) -> str:
    """'3:00 pm' (el cliente nunca recibe horas en 24 h)."""
    return fecha_hora.strftime("%I:%M %p").lstrip("0").lower()


def direccion_del_negocio(business: dict | None) -> str:
    """Direccion + municipio/barrio si lo hay; un texto neutro si el negocio aun no la tiene."""
    if not business:
        return DIRECCION_POR_DEFECTO
    direccion = str(business.get("address") or "").strip()
    if not direccion:
        return DIRECCION_POR_DEFECTO
    ciudad = str(business.get("address_city") or "").strip()
    return f"{direccion}, {ciudad}" if ciudad else direccion


def enlace_del_chat(business_id: str | None) -> str:
    """Chat publico del negocio: sirve para reservar y, ahi mismo, para reprogramar o cancelar."""
    return f"{FRONTEND_URL}/chat/{business_id}" if business_id else FRONTEND_URL


def datos_para_plantilla(
    business: dict | None,
    fecha_hora: datetime,
    nombre_profesional: str | None = None,
) -> dict:
    """Todo lo que piden las plantillas v2, con valores por defecto donde puede faltar un dato."""
    business_id = (business or {}).get("id")
    enlace = enlace_del_chat(business_id)
    return {
        "fecha": fecha_larga(fecha_hora),
        "hora": hora_12h(fecha_hora),
        "profesional": (nombre_profesional or "").strip() or PROFESIONAL_POR_DEFECTO,
        "direccion": direccion_del_negocio(business),
        "enlace_reprogramacion": enlace,
        "enlace_agendamiento": enlace,
    }


def nombre_del_empleado(employee_id: str | None) -> str | None:
    """Nombre del profesional; si no se puede leer, la plantilla usa su texto por defecto (no rompe el envio)."""
    if not employee_id:
        return None
    from services.db import get_employee_by_id

    try:
        empleado = get_employee_by_id(employee_id)
    except Exception:
        return None
    return (empleado or {}).get("name")
