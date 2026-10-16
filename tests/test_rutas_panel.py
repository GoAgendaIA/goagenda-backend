"""Rutas HTTP del panel del negocio (contra la app real de main.py, Supabase en memoria y auth real)."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

BIZ = "biz-1"


@pytest.fixture
def negocio(db):
    db.sembrar("businesses", {"id": BIZ, "name": "Barberia", "owner_id": "dueno", "blocked": False, "home_visits_enabled": True})
    db.sembrar("employees",
               {"id": "emp-owner", "business_id": BIZ, "user_id": "dueno", "name": "Chenier", "role": "owner", "active": True, "created_at": "2026-01-01"},
               {"id": "emp-staff", "business_id": BIZ, "user_id": "empleado", "name": "Luis", "role": "staff", "active": True, "created_at": "2026-02-01"})


# ------------------------------------------------------------------------------
# Servicios
# ------------------------------------------------------------------------------

def test_crud_de_servicios(cliente, como, negocio, db):
    r = cliente.post("/services", json={"business_id": BIZ, "name": "Corte", "price": 15000}, headers=como("dueno"))
    servicio = r.json()["service"]
    assert r.status_code == 200 and servicio["requires_payment"] is False and servicio["active"] is True
    # Se liga solo al empleado principal.
    assert db.filas("employee_services")[0]["employee_id"] == "emp-owner"

    assert [s["name"] for s in cliente.get(f"/services?business_id={BIZ}", headers=como("empleado")).json()["services"]] == ["Corte"]
    r = cliente.put(f"/services/{servicio['id']}", json={"price": 18000}, headers=como("dueno"))
    assert r.json()["service"]["price"] == 18000
    assert cliente.put(f"/services/{servicio['id']}", json={}, headers=como("dueno")).status_code == 400
    assert cliente.delete(f"/services/{servicio['id']}", headers=como("dueno")).json()["service"]["active"] is False
    assert cliente.get(f"/services?business_id={BIZ}", headers=como("dueno")).json()["services"] == []


def test_servicio_con_abono_valida_la_configuracion(cliente, como, negocio):
    base = {"business_id": BIZ, "name": "Tinte", "requires_payment": True}
    assert cliente.post("/services", json={**base, "payment_type": "otro"}, headers=como("dueno")).status_code == 422
    assert cliente.post("/services", json={**base, "payment_type": "percentage", "payment_percentage": 150}, headers=como("dueno")).status_code == 422
    assert cliente.post("/services", json={**base, "payment_type": "fixed", "payment_fixed_amount_cents": 0}, headers=como("dueno")).status_code == 422
    r = cliente.post("/services", json={**base, "payment_type": "percentage", "payment_percentage": 50}, headers=como("dueno"))
    servicio = r.json()["service"]
    assert servicio["requires_payment"] is True and servicio["payment_percentage"] == 50
    # Desactivar el abono limpia su configuracion.
    r = cliente.put(f"/services/{servicio['id']}", json={"requires_payment": False}, headers=como("dueno"))
    assert r.json()["service"]["payment_type"] is None and r.json()["service"]["payment_percentage"] is None


def test_permisos_de_servicios(cliente, como, negocio, db):
    assert cliente.get(f"/services?business_id={BIZ}").status_code == 401
    assert cliente.post("/services", json={"business_id": BIZ, "name": "X"}, headers=como("empleado")).status_code == 403
    assert cliente.put("/services/no-existe", json={"price": 1}, headers=como("dueno")).status_code == 404
    assert cliente.delete("/services/no-existe", headers=como("dueno")).status_code == 404


def test_servicio_que_desaparece_entre_validacion_y_update(cliente, como, negocio, db, monkeypatch):
    from routes import services_routes

    servicio = db.sembrar("services", {"business_id": BIZ, "name": "Corte", "active": True})
    monkeypatch.setattr(services_routes, "_business_id_de_servicio", lambda sid: BIZ)
    assert cliente.put("/services/fantasma", json={"price": 1}, headers=como("dueno")).status_code == 404
    assert cliente.delete("/services/fantasma", headers=como("dueno")).status_code == 404
    assert servicio["active"] is True


# ------------------------------------------------------------------------------
# Ajustes del negocio
# ------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ruta, cuerpo, campo, valor",
    [
        ("/business-settings/info", {"name": "Nuevo", "phone_number": "3001234567"}, "phone_number", "3001234567"),
        ("/business-settings/fcm-token", {"fcm_token": "tok"}, "fcm_token", "tok"),
        ("/business-settings/reminder", {"reminder_hours_before": 2.5}, "reminder_hours_before", 2.5),
        ("/business-settings/onboarding-step", {"step": 3}, "onboarding_step", 3),
        ("/business-settings/home-visits", {"enabled": False}, "home_visits_enabled", False),
    ],
)
def test_ajustes_actualizan_el_negocio(cliente, como, negocio, ruta, cuerpo, campo, valor):
    r = cliente.put(ruta, json={"business_id": BIZ, **cuerpo}, headers=como("dueno"))
    assert r.status_code == 200 and r.json()["business"][campo] == valor
    assert cliente.put(ruta, json={"business_id": BIZ, **cuerpo}, headers=como("empleado")).status_code == 403


def test_ajustes_info_sin_telefono_y_lectura(cliente, como, negocio):
    r = cliente.put("/business-settings/info", json={"business_id": BIZ, "name": "Solo nombre"}, headers=como("dueno"))
    assert r.json()["business"]["name"] == "Solo nombre" and "phone_number" not in r.json()["business"]
    assert cliente.get(f"/business-settings?business_id={BIZ}", headers=como("empleado")).json()["business"]["name"] == "Solo nombre"


def test_recordatorio_fuera_de_rango(cliente, como, negocio):
    assert cliente.put("/business-settings/reminder", json={"business_id": BIZ, "reminder_hours_before": 200}, headers=como("dueno")).status_code == 400


def test_completar_onboarding_exige_servicio_y_horario(cliente, como, negocio, db):
    url, cuerpo = "/business-settings/onboarding/complete", {"business_id": BIZ}
    assert "servicio" in cliente.post(url, json=cuerpo, headers=como("dueno")).json()["detail"]
    db.sembrar("services", {"business_id": BIZ, "active": True})
    assert "horario" in cliente.post(url, json=cuerpo, headers=como("dueno")).json()["detail"]
    db.sembrar("business_hours", {"business_id": BIZ, "day": "mon", "is_open": True})
    assert "direccion" in cliente.post(url, json=cuerpo, headers=como("dueno")).json()["detail"]
    db.tablas["businesses"][0]["address"] = "   "  # solo espacios: sigue sin direccion
    assert cliente.post(url, json=cuerpo, headers=como("dueno")).status_code == 400
    db.tablas["businesses"][0]["address"] = "Vereda La Cita, despues del puente, casa amarilla"
    assert cliente.post(url, json=cuerpo, headers=como("dueno")).json()["business"]["onboarding_completed"] is True


# --- Direccion del negocio --------------------------------------------------------------------------------

URL_DIRECCION = "/business-settings/address"


@pytest.mark.parametrize("direccion, tipo", [
    ("Calle 10 # 5-20, local 3, Centro Comercial El Parque", "formal"),
    ("Vereda La Cita, despues del puente, casa amarilla", "referencias"),
    ("Finca El Mirador km 4 via al rio", "referencias"),
    ("Cra 7 sin numero, junto a la iglesia", "formal"),
])
def test_la_direccion_se_acepta_como_texto_libre(cliente, como, negocio, direccion, tipo):
    r = cliente.put(URL_DIRECCION, json={"business_id": BIZ, "address": f"  {direccion}  ", "address_kind": tipo},
                    headers=como("dueno"))
    assert r.status_code == 200
    negocio_guardado = r.json()["business"]
    assert negocio_guardado["address"] == direccion and negocio_guardado["address_kind"] == tipo
    assert negocio_guardado["address_lat"] is None and negocio_guardado["address_lng"] is None


def test_la_direccion_guarda_ciudad_indicaciones_y_pin(cliente, como, negocio):
    r = cliente.put(URL_DIRECCION, json={
        "business_id": BIZ, "address": "Calle 5 #10-20", "address_city": " Medellin ", "address_notes": "Segundo piso",
        "address_lat": 6.2442, "address_lng": -75.5812}, headers=como("dueno"))
    b = r.json()["business"]
    assert (b["address_city"], b["address_notes"], b["address_lat"], b["address_lng"]) == ("Medellin", "Segundo piso", 6.2442, -75.5812)
    # Campos opcionales vacios quedan en null, no como cadena vacia.
    r = cliente.put(URL_DIRECCION, json={"business_id": BIZ, "address": "Calle 5", "address_city": "  ", "address_notes": ""},
                    headers=como("dueno"))
    assert r.json()["business"]["address_city"] is None and r.json()["business"]["address_notes"] is None


@pytest.mark.parametrize("cuerpo, fragmento", [
    ({"address": ""}, "obligatoria"),
    ({"address": "    "}, "obligatoria"),
    ({"address": "x" * 301}, "300"),
    ({"address": "Calle 1", "address_kind": "mapa"}, "tipo"),
    ({"address": "Calle 1", "address_lat": 6.2}, "juntas"),
    ({"address": "Calle 1", "address_lng": -75.5}, "juntas"),
    ({"address": "Calle 1", "address_lat": 95, "address_lng": 10}, "rango"),
    ({"address": "Calle 1", "address_lat": 10, "address_lng": 200}, "rango"),
    ({"address": "Calle 1", "address_city": "c" * 121}, "120"),
])
def test_direccion_invalida(cliente, como, negocio, cuerpo, fragmento):
    r = cliente.put(URL_DIRECCION, json={"business_id": BIZ, **cuerpo}, headers=como("dueno"))
    assert r.status_code == 400 and fragmento in r.json()["detail"]


def test_direccion_solo_la_cambia_el_dueno_y_el_negocio_debe_existir(cliente, como, negocio, monkeypatch):
    from routes import business_settings_routes as bs

    cuerpo = {"business_id": BIZ, "address": "Calle 1"}
    assert cliente.put(URL_DIRECCION, json=cuerpo, headers=como("empleado")).status_code == 403
    monkeypatch.setattr(bs, "verificar_dueno", lambda b, u: None)
    assert cliente.put(URL_DIRECCION, json={**cuerpo, "business_id": "fantasma"}, headers=como("dueno")).status_code == 404


@pytest.mark.parametrize("ruta, cuerpo", [
    ("/business-settings/info", {"name": "x"}), ("/business-settings/fcm-token", {"fcm_token": "t"}),
    ("/business-settings/reminder", {"reminder_hours_before": 1}), ("/business-settings/onboarding-step", {"step": 1}),
    ("/business-settings/home-visits", {"enabled": True}),
])
def test_ajustes_de_negocio_que_desaparece(cliente, como, negocio, monkeypatch, ruta, cuerpo):
    from routes import business_settings_routes as bs

    monkeypatch.setattr(bs, "verificar_dueno", lambda b, u: None)
    assert cliente.put(ruta, json={"business_id": "fantasma", **cuerpo}, headers=como("dueno")).status_code == 404


def test_lectura_y_onboarding_de_negocio_inexistente(cliente, como, negocio, db, monkeypatch):
    from routes import business_settings_routes as bs

    monkeypatch.setattr(bs, "verificar_acceso_negocio", lambda b, u: None)
    monkeypatch.setattr(bs, "verificar_dueno", lambda b, u: None)
    assert cliente.get("/business-settings?business_id=fantasma", headers=como("dueno")).status_code == 404
    db.sembrar("services", {"business_id": "fantasma", "active": True})
    db.sembrar("business_hours", {"business_id": "fantasma", "is_open": True})
    assert cliente.post("/business-settings/onboarding/complete", json={"business_id": "fantasma"}, headers=como("dueno")).status_code == 404


# ------------------------------------------------------------------------------
# Horario del negocio (y herencia del empleado principal)
# ------------------------------------------------------------------------------

def test_horario_del_negocio_se_hereda_al_principal(cliente, como, negocio, db):
    cuerpo = {"business_id": BIZ, "day": "sat", "is_open": True, "opening_time": "07:00", "closing_time": "20:00",
              "lunch_start": "12:30", "lunch_end": "13:00"}
    assert cliente.put("/business-hours", json=cuerpo, headers=como("dueno")).status_code == 200
    principal = next(f for f in db.filas("employee_hours") if f["employee_id"] == "emp-owner")
    assert principal["opening_time"] == "07:00" and principal["closing_time"] == "20:00" and principal["lunch_end"] == "13:00"
    assert not any(f["employee_id"] == "emp-staff" for f in db.filas("employee_hours"))

    cliente.put("/business-hours", json={"business_id": BIZ, "day": "mon", "is_open": False}, headers=como("dueno"))
    horario = cliente.get(f"/business-hours?business_id={BIZ}", headers=como("dueno")).json()["business_hours"]
    assert [d["day"] for d in horario] == ["mon", "sat"]


def test_horario_dia_invalido_y_permisos(cliente, como, negocio):
    assert cliente.put("/business-hours", json={"business_id": BIZ, "day": "xyz", "is_open": True}, headers=como("dueno")).status_code == 400
    assert cliente.put("/business-hours", json={"business_id": BIZ, "day": "mon", "is_open": True}, headers=como("empleado")).status_code == 403


def test_horario_sin_permisos_rls(cliente, como, negocio, db):
    from postgrest.exceptions import APIError

    db.fallar_en["business_hours"] = APIError({"code": "42501", "message": "RLS"})
    assert cliente.put("/business-hours", json={"business_id": BIZ, "day": "mon", "is_open": True}, headers=como("dueno")).status_code == 403
    db.fallar_en["business_hours"] = APIError({"code": "XX000", "message": "otro"})
    assert cliente.put("/business-hours", json={"business_id": BIZ, "day": "mon", "is_open": True}, headers=como("dueno")).status_code == 500


# ------------------------------------------------------------------------------
# Empleados
# ------------------------------------------------------------------------------

def test_empleados(cliente, como, negocio, db):
    assert [e["name"] for e in cliente.get(f"/employees?business_id={BIZ}", headers=como("dueno")).json()["employees"]] == ["Chenier", "Luis"]
    assert cliente.put("/employees/emp-staff", json={"name": " Luisito "}, headers=como("empleado")).json()["employee"]["name"] == "Luisito"
    assert cliente.put("/employees/emp-staff", json={"name": "  "}, headers=como("dueno")).status_code == 400
    assert cliente.delete("/employees/emp-owner", headers=como("dueno")).status_code == 400
    assert cliente.delete("/employees/emp-staff", headers=como("empleado")).status_code == 403
    assert cliente.delete("/employees/emp-staff", headers=como("dueno")).json()["employee"]["active"] is False
    assert cliente.delete("/employees/nadie", headers=como("dueno")).status_code == 404


def test_actualizar_empleado_que_desaparece(cliente, como, negocio, monkeypatch):
    from routes import employees_routes as er

    monkeypatch.setattr(er, "verificar_acceso_empleado", lambda e, u: BIZ)
    assert cliente.put("/employees/fantasma", json={"name": "X"}, headers=como("dueno")).status_code == 404


def test_horario_de_empleados(cliente, como, negocio, db):
    cuerpo = {"day": "mon", "is_open": True, "opening_time": "08:00", "closing_time": "16:00"}
    assert cliente.put("/employees/emp-staff/hours", json=cuerpo, headers=como("empleado")).json()["employee_hour"]["opening_time"] == "08:00"
    assert cliente.put("/employees/emp-staff/hours", json={**cuerpo, "day": "lunes"}, headers=como("dueno")).status_code == 400
    # El principal hereda el horario del negocio: no se edita aparte.
    r = cliente.put("/employees/emp-owner/hours", json=cuerpo, headers=como("dueno"))
    assert r.status_code == 400 and "seccion Horario" in r.json()["detail"]
    db.sembrar("employee_hours", {"employee_id": "emp-staff", "day": "sun", "is_open": False})
    assert [d["day"] for d in cliente.get("/employees/emp-staff/hours", headers=como("dueno")).json()["employee_hours"]] == ["mon", "sun"]


def test_servicios_de_empleados(cliente, como, negocio, db):
    db.sembrar("services", {"id": "s1", "business_id": BIZ, "name": "Corte", "active": True})
    assert cliente.put("/employees/emp-staff/services", json={"service_ids": ["s1"]}, headers=como("empleado")).status_code == 403
    r = cliente.put("/employees/emp-staff/services", json={"service_ids": ["s1"]}, headers=como("dueno"))
    assert r.json()["business_id"] == BIZ
    db.filas("employee_services")[0]["services"] = {"id": "s1", "name": "Corte", "active": True}
    assert [s["name"] for s in cliente.get("/employees/emp-staff/services", headers=como("empleado")).json()["services"]] == ["Corte"]
    assert cliente.put("/employees/nadie/services", json={"service_ids": []}, headers=como("dueno")).status_code == 404


# ------------------------------------------------------------------------------
# Zonas de domicilio
# ------------------------------------------------------------------------------

def test_zonas_de_domicilio(cliente, como, negocio, db):
    cuerpo = {"business_id": BIZ, "name": " Betulia ", "fee": 5000, "aliases": [" La Bet", "la bet", "", "Bet"]}
    zona = cliente.post("/home-visit-zones", json=cuerpo, headers=como("dueno")).json()["zone"]
    assert zona["name"] == "Betulia" and zona["aliases"] == ["La Bet", "Bet"]
    assert cliente.post("/home-visit-zones", json={**cuerpo, "name": "betulia"}, headers=como("dueno")).status_code == 409
    assert cliente.post("/home-visit-zones", json={**cuerpo, "name": "   "}, headers=como("dueno")).status_code == 400
    assert cliente.post("/home-visit-zones", json={**cuerpo, "fee": -1}, headers=como("dueno")).status_code == 422

    r = cliente.put(f"/home-visit-zones/{zona['id']}", json={"name": " Betulia Centro ", "aliases": ["x"] * 3}, headers=como("dueno"))
    assert r.json()["zone"]["name"] == "Betulia Centro" and r.json()["zone"]["aliases"] == ["x"]
    assert cliente.put(f"/home-visit-zones/{zona['id']}", json={}, headers=como("dueno")).status_code == 400
    assert cliente.put(f"/home-visit-zones/{zona['id']}", json={"name": "   "}, headers=como("dueno")).status_code == 400
    assert cliente.put("/home-visit-zones/nada", json={"fee": 1}, headers=como("dueno")).status_code == 404
    assert [z["name"] for z in cliente.get(f"/home-visit-zones?business_id={BIZ}", headers=como("empleado")).json()["zones"]] == ["Betulia Centro"]
    assert cliente.delete(f"/home-visit-zones/{zona['id']}", headers=como("empleado")).status_code == 403
    assert cliente.delete(f"/home-visit-zones/{zona['id']}", headers=como("dueno")).json() == {"deleted": True}
    assert db.filas("home_visit_zones") == []


def test_limite_de_alias():
    from routes.home_visit_zones_routes import MAX_ALIASES_POR_ZONA, _limpiar_alias

    assert _limpiar_alias(None) == []
    assert len(_limpiar_alias([f"alias {i}" for i in range(20)])) == MAX_ALIASES_POR_ZONA
    assert _limpiar_alias(["a" * 100]) == ["a" * 80]


def test_zona_que_desaparece_al_actualizar(cliente, como, negocio, monkeypatch):
    from routes import home_visit_zones_routes as zr

    monkeypatch.setattr(zr, "_business_id_de_zona", lambda z: BIZ)
    assert cliente.put("/home-visit-zones/fantasma", json={"fee": 1}, headers=como("dueno")).status_code == 404


# ------------------------------------------------------------------------------
# Chats excluidos, /me, codigos de invitacion y super admin
# ------------------------------------------------------------------------------

def test_chats_excluidos(cliente, como, negocio):
    assert cliente.post("/excluded-chats", json={"business_id": BIZ, "phone_number": "12"}, headers=como("dueno")).status_code == 400
    r = cliente.post("/excluded-chats", json={"business_id": BIZ, "phone_number": "+57 300-123-4567"}, headers=como("dueno"))
    assert r.json()["excluded_chat"]["phone_number"] == "573001234567"
    assert len(cliente.get(f"/excluded-chats?business_id={BIZ}", headers=como("dueno")).json()["excluded_chats"]) == 1
    assert cliente.delete(f"/excluded-chats?business_id={BIZ}&phone_number=573001234567", headers=como("dueno")).json() == {"eliminado": True}
    assert cliente.get(f"/excluded-chats?business_id={BIZ}", headers=como("empleado")).status_code == 403


def test_me(cliente, como, negocio, db):
    db.filas("employees")[0]["businesses"] = {"name": "Barberia", "blocked": False, "home_visits_enabled": True}
    db.sembrar("super_admins", {"user_id": "dueno"})
    r = cliente.get("/me", headers=como("dueno")).json()
    assert r["is_super_admin"] is True
    [empleo] = r["employments"]
    assert empleo["business_name"] == "Barberia" and empleo["business_home_visits_enabled"] is True
    assert empleo["business_onboarding_completed"] is True and empleo["business_onboarding_step"] == 1
    sin_negocio = cliente.get("/me", headers=como("nuevo")).json()
    assert sin_negocio == {"user_id": "nuevo", "is_super_admin": False, "employments": []}


def test_invitacion_de_empleado(cliente, como, negocio, db):
    codigo = cliente.post(f"/businesses/{BIZ}/invitation-codes", json={"employee_name": "Ana"}, headers=como("dueno")).json()["invitation_code"]
    r = cliente.post("/invitation-codes/redeem", json={"code": codigo["code"].lower()}, headers=como("ana"))
    assert r.json()["role"] == "employee" and r.json()["employee"]["name"] == "Ana"
    assert cliente.post("/invitation-codes/redeem", json={"code": codigo["code"]}, headers=como("otro")).status_code == 404
    assert cliente.post(f"/businesses/{BIZ}/invitation-codes", json={}, headers=como("empleado")).status_code == 403


def test_invitacion_de_administrador(cliente, como, db):
    db.sembrar("super_admins", {"user_id": "root"})
    negocio = cliente.post("/admin/businesses", json={"name": " Nuevo ", "business_type": " Spa "}, headers=como("root")).json()["business"]
    assert negocio["name"] == "Nuevo" and negocio["business_type"] == "Spa"
    assert len([f for f in db.filas("business_hours") if f["business_id"] == negocio["id"]]) == 7

    codigo = cliente.post(f"/admin/businesses/{negocio['id']}/invitation-codes", headers=como("root")).json()["invitation_code"]
    r = cliente.post("/invitation-codes/redeem", json={"code": codigo["code"]}, headers=como("nuevo-dueno"))
    assert r.json()["role"] == "admin" and r.json()["employee"]["role"] == "owner"
    assert next(b for b in db.filas("businesses") if b["id"] == negocio["id"])["owner_id"] == "nuevo-dueno"

    otro = cliente.post(f"/admin/businesses/{negocio['id']}/invitation-codes", headers=como("root")).json()["invitation_code"]
    assert cliente.post("/invitation-codes/redeem", json={"code": otro["code"]}, headers=como("intruso")).status_code == 409


def test_invitacion_de_administrador_a_negocio_borrado(cliente, como, db):
    db.sembrar("invitation_codes", {"code": "ABC123", "business_id": "borrado", "role": "admin", "used_at": None})
    assert cliente.post("/invitation-codes/redeem", json={"code": "ABC123"}, headers=como("u")).status_code == 404


def test_super_admin(cliente, como, db, capsys):
    db.sembrar("super_admins", {"user_id": "root"})
    assert cliente.post("/admin/businesses", json={"name": "X"}, headers=como("normal")).status_code == 403
    assert cliente.post("/admin/businesses", json={"name": "  "}, headers=como("root")).status_code == 400
    db.sembrar("businesses", {"id": "b1", "name": "A", "created_at": "2026-01-01"}, {"id": "b2", "name": "B", "created_at": "2026-02-01"})
    assert [b["id"] for b in cliente.get("/admin/businesses", headers=como("root")).json()["businesses"]] == ["b2", "b1"]
    assert cliente.put("/admin/businesses/b1/blocked", json={"blocked": True}, headers=como("root")).json()["business"]["blocked"] is True
    assert cliente.put("/admin/businesses/nada/blocked", json={"blocked": True}, headers=como("root")).status_code == 404
    assert cliente.post("/admin/businesses/nada/invitation-codes", headers=como("root")).status_code == 404
    db.fallar_en["business_hours"] = RuntimeError("caido")
    assert cliente.post("/admin/businesses", json={"name": "Sin horario"}, headers=como("root")).status_code == 200
    assert "No se pudo crear el horario por defecto" in capsys.readouterr().out


def test_super_admins_listar_y_crear(cliente, como, db):
    db.sembrar("super_admins", {"user_id": "root", "created_at": "2026-01-01"})
    db.auth.admin.usuarios = [SimpleNamespace(id="root", email="root@goagenda.test"), SimpleNamespace(id="sin-email", email=None)]
    db.sembrar("super_admins", {"user_id": "sin-email", "created_at": "2026-02-01"})

    assert cliente.get("/admin/super-admins").status_code == 401
    assert cliente.get("/admin/super-admins", headers=como("normal")).status_code == 403
    lista = cliente.get("/admin/super-admins", headers=como("root")).json()["super_admins"]
    assert [(a["user_id"], a["email"], a["is_me"]) for a in lista] == [("sin-email", None, False), ("root", "root@goagenda.test", True)]

    r = cliente.post("/admin/super-admins", json={"email": "  Nuevo@GoAgenda.test ", "password": "clave-segura"}, headers=como("root"))
    assert r.status_code == 200 and r.json()["super_admin"]["email"] == "nuevo@goagenda.test"
    nuevo_id = r.json()["super_admin"]["user_id"]
    assert any(f["user_id"] == nuevo_id for f in db.filas("super_admins"))
    assert [u.email for u in db.auth.admin.usuarios][-1] == "nuevo@goagenda.test"

    # La cuenta nueva ya figura en la lista con su email.
    emails = {a["user_id"]: a["email"] for a in cliente.get("/admin/super-admins", headers=como("root")).json()["super_admins"]}
    assert emails[nuevo_id] == "nuevo@goagenda.test"


def test_revocar_super_admin(cliente, como, db, monkeypatch):
    db.auth.admin.usuarios = [
        SimpleNamespace(id="root", email="root@goagenda.test"),
        SimpleNamespace(id="otro", email="otro@goagenda.test"),
        SimpleNamespace(id="jefe", email="Jefe@GoAgenda.test"),
    ]
    db.sembrar("super_admins", {"user_id": "root"}, {"user_id": "otro"}, {"user_id": "jefe"})
    monkeypatch.setenv("SUPER_ADMIN_EMAIL", "jefe@goagenda.test")
    h = como("root")

    assert cliente.delete("/admin/super-admins/otro").status_code == 401
    assert cliente.delete("/admin/super-admins/otro", headers=como("normal")).status_code == 403
    # No puede revocarse a si mismo, ni la cuenta principal, ni una cuenta que no es super admin.
    assert cliente.delete("/admin/super-admins/root", headers=h).status_code == 400
    r = cliente.delete("/admin/super-admins/jefe", headers=h)
    assert r.status_code == 400 and "cuenta principal" in r.json()["detail"]
    assert cliente.delete("/admin/super-admins/nadie", headers=h).status_code == 404
    assert {f["user_id"] for f in db.filas("super_admins")} == {"root", "otro", "jefe"}

    # La lista marca la cuenta principal para que el front no ofrezca revocarla.
    lista = {a["user_id"]: a["is_primary"] for a in cliente.get("/admin/super-admins", headers=h).json()["super_admins"]}
    assert lista == {"root": False, "otro": False, "jefe": True}

    r = cliente.delete("/admin/super-admins/otro", headers=h)
    assert r.status_code == 200 and r.json() == {"revoked": True, "user_id": "otro"}
    assert {f["user_id"] for f in db.filas("super_admins")} == {"root", "jefe"}
    # La cuenta de Auth se conserva, pero ya no puede usar /admin.
    assert [u.id for u in db.auth.admin.usuarios if u.id == "otro"] == ["otro"]
    db.auth.usuarios_por_token["tok-otro"] = "otro"
    assert cliente.get("/admin/super-admins", headers={"Authorization": "Bearer tok-otro"}).status_code == 403


def test_presupuestos_de_ia_listar_y_editar(cliente, como, db, monkeypatch):
    from datetime import datetime

    from core import settings
    from services import ai_usage_tracking as uso

    db.sembrar("super_admins", {"user_id": "root"})
    db.sembrar("businesses",
               {"id": "b1", "name": "Barberia", "monthly_ai_budget_usd": 10},
               {"id": "b2", "name": "Spa", "monthly_ai_budget_usd": None})
    ahora = datetime.now().isoformat()
    db.sembrar("ai_usage_log",
               {"business_id": "b1", "estimated_cost_usd": 2.5, "created_at": ahora},
               {"business_id": "b1", "estimated_cost_usd": 0.5, "created_at": ahora},
               {"business_id": "b2", "estimated_cost_usd": 6.0, "created_at": ahora},
               {"business_id": "b1", "estimated_cost_usd": 99, "created_at": "2020-01-01T00:00:00"})  # otro mes: no cuenta

    assert cliente.get("/admin/ai-budgets").status_code == 401
    assert cliente.get("/admin/ai-budgets", headers=como("normal")).status_code == 403
    r = cliente.get("/admin/ai-budgets", headers=como("root")).json()
    assert r["presupuesto_por_defecto_usd"] == 5.0 and r["corte_duro_activo"] is False
    filas = {f["business_id"]: f for f in r["presupuestos"]}
    assert filas["b1"] == {"business_id": "b1", "name": "Barberia", "presupuesto_guardado_usd": 10,
                           "presupuesto_efectivo_usd": 10, "gasto_mes_usd": 3.0, "porcentaje_usado": 30.0}
    # Sin presupuesto guardado rige el valor por defecto.
    assert filas["b2"]["presupuesto_guardado_usd"] is None and filas["b2"]["presupuesto_efectivo_usd"] == 5.0
    assert filas["b2"]["porcentaje_usado"] == 120.0

    # Editar: aplica de inmediato aunque el corte este cacheado como agotado.
    monkeypatch.setattr(settings, "AI_PRESUPUESTO_CORTE_DURO", True)
    uso.limpiar_cache_presupuesto()
    assert uso.presupuesto_agotado({"id": "b2", "monthly_ai_budget_usd": None, "fcm_token": None}) is True
    r = cliente.put("/admin/businesses/b2/ai-budget", json={"monthly_ai_budget_usd": 20.256}, headers=como("root"))
    assert r.status_code == 200 and r.json() == {"business_id": "b2", "monthly_ai_budget_usd": 20.26}
    assert uso.presupuesto_agotado({"id": "b2", "monthly_ai_budget_usd": 20.26}) is False


def test_presupuestos_de_ia_validaciones(cliente, como, db):
    db.sembrar("super_admins", {"user_id": "root"})
    db.sembrar("businesses", {"id": "b1", "name": "Barberia"})
    h = como("root")
    url = "/admin/businesses/b1/ai-budget"
    assert cliente.put(url, json={"monthly_ai_budget_usd": 5}, headers=como("normal")).status_code == 403
    for malo in (0, -3, 1001, "mucho", None):
        assert cliente.put(url, json={"monthly_ai_budget_usd": malo}, headers=h).status_code == 422
    assert cliente.put(url, json={}, headers=h).status_code == 422
    assert cliente.put("/admin/businesses/nada/ai-budget", json={"monthly_ai_budget_usd": 5}, headers=h).status_code == 404


def test_gasto_de_todos_los_negocios_pagina(db):
    from datetime import datetime

    from services import ai_usage_tracking as uso

    ahora = datetime.now().isoformat()
    db.sembrar("ai_usage_log", *[{"business_id": f"b{i % 3}", "estimated_cost_usd": 0.01, "created_at": ahora} for i in range(1250)])
    gasto = uso.obtener_gasto_mensual_de_todos()
    assert round(sum(gasto.values()), 2) == 12.5 and set(gasto) == {"b0", "b1", "b2"}


def test_super_admins_validaciones(cliente, como, db):
    db.sembrar("super_admins", {"user_id": "root"})
    h = como("root")
    assert cliente.post("/admin/super-admins", json={"email": "no-es-email", "password": "clave-segura"}, headers=h).status_code == 400
    assert cliente.post("/admin/super-admins", json={"email": "a@b.co", "password": "corta"}, headers=h).status_code == 400
    assert cliente.post("/admin/super-admins", json={"email": "a@b.co", "password": "clave-segura"}, headers=como("normal")).status_code == 403
    assert cliente.post("/admin/super-admins", json={"email": "a@b.co"}, headers=h).status_code == 422

    # No promueve una cuenta que ya existe (por ejemplo un dueño de negocio).
    db.auth.admin.usuarios = [SimpleNamespace(id="dueno", email="dueno@negocio.co")]
    r = cliente.post("/admin/super-admins", json={"email": "DUENO@negocio.co", "password": "clave-segura"}, headers=h)
    assert r.status_code == 409
    assert [f["user_id"] for f in db.filas("super_admins")] == ["root"]


def test_super_admins_fallas_de_supabase(cliente, como, db, monkeypatch, capsys):
    from tests.fakes import _Consulta

    db.sembrar("super_admins", {"user_id": "root"})
    h = como("root")

    original_create = db.auth.admin.create_user

    def rechaza(atributos):
        raise RuntimeError("weak password")

    monkeypatch.setattr(db.auth.admin, "create_user", rechaza)
    r = cliente.post("/admin/super-admins", json={"email": "a@b.co", "password": "clave-segura"}, headers=h)
    assert r.status_code == 400 and "No se pudo crear la cuenta" in r.json()["detail"]
    monkeypatch.setattr(db.auth.admin, "create_user", original_create)

    # Falla solo el upsert del rol (la verificacion de super admin sigue funcionando).
    original = _Consulta.execute

    def upsert_roto(self):
        if self._tabla == "super_admins" and self._operacion == "upsert":
            raise RuntimeError("caido")
        return original(self)

    monkeypatch.setattr(_Consulta, "execute", upsert_roto)

    # No queda una cuenta huerfana: se intenta borrar la que se acababa de crear.
    eliminados = []
    monkeypatch.setattr(db.auth.admin, "delete_user", lambda uid: eliminados.append(uid), raising=False)
    r = cliente.post("/admin/super-admins", json={"email": "b@b.co", "password": "clave-segura"}, headers=h)
    assert r.status_code == 500 and len(eliminados) == 1

    # Si ademas la limpieza falla, igual responde el error y lo deja en el log.
    def limpieza_rota(uid):
        raise RuntimeError("sin permisos")

    monkeypatch.setattr(db.auth.admin, "delete_user", limpieza_rota, raising=False)
    r = cliente.post("/admin/super-admins", json={"email": "c@b.co", "password": "clave-segura"}, headers=h)
    assert r.status_code == 500 and "Tampoco se pudo limpiar la cuenta" in capsys.readouterr().out


def test_listar_super_admins_pagina_los_usuarios(cliente, como, db):
    db.auth.admin.usuarios = [SimpleNamespace(id=f"u{i}", email=f"u{i}@x.co") for i in range(250)]
    db.sembrar("super_admins", {"user_id": "u0"}, {"user_id": "u249"})
    lista = cliente.get("/admin/super-admins", headers=como("u0")).json()["super_admins"]
    assert {a["user_id"]: a["email"] for a in lista} == {"u0": "u0@x.co", "u249": "u249@x.co"}
    # Un usuario de Supabase que ya no aparece: la lista no se queda en bucle.
    db.sembrar("super_admins", {"user_id": "fantasma"})
    assert {a["user_id"] for a in cliente.get("/admin/super-admins", headers=como("u0")).json()["super_admins"]} >= {"fantasma"}
    # Con exactamente una pagina llena, la siguiente viene vacia y tampoco se queda en bucle.
    db.auth.admin.usuarios = db.auth.admin.usuarios[:200]
    assert cliente.get("/admin/super-admins", headers=como("u0")).status_code == 200


def test_crear_negocio_sin_respuesta(cliente, como, db, monkeypatch):
    db.sembrar("super_admins", {"user_id": "root"})
    from tests.fakes import _Consulta

    original = _Consulta.execute

    def insert_vacio(self):
        if self._tabla == "businesses" and self._operacion == "insert":
            from tests.fakes import _Resultado
            return _Resultado([])
        return original(self)

    monkeypatch.setattr(_Consulta, "execute", insert_vacio)
    assert cliente.post("/admin/businesses", json={"name": "X"}, headers=como("root")).status_code == 500


# ------------------------------------------------------------------------------
# Wompi (configuracion e historial de abonos)
# ------------------------------------------------------------------------------

def test_configuracion_wompi(cliente, como, negocio, monkeypatch):
    from services import wompi_credentials

    assert cliente.get(f"/business-settings/wompi?business_id={BIZ}", headers=como("dueno")).json() == {"configured": False}
    llaves = {"business_id": BIZ, "public_key": "pub_test_123456", "private_key": "prv_test_123456", "events_key": "evt_test_123456"}
    assert cliente.put("/business-settings/wompi", json={**llaves, "public_key": "corta"}, headers=como("dueno")).status_code == 422
    guardado = cliente.put("/business-settings/wompi", json=llaves, headers=como("dueno")).json()["credentials"]
    assert guardado["sandbox_mode"] is True and "private_key_encrypted" not in guardado
    estado = cliente.get(f"/business-settings/wompi?business_id={BIZ}", headers=como("dueno")).json()
    assert estado["configured"] is True and "private_key" not in str(estado)

    monkeypatch.setattr(wompi_credentials, "obtener_info_comercio", lambda pub, sb: {"id": 1})
    assert cliente.post("/business-settings/wompi/validate", json={"business_id": BIZ}, headers=como("dueno")).json()["valid"] is True

    def rechaza(pub, sb):
        raise wompi_credentials.WompiError("llave invalida")

    monkeypatch.setattr(wompi_credentials, "obtener_info_comercio", rechaza)
    r = cliente.post("/business-settings/wompi/validate", json={"business_id": BIZ}, headers=como("dueno"))
    assert r.status_code == 400 and r.json()["detail"] == "llave invalida"
    assert cliente.delete(f"/business-settings/wompi?business_id={BIZ}", headers=como("dueno")).json() == {"deleted": True}
    assert cliente.get(f"/business-settings/wompi?business_id={BIZ}", headers=como("empleado")).status_code == 403


def test_historial_de_abonos(cliente, como, negocio, db, monkeypatch):
    from routes import wompi_payment_requests_routes as pr

    solicitud = db.sembrar("wompi_payment_requests", {"business_id": BIZ, "status": "pending", "created_at": "2026-01-01"})
    assert len(cliente.get(f"/wompi/payment-requests?business_id={BIZ}", headers=como("empleado")).json()["payment_requests"]) == 1
    assert cliente.get(f"/wompi/payment-requests?business_id={BIZ}&limit=0", headers=como("dueno")).status_code == 400
    r = cliente.post(f"/wompi/payment-requests/{solicitud['id']}/check?business_id={BIZ}", headers=como("dueno"))
    assert "ningun intento de pago" in r.json()["mensaje"]
    assert cliente.post(f"/wompi/payment-requests/nada/check?business_id={BIZ}", headers=como("dueno")).status_code == 400


# ------------------------------------------------------------------------------
# Citas manuales y disponibilidad
# ------------------------------------------------------------------------------

@pytest.fixture
def agenda(negocio, db, monkeypatch):
    from routes import manual_appointments as ma
    from services import scheduling

    monkeypatch.setattr(scheduling, "ahora_local", lambda: datetime(2026, 10, 7, 8, 0))
    efectos = MagicMock()
    monkeypatch.setattr(ma, "enviar_confirmacion_cita_cliente", efectos.whatsapp)
    from services import realtime

    monkeypatch.setattr(realtime, "emitir_evento_cita", efectos.evento)
    corte = {"id": "s1", "business_id": BIZ, "name": "Corte", "duration_minutes": 30, "price": 15000, "active": True, "offers_home_visit": True}
    db.sembrar("services", dict(corte), {"id": "s2", "business_id": BIZ, "name": "Tinte", "duration_minutes": 60, "active": True, "offers_home_visit": False})
    db.sembrar("employee_hours", {"employee_id": "emp-staff", "day": "sat", "is_open": True, "opening_time": "09:00:00", "closing_time": "11:00:00"})
    db.sembrar("home_visit_zones", {"business_id": BIZ, "name": "Betulia", "fee": 5000, "active": True})
    return efectos


CITA = {"business_id": BIZ, "employee_id": "emp-staff", "client_name": "Frank", "service_id": "s1", "fecha_hora": "2026-10-10T09:00:00"}


def test_cita_manual_con_y_sin_telefono(cliente, como, agenda, db):
    r = cliente.post("/appointments/manual", json={**CITA, "client_phone": "300 123 4567"}, headers=como("dueno"))
    assert r.status_code == 200 and r.json()["cita_creada"][0]["status"] == "confirmed"
    assert agenda.whatsapp.call_args.kwargs["client_phone"] == "573001234567"
    agenda.evento.assert_called_once()

    agenda.whatsapp.reset_mock()
    r = cliente.post("/appointments/manual", json={**CITA, "fecha_hora": "2026-10-10T10:00:00"}, headers=como("dueno"))
    assert r.json()["cita_creada"][0]["client_phone"] == ""
    agenda.whatsapp.assert_not_called()


def test_cita_manual_validaciones(cliente, como, agenda, db):
    assert cliente.post("/appointments/manual", json=CITA, headers=como("empleado")).status_code == 403
    assert cliente.post("/appointments/manual", json={**CITA, "employee_id": "nadie"}, headers=como("dueno")).status_code == 404
    assert cliente.post("/appointments/manual", json={**CITA, "service_id": "nada"}, headers=como("dueno")).status_code == 404
    assert cliente.post("/appointments/manual", json={**CITA, "fecha_hora": "2026-10-10T18:00:00"}, headers=como("dueno")).status_code == 400
    cliente.post("/appointments/manual", json=CITA, headers=como("dueno"))
    assert cliente.post("/appointments/manual", json=CITA, headers=como("dueno")).status_code == 409


def test_cita_manual_a_domicilio(cliente, como, agenda, db):
    base = {**CITA, "address": " Calle 5 "}
    assert cliente.post("/appointments/manual", json={**base, "service_id": "s2"}, headers=como("dueno")).status_code == 400
    assert "fuera de las zonas" in cliente.post("/appointments/manual", json={**base, "zone": "Envigado"}, headers=como("dueno")).json()["detail"]
    cita = cliente.post("/appointments/manual", json={**base, "zone": "betulia"}, headers=como("dueno")).json()["cita_creada"][0]
    assert cita["address"] == "Calle 5" and cita["home_visit_zone"] == "Betulia" and cita["home_visit_fee"] == 5000.0
    db.filas("businesses")[0]["home_visits_enabled"] = False
    assert "no tiene los domicilios" in cliente.post("/appointments/manual", json=base, headers=como("dueno")).json()["detail"]


def test_cita_manual_whatsapp_que_falla_no_bloquea(cliente, como, agenda, capsys):
    agenda.whatsapp.side_effect = RuntimeError("meta caido")
    assert cliente.post("/appointments/manual", json={**CITA, "client_phone": "3001234567"}, headers=como("dueno")).status_code == 200
    assert "No se pudo enviar la confirmacion" in capsys.readouterr().out


def test_listar_citas_con_filtros(cliente, como, agenda, db):
    base = {"business_id": BIZ, "status": "confirmed"}
    db.sembrar("appointments",
               {**base, "employee_id": "emp-staff", "scheduled_at": "2026-10-10T09:00:00", "is_home_visit": True},
               {**base, "employee_id": "emp-owner", "scheduled_at": "2026-10-11T09:00:00", "is_home_visit": False},
               {**base, "employee_id": "emp-staff", "scheduled_at": "2026-10-12T09:00:00", "status": "cancelled", "is_home_visit": False})
    url = f"/appointments?business_id={BIZ}"
    assert cliente.get(url, headers=como("dueno")).json()["total"] == 3
    assert cliente.get(f"{url}&status=cancelled", headers=como("dueno")).json()["total"] == 1
    assert cliente.get(f"{url}&date_from=2026-10-11T00:00:00&date_to=2026-10-11T23:59:59", headers=como("dueno")).json()["total"] == 1
    assert cliente.get(f"{url}&home_visit=true", headers=como("dueno")).json()["total"] == 1
    assert cliente.get(f"{url}&employee_id=emp-staff", headers=como("empleado")).json()["total"] == 2
    assert cliente.get(url, headers=como("empleado")).status_code == 403
    assert cliente.get(f"{url}&status=raro", headers=como("dueno")).status_code == 422
    pagina = cliente.get(f"{url}&limit=1&offset=1", headers=como("dueno")).json()
    assert len(pagina["appointments"]) == 1 and pagina["appointments"][0]["scheduled_at"] == "2026-10-11T09:00:00"


def test_turnos_disponibles(cliente, como, agenda):
    url = f"/appointments/available-slots?business_id={BIZ}&employee_id=emp-staff&date=2026-10-10"
    assert cliente.get(url, headers=como("dueno")).json()["available_slots"] == ["09:00", "09:30", "10:00", "10:30"]
    r = cliente.get(f"{url}&service_id=s2", headers=como("dueno")).json()
    assert r["duration_minutes"] == 60 and r["available_slots"] == ["09:00", "09:30", "10:00"]
    assert cliente.get(f"{url}&service_id=nada", headers=como("dueno")).status_code == 404
    assert cliente.get(url.replace("2026-10-10", "mañana"), headers=como("dueno")).status_code == 400


# ------------------------------------------------------------------------------
# Manejo global de errores de Postgrest (main.py)
# ------------------------------------------------------------------------------

@pytest.mark.parametrize("codigo, status", [("22P02", 404), ("PGRST205", 503), ("XX000", 500)])
def test_errores_de_postgrest_se_traducen(cliente, como, negocio, db, codigo, status):
    from postgrest.exceptions import APIError

    db.fallar_en["home_visit_zones"] = APIError({"code": codigo, "message": "x"})
    assert cliente.get(f"/home-visit-zones?business_id={BIZ}", headers=como("dueno")).status_code == status


# ------------------------------------------------------------------------------
# Cancelar una cita desde el panel (el profesional)
# ------------------------------------------------------------------------------

@pytest.fixture
def cancelacion(agenda, db, monkeypatch):
    """Cita futura confirmada del empleado `emp-staff`; el aviso por WhatsApp queda espiado."""
    from routes import manual_appointments as ma

    aviso = MagicMock(return_value={"messages": [{"id": "wamid"}]})
    monkeypatch.setattr(ma, "enviar_cancelacion_cita_cliente", aviso)
    cita = db.sembrar("appointments", {
        "id": "c1", "business_id": BIZ, "employee_id": "emp-staff", "client_phone": "573001234567", "client_name": "Frank",
        "service_id": "s1", "scheduled_at": "2026-10-10T09:00:00", "status": "confirmed",
        # El doble de Supabase no resuelve los joins (en produccion los trae get_appointment_full).
        "services": {"name": "Corte", "price": 15000, "duration_minutes": 30}, "employees": {"name": "Luis"}})
    return SimpleNamespace(aviso=aviso, evento=agenda.evento, cita=cita)


def test_el_dueno_cancela_una_cita_y_el_cliente_recibe_el_aviso(cliente, como, cancelacion, db):
    r = cliente.post("/appointments/c1/cancel", headers=como("dueno"))

    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["cita"]["status"] == "cancelled" and cuerpo["cita"]["cancelled_by"] == "business"
    assert cuerpo["aviso_cliente"] == "enviado" and cuerpo["tiene_pago"] is False
    assert db.filas("appointments")[0]["status"] == "cancelled"  # se cancela, no se borra
    kw = cancelacion.aviso.call_args.kwargs
    assert kw["client_phone"] == "573001234567" and kw["nombre_cliente"] == "Frank"
    assert kw["nombre_negocio"] == "Barberia" and kw["nombre_servicio"] == "Corte"
    assert kw["datos_plantilla"]["fecha"] == "sábado 10 de octubre" and kw["datos_plantilla"]["hora"] == "9:00 am"
    assert kw["datos_plantilla"]["profesional"] == "Luis"
    cancelacion.evento.assert_called_once_with("appointment.cancelled", BIZ, "c1")


def test_el_profesional_cancela_su_propia_cita_pero_no_la_de_otro(cliente, como, cancelacion, db):
    db.sembrar("appointments", {"id": "c2", "business_id": BIZ, "employee_id": "emp-owner", "client_phone": "573009998877",
                                "client_name": "Ana", "service_id": "s1", "scheduled_at": "2026-10-10T10:00:00", "status": "confirmed"})
    assert cliente.post("/appointments/c2/cancel", headers=como("empleado")).status_code == 403  # es de otro profesional
    assert cliente.post("/appointments/c1/cancel", headers=como("empleado")).status_code == 200
    assert cliente.post("/appointments/c2/cancel", headers=como("desconocido")).status_code == 403


def test_cancelar_exige_sesion(cliente, cancelacion):
    assert cliente.post("/appointments/c1/cancel").status_code == 401


def test_cita_inexistente_ya_cancelada_o_completada(cliente, como, cancelacion, db):
    assert cliente.post("/appointments/fantasma/cancel", headers=como("dueno")).status_code == 404

    assert cliente.post("/appointments/c1/cancel", headers=como("dueno")).status_code == 200
    repetida = cliente.post("/appointments/c1/cancel", headers=como("dueno"))
    assert repetida.status_code == 409 and "ya estaba cancelada" in repetida.json()["detail"]
    assert cancelacion.aviso.call_count == 1  # no se avisa dos veces

    db.sembrar("appointments", {"id": "c3", "business_id": BIZ, "employee_id": "emp-staff", "client_phone": "573001234567",
                                "service_id": "s1", "scheduled_at": "2026-10-01T09:00:00", "status": "completed"})
    assert cliente.post("/appointments/c3/cancel", headers=como("dueno")).status_code == 400


def test_cita_sin_empleado_solo_la_cancela_el_dueno(cliente, como, cancelacion, db):
    db.sembrar("appointments", {"id": "c4", "business_id": BIZ, "employee_id": None, "client_phone": "573001234567",
                                "client_name": "Sin empleado", "service_id": "s1", "scheduled_at": "2026-10-10T11:00:00", "status": "confirmed"})
    assert cliente.post("/appointments/c4/cancel", headers=como("empleado")).status_code == 403
    assert cliente.post("/appointments/c4/cancel", headers=como("dueno")).status_code == 200


def test_cita_de_un_cliente_sin_whatsapp_se_cancela_sin_avisar(cliente, como, cancelacion, db):
    db.sembrar("appointments", {"id": "c5", "business_id": BIZ, "employee_id": "emp-staff", "client_phone": "",
                                "client_name": "Presencial", "service_id": "s1", "scheduled_at": "2026-10-10T10:00:00", "status": "confirmed"})
    r = cliente.post("/appointments/c5/cancel", headers=como("dueno")).json()
    assert r["cita"]["status"] == "cancelled" and r["aviso_cliente"] == "sin_whatsapp"
    cancelacion.aviso.assert_not_called()


def test_el_aviso_dice_si_las_plantillas_no_estan_activas_o_si_fallo(cliente, como, cancelacion, db):
    for id_, resultado, esperado in [
        ("c6", {"omitido": "plantillas v2 de WhatsApp no activas"}, "no_disponible"),
        ("c7", {"error": "template not found"}, "fallo"),
    ]:
        db.sembrar("appointments", {"id": id_, "business_id": BIZ, "employee_id": "emp-staff", "client_phone": "573001234567",
                                    "service_id": "s1", "scheduled_at": "2026-10-10T10:00:00", "status": "confirmed"})
        cancelacion.aviso.return_value = resultado
        r = cliente.post(f"/appointments/{id_}/cancel", headers=como("dueno"))
        assert r.status_code == 200 and r.json()["aviso_cliente"] == esperado and r.json()["cita"]["status"] == "cancelled"


def test_si_el_envio_lanza_error_la_cita_igual_se_cancela(cliente, como, cancelacion, capsys):
    cancelacion.aviso.side_effect = RuntimeError("Meta caido")
    r = cliente.post("/appointments/c1/cancel", headers=como("dueno"))
    assert r.status_code == 200 and r.json()["aviso_cliente"] == "fallo" and r.json()["cita"]["status"] == "cancelled"
    assert "No se pudo avisar la cancelacion" in capsys.readouterr().out


def test_cancelar_una_cita_con_pago_pendiente_expira_su_link_y_avisa_del_abono(cliente, como, cancelacion, db):
    db.sembrar("services", {"id": "s-abono", "business_id": BIZ, "name": "Tinte", "price": 80000, "requires_payment": True, "active": True})
    db.sembrar("appointments",
               {"id": "c8", "business_id": BIZ, "employee_id": "emp-staff", "client_phone": "573001234567", "client_name": "Pepe",
                "service_id": "s-abono", "scheduled_at": "2026-10-10T10:00:00", "status": "pending_payment"},
               {"id": "c9", "business_id": BIZ, "employee_id": "emp-staff", "client_phone": "573001234567", "client_name": "Pepe",
                "service_id": "s-abono", "scheduled_at": "2026-10-10T11:00:00", "status": "confirmed"})
    db.sembrar("wompi_payment_requests",
               {"id": "r1", "business_id": BIZ, "appointment_id": "c8", "status": "pending"},
               {"id": "r2", "business_id": BIZ, "appointment_id": "c8", "status": "approved"},
               {"id": "r3", "business_id": BIZ, "appointment_id": "otra", "status": "pending"})

    pendiente = cliente.post("/appointments/c8/cancel", headers=como("dueno")).json()
    assert pendiente["tiene_pago"] is False  # aun no pago nada
    estados = {r["id"]: r["status"] for r in db.filas("wompi_payment_requests")}
    assert estados == {"r1": "expired", "r2": "approved", "r3": "pending"}

    pagada = cliente.post("/appointments/c9/cancel", headers=como("dueno")).json()
    assert pagada["tiene_pago"] is True  # ya habia abonado: hay que coordinar la devolucion


def test_si_no_se_puede_expirar_el_link_igual_se_cancela(cliente, como, cancelacion, db, capsys):
    db.fallar_en["wompi_payment_requests"] = RuntimeError("tabla caida")
    r = cliente.post("/appointments/c1/cancel", headers=como("dueno"))
    assert r.status_code == 200 and r.json()["cita"]["status"] == "cancelled"
    assert "No se pudo expirar el link de pago" in capsys.readouterr().out


def test_si_la_cita_desaparece_mientras_se_cancela(cliente, como, cancelacion, monkeypatch):
    from routes import manual_appointments as ma

    monkeypatch.setattr(ma, "update_appointment_status", lambda *a, **k: None)
    assert cliente.post("/appointments/c1/cancel", headers=como("dueno")).status_code == 404


def test_un_fallo_del_tiempo_real_no_impide_cancelar(cliente, como, cancelacion, capsys):
    cancelacion.evento.side_effect = RuntimeError("socket cerrado")
    assert cliente.post("/appointments/c1/cancel", headers=como("dueno")).status_code == 200
    assert "tiempo real" in capsys.readouterr().out


def test_el_empleado_de_otro_negocio_no_cancela(cliente, como, cancelacion, db):
    db.sembrar("businesses", {"id": "b2", "name": "Otra", "owner_id": "otro-dueno", "blocked": False})
    db.sembrar("employees", {"id": "emp-b2", "business_id": "b2", "user_id": "dueno", "name": "X", "role": "staff", "active": True})
    db.sembrar("appointments", {"id": "c10", "business_id": BIZ, "employee_id": "emp-b2", "client_phone": "573001234567",
                                "service_id": "s1", "scheduled_at": "2026-10-10T10:00:00", "status": "confirmed"})
    r = cliente.post("/appointments/c10/cancel", headers=como("dueno"))
    assert r.status_code == 403
