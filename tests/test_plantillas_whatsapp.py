"""Plantillas v2 de WhatsApp para el cliente: datos, orden de variables, activacion segura y documentacion."""

import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import notificaciones_cliente as nc
from services import whatsapp as wa
from tests.test_servicios import RespuestaHttp

MIERCOLES_3PM = datetime(2026, 10, 14, 15, 0)
NEGOCIO = {"id": "biz-1", "name": "Barberia", "address": "Calle 10 # 5-20, local 3", "address_city": "Envigado"}


# --- datos -------------------------------------------------------------------------------------------------

def test_fecha_y_hora_para_el_cliente():
    assert nc.fecha_larga(MIERCOLES_3PM) == "miércoles 14 de octubre"
    assert nc.fecha_larga(datetime(2026, 1, 3, 9, 0)) == "sábado 3 de enero"
    assert nc.hora_12h(MIERCOLES_3PM) == "3:00 pm"
    assert nc.hora_12h(datetime(2026, 10, 14, 9, 30)) == "9:30 am"
    assert nc.hora_12h(datetime(2026, 10, 14, 12, 0)) == "12:00 pm"


def test_direccion_con_ciudad_sin_ciudad_y_sin_direccion():
    assert nc.direccion_del_negocio(NEGOCIO) == "Calle 10 # 5-20, local 3, Envigado"
    assert nc.direccion_del_negocio({"address": "Vereda La Cita, despues del puente"}) == "Vereda La Cita, despues del puente"
    for sin_direccion in (None, {}, {"address": "   "}, {"address": None, "address_city": "Envigado"}):
        assert nc.direccion_del_negocio(sin_direccion) == nc.DIRECCION_POR_DEFECTO


def test_datos_para_plantilla_nunca_trae_valores_vacios():
    datos = nc.datos_para_plantilla(NEGOCIO, MIERCOLES_3PM, "  Chenier ")
    assert datos["profesional"] == "Chenier" and datos["fecha"] == "miércoles 14 de octubre" and datos["hora"] == "3:00 pm"
    assert datos["enlace_reprogramacion"] == datos["enlace_agendamiento"] == f"{nc.FRONTEND_URL}/chat/biz-1"

    sin_nada = nc.datos_para_plantilla(None, MIERCOLES_3PM, None)
    assert sin_nada["profesional"] == nc.PROFESIONAL_POR_DEFECTO and sin_nada["direccion"] == nc.DIRECCION_POR_DEFECTO
    assert sin_nada["enlace_agendamiento"] == nc.FRONTEND_URL
    assert all(str(v).strip() for v in sin_nada.values())


def test_nombre_del_empleado(db):
    db.sembrar("employees", {"id": "emp-1", "name": "Chenier"})
    assert nc.nombre_del_empleado("emp-1") == "Chenier"
    assert nc.nombre_del_empleado(None) is None and nc.nombre_del_empleado("fantasma") is None


def test_nombre_del_empleado_si_la_lectura_falla(db):
    db.fallar_en["employees"] = RuntimeError("base caida")
    assert nc.nombre_del_empleado("emp-1") is None


# --- envio ---------------------------------------------------------------------------------------------------

@pytest.fixture
def meta(monkeypatch):
    llamadas = []
    monkeypatch.setattr(wa.requests, "post", lambda url, **kw: llamadas.append(kw) or RespuestaHttp(200, {"messages": [{"id": "w"}]}))
    return SimpleNamespace(llamadas=llamadas)


@pytest.fixture
def v2(monkeypatch):
    monkeypatch.setattr(wa, "WHATSAPP_PLANTILLAS_VERSION", "v2")


def _plantilla(meta, indice=-1):
    t = meta.llamadas[indice]["json"]["template"]
    return t["name"], [p["text"] for p in t["components"][0]["parameters"]]


DATOS = nc.datos_para_plantilla(NEGOCIO, MIERCOLES_3PM, "Chenier")


def test_v2_confirmacion(meta, v2):
    wa.enviar_confirmacion_cita_cliente("573001", "Ana", "Barberia", "Corte", "hoy", datos_plantilla=DATOS)
    assert _plantilla(meta) == ("confirmacion_cita_v2_goagenda", [
        "Ana", "Barberia", "miércoles 14 de octubre", "3:00 pm", "Corte", "Chenier", "Calle 10 # 5-20, local 3, Envigado"])


def test_v2_recordatorio(meta, v2):
    wa.enviar_recordatorio_cita_template("573001", "Ana", "Barberia", "Corte", "hoy", datos_plantilla=DATOS)
    assert _plantilla(meta) == ("recordatorio_cita_v2_goagenda", [
        "Ana", "Barberia", "miércoles 14 de octubre", "3:00 pm", "Corte", "Calle 10 # 5-20, local 3, Envigado",
        f"{nc.FRONTEND_URL}/chat/biz-1"])


def test_v2_reprogramacion_y_cancelacion(meta, v2):
    wa.enviar_reprogramacion_cita_cliente("573001", "Ana", "Barberia", DATOS)
    assert _plantilla(meta) == ("reprogramacion_cita_v2_goagenda", ["Ana", "Barberia", "miércoles 14 de octubre", "3:00 pm"])
    wa.enviar_cancelacion_cita_cliente("573001", "Ana", "Barberia", "Corte", DATOS)
    assert _plantilla(meta) == ("cancelacion_cita_v2_goagenda", [
        "Ana", "Barberia", "miércoles 14 de octubre", "3:00 pm", "Corte", f"{nc.FRONTEND_URL}/chat/biz-1"])


def test_el_nombre_de_cada_plantilla_se_configura_por_entorno(meta, v2, monkeypatch):
    monkeypatch.setenv("WHATSAPP_TEMPLATE_CANCELACION_V2", "cancelacion_v3")
    wa.enviar_cancelacion_cita_cliente("573001", "Ana", "B", "Corte", DATOS)
    assert _plantilla(meta)[0] == "cancelacion_v3"


def test_con_v1_todo_sigue_igual_y_no_se_envia_lo_que_no_existia(meta):
    """Mientras Meta no apruebe las plantillas v2 (WHATSAPP_PLANTILLAS_VERSION=v1) nada cambia."""
    wa.enviar_confirmacion_cita_cliente("573001", "Ana", "Barberia", "Corte", "hoy a las 3:00 pm", datos_plantilla=DATOS)
    assert _plantilla(meta) == ("confirmacion_cita_goagenda", ["Ana", "Barberia", "Corte", "hoy a las 3:00 pm"])
    wa.enviar_recordatorio_cita_template("573001", "Ana", "Barberia", "Corte", "hoy", datos_plantilla=DATOS)
    assert _plantilla(meta)[0] == "recordatorio_cita_goagenda"

    n = len(meta.llamadas)
    assert wa.enviar_reprogramacion_cita_cliente("573001", "Ana", "B", DATOS) == {"omitido": "plantillas v2 de WhatsApp no activas"}
    assert "omitido" in wa.enviar_cancelacion_cita_cliente("573001", "Ana", "B", "Corte", DATOS)
    assert len(meta.llamadas) == n  # no salio nada hacia Meta


def test_v2_sin_datos_cae_a_la_plantilla_v1(meta, v2):
    wa.enviar_confirmacion_cita_cliente("573001", "Ana", "Barberia", "Corte", "hoy")
    assert _plantilla(meta)[0] == "confirmacion_cita_goagenda"


def test_v2_sin_credenciales_no_envia(meta, v2, monkeypatch):
    monkeypatch.setattr(wa, "GOAGENDA_WHATSAPP_TOKEN", None)
    assert "no configuradas" in wa.enviar_reprogramacion_cita_cliente("573", "A", "B", DATOS)["error"]
    assert meta.llamadas == []


def test_un_parametro_vacio_no_sale_vacio(meta, v2):
    """Meta rechaza variables vacias: un dato en blanco se manda como '-'."""
    wa.enviar_reprogramacion_cita_cliente("573001", "  ", "Barberia", DATOS)
    assert _plantilla(meta)[1][0] == "-"


# --- documentacion para Meta ----------------------------------------------------------------------------------

DOC = Path(__file__).resolve().parent.parent / "docs" / "PLANTILLAS_WHATSAPP.md"


@pytest.mark.parametrize("tipo", list(wa.PLANTILLAS_V2))
def test_la_documentacion_trae_cada_plantilla_con_sus_variables(tipo):
    """Lo que se envia a aprobar en Meta tiene que calzar con lo que manda el codigo: nombre y numero de variables."""
    texto = DOC.read_text(encoding="utf-8")
    nombre = wa.PLANTILLAS_V2[tipo][1]
    variables = wa.PLANTILLAS_V2[tipo][2]
    bloque = re.search(rf"### `{nombre}`.*?```\n(.*?)```", texto, re.S)
    assert bloque, f"falta la plantilla {nombre} en docs/PLANTILLAS_WHATSAPP.md"
    usadas = sorted({int(n) for n in re.findall(r"\{\{(\d+)\}\}", bloque.group(1))})
    assert usadas == list(range(1, len(variables) + 1))
