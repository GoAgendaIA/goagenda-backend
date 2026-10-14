"""Pruebas de agent/tools.py: helpers de validacion y cada tool del agente (llamando .func directamente)."""

import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent import tools as t
from services import scheduling

BIZ = "biz-1"
AHORA = datetime(2026, 10, 7, 8, 0)  # miercoles
SABADO = "2026-10-10"


def humano(*textos):
    return [HumanMessage(content=x) for x in textos]


def contenido(cmd):
    """Texto del ToolMessage de un Command devuelto por una tool."""
    return cmd.update["messages"][0].content


@pytest.fixture(autouse=True)
def hora_fija(monkeypatch):
    monkeypatch.setattr(scheduling, "ahora_local", lambda: AHORA)
    monkeypatch.setattr(t, "ahora_local", lambda: AHORA)


@pytest.fixture(autouse=True)
def sin_efectos_externos(monkeypatch):
    efectos = MagicMock()
    monkeypatch.setattr(t, "geocodificar", lambda direccion: None)
    monkeypatch.setattr(t, "enviar_notificacion_escalamiento", efectos.push)
    monkeypatch.setattr(t.gestor_tiempo_real, "emitir", efectos.emitir)
    return efectos


@pytest.fixture
def negocio(db):
    db.sembrar("businesses", {"id": BIZ, "name": "Barberia", "home_visits_enabled": True, "fcm_token": "fcm-1"})
    emp = db.sembrar("employees", {"id": "emp-1", "business_id": BIZ, "name": "Chenier", "active": True, "role": "owner"})
    corte = {"id": "s-corte", "business_id": BIZ, "name": "Corte", "duration_minutes": 30, "price": 15000.0,
             "active": True, "offers_home_visit": True, "requires_payment": False}
    tinte = {"id": "s-tinte", "business_id": BIZ, "name": "Tinte", "duration_minutes": 60, "price": 80000.0,
             "active": True, "offers_home_visit": False, "requires_payment": True}
    db.sembrar("services", dict(corte), dict(tinte))
    db.sembrar("employee_services", {"employee_id": "emp-1", "service_id": "s-corte", "services": dict(corte)},
               {"employee_id": "emp-1", "service_id": "s-tinte", "services": dict(tinte)})
    for dia in ["mon", "tue", "wed", "thu", "fri", "sat"]:
        db.sembrar("employee_hours", {"employee_id": "emp-1", "day": dia, "is_open": True, "opening_time": "09:00:00",
                                      "closing_time": "12:00:00", "lunch_start": None, "lunch_end": None})
    db.sembrar("employee_hours", {"employee_id": "emp-1", "day": "sun", "is_open": False})
    db.sembrar("home_visit_zones", {"business_id": BIZ, "name": "Betulia", "fee": 5000, "active": True, "aliases": ["La Bet"]},
               {"business_id": BIZ, "name": "Urrao", "fee": 8000, "active": True, "aliases": []})
    return emp


# ------------------------------------------------------------------------------------
# Helpers de formato y validacion
# ------------------------------------------------------------------------------------

@pytest.mark.parametrize("h24, h12", [("09:00", "9:00 am"), ("12:30", "12:30 pm"), ("00:30", "12:30 am"), ("18:05", "6:05 pm")])
def test_formato_hora_12h(h24, h12):
    assert t._formato_hora_12h(h24) == h12


def test_horas_por_franja_agrupa_manana_y_tarde_con_todas_las_horas():
    horas = ["09:00", "11:30", "12:00", "14:30", "17:30"]
    assert t._horas_por_franja(horas) == "🌅 *En la mañana:*\n• 9:00 am\n• 11:30 am\n🌇 *En la tarde:*\n• 12:00 pm\n• 2:30 pm\n• 5:30 pm"
    assert t._horas_por_franja(["14:00"]) == "🌇 *En la tarde:*\n• 2:00 pm"
    assert t._horas_por_franja(["08:00"]) == "🌅 *En la mañana:*\n• 8:00 am"
    assert t._horas_por_franja([]) == ""


@pytest.mark.parametrize("precio, texto", [(15000, "$15.000"), (1500000.4, "$1.500.000"), ("7", "$7"), (None, "$0"), ("abc", "$0")])
def test_formato_precio_cop(precio, texto):
    assert t._formato_precio_cop(precio) == texto


@pytest.mark.parametrize("tel, visible", [("573001234567", "3001234567"), ("3001234567", "3001234567"), (None, ""), ("", "")])
def test_formato_telefono_visible(tel, visible):
    assert t._formato_telefono_visible(tel) == visible


def test_normalizar_y_ngramas():
    assert t._normalizar("  Medellín ÑANDÚ ") == "medellin nandu"
    assert t._generar_ngramas(["a", "b", "c"], 2) == ["a b", "b c"]
    assert t._generar_ngramas(["a"], 2) == [] and t._generar_ngramas(["a"], 0) == []


@pytest.mark.parametrize(
    "textos, esperado",
    [
        (["quiero cita para mañana"], True),
        (["el sábado a las 3"], True),
        (["para el 21"], True),
        (["21/09 porfa"], True),
        (["2026-10-10"], True),
        (["hola, quiero un corte"], False),
        (["vivo en la calle 24 nro 15-18"], False),
    ],
)
def test_cliente_dio_fecha(textos, esperado):
    assert t._cliente_dio_fecha(humano(*textos)) is esperado


def test_cliente_dio_fecha_ignora_mensajes_que_no_son_del_cliente():
    assert t._cliente_dio_fecha([AIMessage(content="¿te sirve mañana?")]) is False


def test_validar_domicilio(negocio, db):
    servicio_local = {"offers_home_visit": False}
    servicio_domicilio = {"offers_home_visit": True}
    assert t._validar_domicilio(BIZ, servicio_domicilio, None, []) is None
    assert "no se presta a domicilio" in t._validar_domicilio(BIZ, servicio_local, "Calle 1", humano("Calle 1"))
    assert "NO la inventes" in t._validar_domicilio(BIZ, servicio_domicilio, "Carrera 80 # 45-10 Belen", humano("hola"))
    assert t._validar_domicilio(BIZ, servicio_domicilio, "Carrera 80 # 45-10", humano("vivo en carrera 80 # 45-10")) is None
    db.filas("businesses")[0]["home_visits_enabled"] = False
    assert "no hace domicilios" in t._validar_domicilio(BIZ, servicio_domicilio, "Calle 1", humano("Calle 1"))


# ------------------------------------------------------------------------------------
# Resolucion de zonas de domicilio
# ------------------------------------------------------------------------------------

ZONAS = [{"name": "Betulia", "fee": 5000, "aliases": ["La Bet"]}, {"name": "El Poblado", "fee": 0, "aliases": []}]


def test_mejor_coincidencia_tolera_typos_y_multipalabra():
    zona, score = t._mejor_coincidencia_zona("vivo en betulua", ZONAS)
    assert zona["name"] == "Betulia" and score >= t._UMBRAL_ZONA_FUERTE
    zona, score = t._mejor_coincidencia_zona("barrio el pobladoo", ZONAS)
    assert zona["name"] == "El Poblado"
    assert t._mejor_coincidencia_zona("algo", []) == (None, 0)


def test_texto_menciona_zona_por_alias_o_typo():
    assert t._texto_menciona_zona("estoy en la bet", ZONAS[0])
    assert t._texto_menciona_zona("estoy en betulua", ZONAS[0])
    assert not t._texto_menciona_zona("estoy en medellin", ZONAS[0])


def test_zona_por_localidad():
    assert t._zona_por_localidad("Betulia", ZONAS)["name"] == "Betulia"
    assert t._zona_por_localidad("Bogota", ZONAS) is None


def test_inferir_zona_por_geocodificacion(monkeypatch):
    monkeypatch.setattr(t, "geocodificar", lambda d: None)
    assert t._inferir_zona_por_geocodificacion("x", ZONAS) == (None, None)
    monkeypatch.setattr(t, "geocodificar", lambda d: {"town": "Betulia", "state": "Antioquia"})
    assert t._inferir_zona_por_geocodificacion("x", ZONAS)[0]["name"] == "Betulia"
    monkeypatch.setattr(t, "geocodificar", lambda d: {"suburb": "Belen", "city": "Medellin"})
    assert t._inferir_zona_por_geocodificacion("x", ZONAS) == (None, "Belen")
    monkeypatch.setattr(t, "geocodificar", lambda d: {"country": "Colombia"})
    assert t._inferir_zona_por_geocodificacion("x", ZONAS) == (None, None)


class TestResolverZona:
    def test_sin_direccion_o_sin_zonas_configuradas(self, negocio, db):
        assert t._resolver_zona(BIZ, None, None, []) == (None, None)
        db.tablas["home_visit_zones"] = []
        assert t._resolver_zona(BIZ, "Calle 1", None, humano("Calle 1")) == (None, None)

    def test_zona_explicita_exacta_alias_y_typo(self, negocio):
        assert t._resolver_zona(BIZ, "Calle 1", "Betulia", humano("calle 1 en betulia"))[0]["name"] == "Betulia"
        assert t._resolver_zona(BIZ, "Calle 1", "la bet", humano("calle 1, la bet"))[0]["name"] == "Betulia"
        assert t._resolver_zona(BIZ, "Calle 1", "Betulua", humano("calle 1 en betulua"))[0]["name"] == "Betulia"

    def test_zona_explicita_fuera_de_cobertura_escala_al_negocio(self, negocio, sin_efectos_externos):
        zona, error = t._resolver_zona(BIZ, "Calle 1", "Envigado", humano("calle 1 envigado"), "sess-1", "573001")
        assert zona is None and "fuera de la zona de cobertura" in error
        sin_efectos_externos.emitir.assert_called_once()
        sin_efectos_externos.push.assert_called_once_with("fcm-1", "573001", BIZ, "sess-1")

    def test_zona_explicita_que_el_cliente_no_dijo_no_se_acepta(self, negocio):
        zona, error = t._resolver_zona(BIZ, "Calle 1", "Urrao", humano("calle 1"))
        assert zona is None and "todavia no ha dicho" in error

    def test_zona_inferida_del_texto(self, negocio):
        assert t._resolver_zona(BIZ, "Calle 1 Urrao", None, humano("calle 1 urrao"))[0]["name"] == "Urrao"

    def test_zona_inferida_por_geocodificacion(self, negocio, monkeypatch):
        monkeypatch.setattr(t, "geocodificar", lambda d: {"town": "Urrao"})
        assert t._resolver_zona(BIZ, "Carrera 5", None, humano("carrera 5"))[0]["name"] == "Urrao"

    def test_geocodificacion_sin_match_cae_al_texto(self, negocio, monkeypatch):
        monkeypatch.setattr(t, "geocodificar", lambda d: {"suburb": "Los Laureles II", "city": "Bogota"})
        assert t._resolver_zona(BIZ, "Cra 43 Betulua", None, humano("cra 43 betulua"))[0]["name"] == "Betulia"

    def test_coincidencia_dudosa_pide_confirmar_sin_escalar(self, negocio, sin_efectos_externos):
        zona, error = t._resolver_zona(BIZ, "Calle 1", None, humano("calle 1 por urraito"), "sess-1", "573")
        assert zona is None and "PREGUNTALE" in error
        sin_efectos_externos.emitir.assert_not_called()

    def test_lugar_geocodificado_fuera_de_cobertura_escala(self, negocio, monkeypatch, sin_efectos_externos):
        monkeypatch.setattr(t, "geocodificar", lambda d: {"suburb": "Belen", "city": "Medellin"})
        zona, error = t._resolver_zona(BIZ, "Cra 80", None, humano("cra 80"), "sess-1", "573")
        assert zona is None and "podria estar en Belen" in error
        sin_efectos_externos.emitir.assert_called_once()

    def test_sin_ninguna_pista_pide_el_municipio(self, negocio, sin_efectos_externos):
        zona, error = t._resolver_zona(BIZ, "en mi casa", None, humano("en mi casa"), "sess-1", "573")
        assert zona is None and "No puedo confirmar" in error
        sin_efectos_externos.emitir.assert_not_called()

    def test_sin_session_id_no_escala(self, negocio, sin_efectos_externos):
        t._resolver_zona(BIZ, "Calle 1", "Envigado", humano("calle 1"))
        sin_efectos_externos.emitir.assert_not_called()


def test_notificar_negocio_sin_token_push_igual_emite_en_tiempo_real(negocio, db, sin_efectos_externos):
    db.filas("businesses")[0]["fcm_token"] = None
    t._notificar_negocio_escalamiento(BIZ, "sess-1", "Ana")
    sin_efectos_externos.push.assert_not_called()
    evento = sin_efectos_externos.emitir.call_args.args[1]
    assert evento == {"type": "chat.escalated", "business_id": BIZ, "session_id": "sess-1", "client_name": "Ana"}


# ------------------------------------------------------------------------------------
# Tools
# ------------------------------------------------------------------------------------

class TestRegistrarTelefono:
    def test_numero_valido_que_el_cliente_escribio(self):
        cmd = t.registrar_telefono_cliente.func("300 123 4567", "tc", humano("mi numero es 300 123 4567"))
        assert cmd.update["client_phone"] == "573001234567"

    def test_numero_invalido(self):
        cmd = t.registrar_telefono_cliente.func("12345", "tc", humano("12345"))
        assert "client_phone" not in cmd.update and "no parece un WhatsApp" in contenido(cmd)

    def test_numero_inventado_por_el_modelo(self):
        cmd = t.registrar_telefono_cliente.func("3001234567", "tc", humano("hola"))
        assert "client_phone" not in cmd.update and "NO lo inventes" in contenido(cmd)


def test_consultar_empleados_disponibles(negocio, db):
    db.sembrar("employees", {"id": "emp-2", "business_id": BIZ, "name": None, "active": True})
    cmd = t.consultar_empleados_disponibles.func(BIZ, "tc")
    assert [o["value"] for o in cmd.update["ultimas_opciones"]] == ["Chenier", "Sin nombre"]
    # Filtrado por dia: emp-2 no tiene horario, asi que el domingo no aparece nadie y el sabado solo Chenier.
    assert [o["value"] for o in t.consultar_empleados_disponibles.func(BIZ, "tc", SABADO).update["ultimas_opciones"]] == ["Chenier"]
    assert t.consultar_empleados_disponibles.func(BIZ, "tc", "2026-10-11").update["ultimas_opciones"] == []
    assert len(t.consultar_empleados_disponibles.func(BIZ, "tc", "no-fecha").update["ultimas_opciones"]) == 2


def test_seleccionar_empleado(negocio, db):
    assert t.seleccionar_empleado.func("emp-1", BIZ, "tc").update["employee_id"] == "emp-1"
    assert "no existe" in contenido(t.seleccionar_empleado.func("nadie", BIZ, "tc"))
    assert "no existe" in contenido(t.seleccionar_empleado.func("emp-1", "otro-negocio", "tc"))


def test_consultar_servicios_disponibles(negocio, db):
    cmd = t.consultar_servicios_disponibles.func(BIZ, None, "tc")
    assert [o["label"] for o in cmd.update["ultimas_opciones"]] == ["Corte · 30 min · $15.000", "Tinte · 60 min · $80.000"]
    assert "offers_home_visit" in contenido(cmd)
    assert len(t.consultar_servicios_disponibles.func(BIZ, "emp-1", "tc").update["ultimas_opciones"]) == 2
    db.filas("businesses")[0]["home_visits_enabled"] = False
    assert "offers_home_visit" not in contenido(t.consultar_servicios_disponibles.func(BIZ, None, "tc"))


def test_seleccionar_servicio(negocio):
    cmd = t.seleccionar_servicio.func("s-corte", BIZ, "tc")
    assert cmd.update["service_id"] == "s-corte" and "SI ofrece domicilio" in contenido(cmd)
    assert "domicilio" not in contenido(t.seleccionar_servicio.func("s-tinte", BIZ, "tc"))
    assert "no existe" in contenido(t.seleccionar_servicio.func("nada", BIZ, "tc"))


class TestConsultarHoras:
    def test_requiere_empleado(self, negocio):
        assert "Primero hay que saber" in contenido(t.consultar_horas_disponibles.func(SABADO, BIZ, None, "tc", humano("el sabado")))

    def test_requiere_que_el_cliente_haya_dado_la_fecha(self, negocio):
        assert contenido(t.consultar_horas_disponibles.func(SABADO, BIZ, "emp-1", "tc", humano("hola"))) == t.MENSAJE_FALTA_FECHA

    def test_fecha_pasada(self, negocio):
        assert "ya paso" in contenido(t.consultar_horas_disponibles.func("2026-10-01", BIZ, "emp-1", "tc", humano("el 1")))

    def test_horas_libres_con_duracion_del_servicio(self, negocio):
        cmd = t.consultar_horas_disponibles.func(SABADO, BIZ, "emp-1", "tc", humano("el sabado"), "tinte")
        assert [o["value"] for o in cmd.update["ultimas_opciones"]] == ["9:00 am", "9:30 am", "10:00 am", "10:30 am", "11:00 am"]
        datos = json.loads(contenido(cmd))
        assert datos["negocio_abierto_ese_dia"] is True and datos["dia_semana"] == "sabado"
        # El texto lista TODAS las horas, las mismas que los botones, agrupadas en Mañana/Tarde.
        todas = [o["value"] for o in cmd.update["ultimas_opciones"]]
        lineas = datos["horas_por_franja"].split("\n")
        mostradas = [linea[2:] for linea in lineas if linea.startswith("• ")]
        assert mostradas == todas and lineas[0] == "🌅 *En la mañana:*"
        assert "muestra_para_mostrar" not in datos

    def test_dia_cerrado(self, negocio):
        cmd = t.consultar_horas_disponibles.func("2026-10-11", BIZ, "emp-1", "tc", humano("el domingo"), "Servicio que no existe")
        assert cmd.update["ultimas_opciones"] == [] and json.loads(contenido(cmd))["negocio_abierto_ese_dia"] is False


# --- pedir_confirmacion_cita -----------------------------------------------------------------

def _pedir(fecha_hora=f"{SABADO}T10:00:00", servicio="Corte", mensajes=None, client_phone="573001234567", **kw):
    return t.pedir_confirmacion_cita.func(
        servicio_nombre=servicio, fecha_hora=fecha_hora, nombre_cliente="Ana", business_id=BIZ, employee_id="emp-1",
        client_phone=client_phone, session_id="sess-1", tool_call_id="tc",
        mensajes=mensajes if mensajes is not None else humano("el sabado a las 10"), **kw,
    )


class TestPedirConfirmacion:
    def test_resumen_con_botones(self, negocio):
        cmd = _pedir()
        texto = contenido(cmd)
        assert "Servicio: *Corte* · $15.000" in texto and "sabado 10 de octubre a las 10:00 am" in texto
        assert "Número: *3001234567*" in texto and "Con:" not in texto  # un solo empleado: no se muestra
        assert [o["value"] for o in cmd.update["ultimas_opciones"]] == ["Si", "No"]

    def test_muestra_empleado_si_hay_varios(self, negocio, db):
        db.sembrar("employees", {"business_id": BIZ, "name": "Otro", "active": True})
        assert "Con: *Chenier*" in contenido(_pedir())

    def test_requiere_telefono_y_fecha(self, negocio):
        assert "OBLIGATORIO" in contenido(_pedir(client_phone=None))
        assert contenido(_pedir(mensajes=humano("hola"))) == t.MENSAJE_FALTA_FECHA

    def test_hora_ocupada_o_fuera_de_horario(self, negocio, db):
        assert "fuera del horario" in contenido(_pedir(fecha_hora=f"{SABADO}T18:00:00"))
        db.sembrar("appointments", {"business_id": BIZ, "employee_id": "emp-1", "scheduled_at": f"{SABADO}T10:00:00",
                                    "status": "confirmed", "services": {"duration_minutes": 30}})
        assert "Ya hay una cita" in contenido(_pedir())

    def test_fecha_hora_sin_formato_se_muestra_tal_cual(self, negocio):
        assert "Cuándo: *el sabado*" in contenido(_pedir(fecha_hora="el sabado"))

    def test_domicilio_con_recargo_muestra_total(self, negocio):
        msgs = humano("el sabado a las 10", "calle 5 # 10-20 en betulia")
        texto = contenido(_pedir(mensajes=msgs, direccion="Calle 5 # 10-20", zona="Betulia"))
        assert "Domicilio en: *Calle 5 # 10-20*" in texto and "Recargo por domicilio (Betulia): *+$5.000*" in texto
        assert "Total: *$20.000*" in texto

    def test_domicilio_fuera_de_cobertura(self, negocio):
        msgs = humano("el sabado a las 10", "calle 5 en envigado")
        assert "fuera de la zona" in contenido(_pedir(mensajes=msgs, direccion="Calle 5", zona="Envigado"))


# --- crear_cita -------------------------------------------------------------------------------------

def _crear(servicio="Corte", fecha_hora=f"{SABADO}T10:00:00", client_phone="573001234567", employee_id="emp-1",
           mensajes=None, employee_fijo=False, **kw):
    return t.crear_cita.func(
        servicio_nombre=servicio, fecha_hora=fecha_hora, nombre_cliente="Ana", business_id=BIZ,
        client_phone=client_phone, employee_id=employee_id, employee_fijo=employee_fijo, session_id="sess-1",
        mensajes=mensajes if mensajes is not None else humano("el sabado"), **kw,
    )


class TestCrearCita:
    def test_validaciones_previas(self, negocio):
        assert "numero de WhatsApp" in _crear(client_phone=None)["error"]
        assert "con que empleado" in _crear(employee_id=None)["error"]
        assert "No encontre el servicio" in _crear(servicio="Masaje")["error"]
        assert "fuera del horario" in _crear(fecha_hora=f"{SABADO}T18:00:00")["error"]

    def test_choque_con_otra_cita(self, negocio, db):
        db.sembrar("appointments", {"business_id": BIZ, "employee_id": "emp-1", "scheduled_at": f"{SABADO}T10:00:00",
                                    "status": "confirmed", "services": {"duration_minutes": 30}})
        assert "Ya hay una cita" in _crear()["error"]

    def test_domicilio_invalido(self, negocio):
        assert "no se presta a domicilio" in _crear(servicio="Tinte", direccion="Calle 1", mensajes=humano("calle 1"))["error"]

    def test_cita_sin_abono(self, negocio, monkeypatch):
        from services import appointment_confirmation as ac

        finalizar = MagicMock(return_value={"id": "cita-1"})
        monkeypatch.setattr(ac, "finalizar_creacion_cita", finalizar)
        msgs = humano("el sabado", "calle 5 # 10 en urrao")
        assert _crear(direccion=" Calle 5 # 10 ", zona="Urrao", mensajes=msgs) == {"cita_creada": [{"id": "cita-1"}]}
        kwargs = finalizar.call_args.kwargs
        assert kwargs["address"] == "Calle 5 # 10" and kwargs["home_visit_zone"] == "Urrao" and kwargs["home_visit_fee"] == 8000.0

    def test_cita_sin_abono_que_no_se_pudo_crear(self, negocio, monkeypatch):
        from services import appointment_confirmation as ac

        monkeypatch.setattr(ac, "finalizar_creacion_cita", lambda **kw: None)
        assert _crear() == {"cita_creada": []}

    def test_cita_con_abono_genera_link(self, negocio, monkeypatch):
        from services import appointment_confirmation as ac, wompi_payment_requests as wpr

        monkeypatch.setattr(ac, "crear_cita_pendiente_pago", lambda **kw: {"id": "cita-p"})
        solicitud = MagicMock(return_value={"checkout_url": "https://checkout.wompi.co/l/x", "amount_in_cents": 4000000,
                                            "description": "Abono"})
        monkeypatch.setattr(wpr, "crear_solicitud_pago", solicitud)
        r = _crear(servicio="Tinte", employee_fijo=True)
        assert r == {"pago_pendiente": True, "link_pago": "https://checkout.wompi.co/l/x", "monto_abono_cents": 4000000,
                     "descripcion_abono": "Abono"}
        assert solicitud.call_args.kwargs["employee_id"] == "emp-1" and solicitud.call_args.kwargs["appointment_id"] == "cita-p"
        _crear(servicio="Tinte", employee_fijo=False)
        assert solicitud.call_args.kwargs["employee_id"] is None

    def test_la_cita_creada_marca_la_conversacion_y_el_origen_es_ia(self, negocio, db, monkeypatch):
        from services import appointment_confirmation as ac

        finalizar = MagicMock(return_value={"id": "cita-1"})
        monkeypatch.setattr(ac, "finalizar_creacion_cita", finalizar)
        _crear()
        fila = db.filas("chat_sessions")[0]
        assert fila["session_id"] == "sess-1" and fila["ended_in_appointment"] is True and fila["appointment_id"] == "cita-1"

    def test_cita_con_abono_marca_la_conversacion_solo_si_hubo_link(self, negocio, db, monkeypatch):
        from services import appointment_confirmation as ac, wompi_payment_requests as wpr

        monkeypatch.setattr(ac, "crear_cita_pendiente_pago", lambda **kw: {"id": "cita-p"})
        monkeypatch.setattr(wpr, "crear_solicitud_pago", lambda **kw: None)
        _crear(servicio="Tinte")
        assert not [f for f in db.filas("chat_sessions") if f.get("ended_in_appointment")]

        monkeypatch.setattr(wpr, "crear_solicitud_pago",
                            lambda **kw: {"checkout_url": "https://x", "amount_in_cents": 1, "description": "Abono"})
        _crear(servicio="Tinte")
        assert db.filas("chat_sessions")[0]["appointment_id"] == "cita-p"

    def test_escalar_al_equipo_marca_la_conversacion(self, negocio, db):
        t._notificar_negocio_escalamiento(BIZ, "sess-1", "Ana")
        assert db.filas("chat_sessions")[0]["handed_off"] is True

    def test_cita_con_abono_horario_tomado_en_el_mismo_instante(self, negocio, monkeypatch):
        from services import appointment_confirmation as ac

        monkeypatch.setattr(ac, "crear_cita_pendiente_pago", lambda **kw: None)
        assert "se acaba de ocupar" in _crear(servicio="Tinte")["error"]

    @pytest.mark.parametrize("falla", [None, RuntimeError("wompi caido")])
    def test_cita_con_abono_sin_link_libera_el_cupo(self, negocio, monkeypatch, db, falla):
        from services import appointment_confirmation as ac, wompi_payment_requests as wpr

        cita = db.sembrar("appointments", {"status": "pending_payment"})
        monkeypatch.setattr(ac, "crear_cita_pendiente_pago", lambda **kw: cita)

        def solicitud(**kw):
            if falla:
                raise falla
            return None

        monkeypatch.setattr(wpr, "crear_solicitud_pago", solicitud)
        assert "no fue posible generar el link" in _crear(servicio="Tinte")["error"]
        assert db.filas("appointments")[0]["status"] == "cancelled"


# --- consultar / cancelar / reprogramar ----------------------------------------------------------------

@pytest.fixture
def cita_existente(negocio, db):
    return db.sembrar("appointments", {
        "business_id": BIZ, "employee_id": "emp-1", "client_phone": "573001234567", "client_name": "Ana",
        "service_id": "s-corte", "scheduled_at": f"{SABADO}T10:00:00", "status": "confirmed",
        "services": {"name": "Corte", "duration_minutes": 30, "requires_payment": False},
    })


@pytest.fixture
def efectos_cita(monkeypatch):
    from services import push_notifications, realtime

    efectos = MagicMock()
    monkeypatch.setattr(push_notifications, "enviar_notificacion_cita_cancelada", efectos.cancelada)
    monkeypatch.setattr(push_notifications, "enviar_notificacion_cita_reprogramada", efectos.reprogramada)
    monkeypatch.setattr(realtime, "emitir_evento_cita", efectos.evento)
    return efectos


def test_consultar_citas_cliente(cita_existente):
    assert "numero de WhatsApp" in t.consultar_citas_cliente.func(BIZ, None)["error"]
    assert [c["id"] for c in t.consultar_citas_cliente.func(BIZ, "573001234567")["citas"]] == [cita_existente["id"]]


class TestCancelarCita:
    def test_validaciones(self, cita_existente):
        assert "numero de WhatsApp" in t.cancelar_cita.func(cita_existente["id"], BIZ, None, "sess-1")["error"]
        assert "No encontre esa cita" in t.cancelar_cita.func(cita_existente["id"], BIZ, "573999", "sess-1")["error"]

    def test_cancela_cita_sin_pago(self, cita_existente, db, efectos_cita, sin_efectos_externos):
        r = t.cancelar_cita.func(cita_existente["id"], BIZ, "573001234567", "sess-1")
        assert r["resultado"][0]["status"] == "cancelled"
        efectos_cita.cancelada.assert_called_once()
        efectos_cita.evento.assert_called_once_with("appointment.cancelled", BIZ, cita_existente["id"])
        sin_efectos_externos.emitir.assert_not_called()  # no es escalamiento

    def test_cita_pagada_pide_confirmacion_y_luego_avisa_al_negocio(self, cita_existente, db, efectos_cita, sin_efectos_externos):
        cita_existente["services"]["requires_payment"] = True
        db.filas("appointments")[0]["services"]["requires_payment"] = True
        r = t.cancelar_cita.func(cita_existente["id"], BIZ, "573001234567", "sess-1")
        assert r["requiere_confirmacion_por_pago"] is True and "devolucion" in r["advertencia"]
        assert db.filas("appointments")[0]["status"] == "confirmed"

        t.cancelar_cita.func(cita_existente["id"], BIZ, "573001234567", "sess-1", confirmar_a_pesar_del_pago=True)
        assert db.filas("appointments")[0]["status"] == "cancelled"
        sin_efectos_externos.emitir.assert_called_once()

    def test_falla_de_notificacion_no_impide_cancelar(self, cita_existente, db, efectos_cita, capsys):
        efectos_cita.cancelada.side_effect = RuntimeError("firebase caido")
        t.cancelar_cita.func(cita_existente["id"], BIZ, "573001234567", "sess-1")
        assert db.filas("appointments")[0]["status"] == "cancelled"
        assert "No se pudo enviar la notificacion de cancelacion" in capsys.readouterr().out


def _reprogramar(cita_id, **kw):
    base = dict(appointment_id=cita_id, business_id=BIZ, client_phone="573001234567", session_id="sess-1", mensajes=humano("el sabado"))
    base.update(kw)
    return t.reprogramar_cita.func(**base)


class TestReprogramarCita:
    def test_validaciones(self, cita_existente):
        assert "numero de WhatsApp" in _reprogramar(cita_existente["id"], client_phone=None)["error"]
        assert "No encontre esa cita" in _reprogramar("otra")["error"]
        assert "fuera del horario" in _reprogramar(cita_existente["id"], nueva_fecha_hora=f"{SABADO}T18:00:00")["error"]

    def test_cambia_la_hora(self, cita_existente, db, efectos_cita):
        r = _reprogramar(cita_existente["id"], nueva_fecha_hora=f"{SABADO}T11:00:00")
        assert r == {"cita_reprogramada": True, "nueva_fecha_hora": f"{SABADO}T11:00:00"}
        assert db.filas("appointments")[0]["scheduled_at"] == f"{SABADO}T11:00:00"
        efectos_cita.reprogramada.assert_called_once()

    def test_choque_ignorando_la_propia_cita(self, cita_existente, db, efectos_cita):
        db.sembrar("appointments", {"business_id": BIZ, "employee_id": "emp-1", "scheduled_at": f"{SABADO}T11:00:00",
                                    "status": "confirmed", "services": {"duration_minutes": 30}})
        assert "Ya hay una cita" in _reprogramar(cita_existente["id"], nueva_fecha_hora=f"{SABADO}T11:00:00")["error"]
        assert "error" not in _reprogramar(cita_existente["id"], nueva_fecha_hora=f"{SABADO}T10:15:00")

    def test_pasa_a_domicilio_en_la_misma_cita(self, cita_existente, db, efectos_cita):
        msgs = humano("el sabado", "calle 5 # 10-20 en betulia")
        r = _reprogramar(cita_existente["id"], mensajes=msgs, direccion="Calle 5 # 10-20", zona="Betulia")
        assert r["es_domicilio_ahora"] is True and r["zona"] == "Betulia" and r["recargo_domicilio_cents"] == 500000
        fila = db.filas("appointments")[0]
        assert len(db.filas("appointments")) == 1 and fila["is_home_visit"] is True and fila["home_visit_fee"] == 5000.0

    def test_pasa_a_domicilio_sin_zonas_configuradas(self, cita_existente, db, efectos_cita):
        db.tablas["home_visit_zones"] = []
        msgs = humano("el sabado", "calle 5 # 10-20")
        r = _reprogramar(cita_existente["id"], mensajes=msgs, direccion="Calle 5 # 10-20")
        assert r["es_domicilio_ahora"] is True and r["zona"] is None and r["recargo_domicilio_cents"] == 0

    def test_domicilio_invalido_o_fuera_de_zona(self, cita_existente, efectos_cita):
        assert "NO la inventes" in _reprogramar(cita_existente["id"], direccion="Calle 9 sur", zona="Betulia")["error"]
        msgs = humano("el sabado", "calle 5 en envigado")
        assert "fuera de la zona" in _reprogramar(cita_existente["id"], mensajes=msgs, direccion="Calle 5", zona="Envigado")["error"]

    def test_vuelve_al_local(self, cita_existente, db, efectos_cita):
        db.filas("appointments")[0].update({"is_home_visit": True, "address": "Calle 1"})
        r = _reprogramar(cita_existente["id"], volver_al_local=True)
        assert r["es_domicilio_ahora"] is False and "direccion" not in r
        assert db.filas("appointments")[0]["is_home_visit"] is False and db.filas("appointments")[0]["address"] is None

    def test_falla_de_notificacion_no_impide_reprogramar(self, cita_existente, db, efectos_cita, capsys):
        efectos_cita.reprogramada.side_effect = RuntimeError("firebase caido")
        _reprogramar(cita_existente["id"], nueva_fecha_hora=f"{SABADO}T11:00:00")
        assert db.filas("appointments")[0]["scheduled_at"] == f"{SABADO}T11:00:00"
        assert "No se pudo enviar la notificacion de reprogramacion" in capsys.readouterr().out


# --- escalamiento y zonas --------------------------------------------------------------------------

def test_transferir_a_equipo(negocio, sin_efectos_externos):
    cmd = t.transferir_a_equipo.func(BIZ, "sess-1", None, "tc")
    assert cmd.update["transferido"] is True
    sin_efectos_externos.push.assert_called_once_with("fcm-1", "Un cliente", BIZ, "sess-1")


def test_escalar_por_confusion(negocio, sin_efectos_externos):
    cmd = t.escalar_por_confusion.func(BIZ, "sess-1", "573001", "tc")
    assert cmd.update["escalado"] is True and "confirmar eso con el equipo" in contenido(cmd)
    sin_efectos_externos.emitir.assert_called_once()


def test_consultar_zonas_domicilio(negocio, db):
    cmd = t.consultar_zonas_domicilio.func(BIZ, "tc")
    datos = json.loads(contenido(cmd))
    assert {"nombre": "Betulia", "recargo": "$5.000"} in datos["zonas"] and datos["sin_restriccion"] is False
    db.filas("businesses")[0]["home_visits_enabled"] = False
    assert "no hace domicilios" in contenido(t.consultar_zonas_domicilio.func(BIZ, "tc"))


def test_lista_de_tools_expuestas_al_modelo():
    nombres = {tool.name for tool in t.TOOLS}
    assert {"crear_cita", "cancelar_cita", "reprogramar_cita", "pedir_confirmacion_cita", "transferir_a_equipo"} <= nombres
    assert all(isinstance(tool.description, str) and tool.description for tool in t.TOOLS)


# --- T1.3: salidas de herramientas compactas ---------------------------------------------------------------

class TestSalidasCompactas:
    def test_servicios_solo_con_los_campos_que_usa_el_modelo(self, negocio, db):
        # Una fila "real" de la base trae columnas internas que el modelo no necesita.
        db.sembrar("services", {"id": "s-extra", "business_id": BIZ, "name": "Barba", "duration_minutes": 20, "price": 8000.0,
                                "active": True, "offers_home_visit": True, "requires_payment": False,
                                "payment_type": None, "payment_percentage": None, "payment_fixed_amount_cents": None,
                                "payment_description": "interno", "created_at": "2026-01-01T00:00:00"})
        datos = json.loads(contenido(t.consultar_servicios_disponibles.func(BIZ, None, "tc")))
        barba = next(s for s in datos["servicios"] if s["name"] == "Barba")
        assert barba == {"id": "s-extra", "name": "Barba", "duration_minutes": 20, "price": 8000, "offers_home_visit": True}
        assert all("business_id" not in s and "created_at" not in s and "active" not in s for s in datos["servicios"])

    def test_servicio_con_abono_conserva_su_configuracion_de_pago(self, negocio, db):
        db.sembrar("services", {"id": "s-abono", "business_id": BIZ, "name": "Color", "duration_minutes": 90, "price": 120000.0,
                                "active": True, "offers_home_visit": False, "requires_payment": True,
                                "payment_type": "percentage", "payment_percentage": 50.0, "payment_fixed_amount_cents": None})
        datos = json.loads(contenido(t.consultar_servicios_disponibles.func(BIZ, None, "tc")))
        color = next(s for s in datos["servicios"] if s["name"] == "Color")
        assert color["requires_payment"] is True and color["payment_type"] == "percentage" and color["payment_percentage"] == 50
        assert "payment_fixed_amount_cents" not in color

    def test_reduce_al_menos_40_por_ciento_los_tokens_de_la_lista_de_servicios(self, negocio, db):
        filas = db.filas("services")
        antes = str({"servicios": filas})  # el formato anterior: repr del dict completo de la base
        despues = contenido(t.consultar_servicios_disponibles.func(BIZ, None, "tc"))
        assert len(despues) <= len(antes) * 0.6

    def test_el_precio_sigue_visible_como_numero_para_la_red_anti_alucinacion(self, negocio):
        """_precios_no_respaldados busca numeros en las salidas de tools: el precio no puede desaparecer."""
        from agent import graph as g

        mensaje = ToolMessage(content=contenido(t.consultar_servicios_disponibles.func(BIZ, None, "tc")),
                              name="consultar_servicios_disponibles", tool_call_id="c")
        assert g._precios_no_respaldados("El corte cuesta $15.000", [mensaje], [], BIZ) == []
        assert g._precios_no_respaldados("El corte cuesta $99.000", [mensaje], [], BIZ) == [99000]

    def test_json_compacto_y_numeros(self):
        assert t._json_compacto({"a": [1, 2], "b": True, "n": "ñ"}) == '{"a":[1,2],"b":true,"n":"ñ"}'
        assert t._numero_limpio(15000.0) == 15000 and t._numero_limpio(15000.5) == 15000.5
        assert t._numero_limpio(None) is None and t._numero_limpio("abc") == "abc"
