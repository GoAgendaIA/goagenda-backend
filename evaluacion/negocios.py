"""Negocios de prueba que usa la evaluacion (se siembran en el Supabase en memoria)."""

from datetime import datetime

BIZ_ID = "eval-biz"
TELEFONO_NEGOCIO = "573001112233"

# Miercoles 7 de octubre de 2026, 8:00: "sabado" = 2026-10-10, "viernes" = 2026-10-09.
AHORA = datetime(2026, 10, 7, 8, 0)
SABADO = "2026-10-10"
VIERNES = "2026-10-09"
DOMINGO = "2026-10-11"

PRESETS = ("barberia", "domicilios", "abono", "equipo")

LINK_DE_PAGO = "https://checkout.wompi.co/l/test123"
ABONO_CENTAVOS = 4_000_000  # 50 % de $80.000


def sembrar(db, preset: str) -> dict:
    """
    Siembra un negocio completo y devuelve sus datos:
    {"business_id", "employees": {id: nombre}, "services": {clave: fila}, "precios_validos": {int}}.

    - barberia: un empleado, sin domicilios ni abonos.
    - domicilios: igual + domicilios activos con zonas (Betulia $5.000, Urrao $8.000).
    - abono: igual a barberia + "Tinte" que exige abono del 50 %.
    - equipo: dos empleados (Luis solo hace "Corte de cabello" y no trabaja el sabado).
    """
    if preset not in PRESETS:
        raise ValueError(f"preset desconocido: {preset}")

    domicilios = preset == "domicilios"
    db.sembrar(
        "businesses",
        {"id": BIZ_ID, "name": "Barberia Elegant", "business_type": "barberia", "phone_number": TELEFONO_NEGOCIO,
         "home_visits_enabled": domicilios, "plan": "pro", "blocked": False, "onboarding_completed": True,
         "fcm_token": None, "monthly_ai_budget_usd": 100},
    )

    servicios = {
        "corte": {"id": "s-corte", "name": "Corte de cabello", "duration_minutes": 30, "price": 15000.0,
                  "offers_home_visit": domicilios, "requires_payment": False},
        "barba": {"id": "s-barba", "name": "Corte y barba", "duration_minutes": 45, "price": 22000.0,
                  "offers_home_visit": False, "requires_payment": False},
    }
    if preset == "abono":
        servicios["tinte"] = {"id": "s-tinte", "name": "Tinte", "duration_minutes": 60, "price": 80000.0,
                              "offers_home_visit": False, "requires_payment": True, "payment_type": "percentage",
                              "payment_percentage": 50.0}

    for s in servicios.values():
        db.sembrar("services", {**s, "business_id": BIZ_ID, "active": True})

    empleados = {"emp-1": "Chenier"}
    if preset == "equipo":
        empleados["emp-2"] = "Luis"

    for emp_id, nombre in empleados.items():
        db.sembrar("employees", {"id": emp_id, "business_id": BIZ_ID, "name": nombre, "active": True,
                                 "role": "owner" if emp_id == "emp-1" else "staff"})
        propios = servicios if emp_id == "emp-1" else {"corte": servicios["corte"]}
        for s in propios.values():
            db.sembrar("employee_services", {"employee_id": emp_id, "service_id": s["id"],
                                             "services": {**s, "business_id": BIZ_ID, "active": True}})
        dias = ["mon", "tue", "wed", "thu", "fri", "sat"] if emp_id == "emp-1" else ["mon", "tue", "wed", "thu", "fri"]
        for dia in dias:
            db.sembrar("employee_hours", {"employee_id": emp_id, "day": dia, "is_open": True, "opening_time": "09:00:00",
                                          "closing_time": "18:00:00", "lunch_start": "12:30:00", "lunch_end": "13:00:00"})
        for dia in ["sun"] + ([] if emp_id == "emp-1" else ["sat"]):
            db.sembrar("employee_hours", {"employee_id": emp_id, "day": dia, "is_open": False})

    for dia in ["mon", "tue", "wed", "thu", "fri", "sat"]:
        db.sembrar("business_hours", {"business_id": BIZ_ID, "day": dia, "is_open": True, "opening_time": "09:00:00",
                                      "closing_time": "18:00:00", "lunch_start": "12:30:00", "lunch_end": "13:00:00"})
    db.sembrar("business_hours", {"business_id": BIZ_ID, "day": "sun", "is_open": False})

    zonas = []
    if domicilios:
        zonas = [{"business_id": BIZ_ID, "name": "Betulia", "fee": 5000, "active": True, "aliases": ["La Bet"]},
                 {"business_id": BIZ_ID, "name": "Urrao", "fee": 8000, "active": True, "aliases": []}]
        db.sembrar("home_visit_zones", *zonas)

    precios = {int(s["price"]) for s in servicios.values()}
    recargos = {z["fee"] for z in zonas}
    validos = precios | recargos | {p + r for p in precios for r in recargos} | {0}
    if preset == "abono":
        validos.add(ABONO_CENTAVOS // 100)

    return {"business_id": BIZ_ID, "employees": empleados, "services": servicios, "precios_validos": validos}


def cita_previa(db, datos: dict, *, id: str, telefono: str, nombre: str, scheduled_at: str, servicio: str = "corte",
                employee_id: str = "emp-1", status: str = "confirmed") -> dict:
    """Siembra una cita ya existente (para escenarios de cancelar, reprogramar y consultar)."""
    s = datos["services"][servicio]
    return db.sembrar("appointments", {
        "id": id, "business_id": datos["business_id"], "employee_id": employee_id, "client_phone": telefono,
        "client_name": nombre, "service_id": s["id"], "scheduled_at": scheduled_at, "status": status,
        "source": "ai", "services": {"name": s["name"], "duration_minutes": s["duration_minutes"],
                                     "requires_payment": s.get("requires_payment", False)},
    })
