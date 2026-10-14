"""Pruebas (offline) de la propia herramienta de evaluacion: escenarios, entorno, evaluador, ejecutor y reportes."""

import json
import uuid
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage

from evaluacion import ejecutor, negocios
from evaluacion.escenarios import ESCENARIOS, Escenario
from tests.fakes import ModeloGuionado

CLAVES_VALIDAS = {"llama", "no_llama", "responde_con", "no_responde_con", "citas_nuevas", "cita", "sin_cita_nueva_en",
                  "estado_cita", "cita_programada_en", "max_llamadas"}


# --- escenarios ---------------------------------------------------------------------------------------------

def test_el_conjunto_tiene_al_menos_30_escenarios_con_ids_unicos():
    ids = [e.id for e in ESCENARIOS]
    assert len(ids) >= 30 and len(set(ids)) == len(ids)


def test_cada_escenario_esta_bien_formado():
    for e in ESCENARIOS:
        assert isinstance(e, Escenario) and e.turnos and all(t.strip() for t in e.turnos), e.id
        assert e.preset in negocios.PRESETS, e.id
        assert set(e.esperar) <= CLAVES_VALIDAS, (e.id, set(e.esperar) - CLAVES_VALIDAS)
        assert e.esperar, f"{e.id} no espera nada"
        previas = {c["id"] for c in e.citas_previas}
        assert len(previas) == len(e.citas_previas), e.id
        for clave in ("estado_cita", "cita_programada_en"):
            assert set(e.esperar.get(clave, {})) <= previas, (e.id, clave)


def test_cubren_las_categorias_que_pide_la_estrategia():
    categorias = {e.categoria for e in ESCENARIOS}
    assert {"agendar", "gestionar", "identificacion", "humano", "seguridad", "domicilios", "abonos", "equipo"} <= categorias


# --- negocios de prueba -------------------------------------------------------------------------------------

@pytest.mark.parametrize("preset", negocios.PRESETS)
def test_cada_preset_se_siembra_completo(db, preset):
    datos = negocios.sembrar(db, preset)
    assert datos["business_id"] == negocios.BIZ_ID
    assert {e["id"] for e in db.filas("employees")} == set(datos["employees"])
    assert db.filas("employee_hours") and db.filas("employee_services") and datos["precios_validos"] >= {15000, 22000}
    negocio = db.filas("businesses")[0]
    assert negocio["home_visits_enabled"] is (preset == "domicilios")
    assert bool(db.filas("home_visit_zones")) is (preset == "domicilios")
    assert ("tinte" in datos["services"]) is (preset == "abono")
    assert (len(datos["employees"]) == 2) is (preset == "equipo")
    assert (40000 in datos["precios_validos"]) is (preset == "abono")


def test_preset_desconocido(db):
    with pytest.raises(ValueError):
        negocios.sembrar(db, "inexistente")


# --- evaluador ----------------------------------------------------------------------------------------------

def resultado(**campos):
    base = dict(escenario_id="x", titulo="x", categoria="x", respuestas=["ok"], herramientas=[])
    return ejecutor.Resultado(**{**base, **campos})


def esc(**esperar):
    return Escenario(id="x", titulo="x", categoria="x", turnos=("hola",), esperar=esperar)


def test_evaluar_herramientas_y_respuestas():
    r = resultado(herramientas=["consultar_servicios_disponibles"], respuestas=["Corte de cabello $15.000"])
    assert ejecutor.evaluar(esc(llama=["consultar_servicios_disponibles"], responde_con=[r"15\.?000"],
                                no_llama=["crear_cita"], no_responde_con=[r"cedula"]), r, {15000}) == []
    fallos = ejecutor.evaluar(esc(llama=["crear_cita"], no_llama=["consultar_servicios_disponibles"],
                                  responde_con=[r"zzz"], no_responde_con=[r"corte"]), r, {15000})
    assert len(fallos) == 4


def test_evaluar_citas_y_estados():
    r = resultado(citas_nuevas=[{"scheduled_at": "2026-10-10T10:00:00", "status": "confirmed", "is_home_visit": True}],
                  citas={"apt-1": {"status": "cancelled", "scheduled_at": "2026-10-10T11:00:00"}})
    ok = esc(citas_nuevas=1, cita={"scheduled_at": "2026-10-10T10:00", "status": "confirmed"},
             estado_cita={"apt-1": "cancelled"}, cita_programada_en={"apt-1": "2026-10-10T11:00"},
             sin_cita_nueva_en=["2026-10-11"])
    assert ejecutor.evaluar(ok, r, set()) == []

    mal = esc(citas_nuevas=2, cita={"status": "pending_payment"}, estado_cita={"apt-1": "confirmed"},
              cita_programada_en={"apt-1": "2026-10-12"}, sin_cita_nueva_en=["2026-10-10"])
    assert len(ejecutor.evaluar(mal, r, set())) == 5
    assert "debia crearse una cita" in ejecutor.evaluar(esc(cita={"status": "x"}), resultado(), set())[0]


def test_evaluar_tope_de_llamadas():
    assert ejecutor.evaluar(esc(max_llamadas=3), resultado(llamadas_modelo=3), set()) == []
    assert "5 llamadas" in ejecutor.evaluar(esc(max_llamadas=3), resultado(llamadas_modelo=5), set())[0]


def test_invariantes_hora_24h_y_precios_inventados():
    validos = {15000, 20000}
    assert ejecutor.evaluar(esc(), resultado(respuestas=["Te espero a las 3:00 pm o 10:30 am, $15.000"]), validos) == []
    assert ejecutor.evaluar(esc(), resultado(respuestas=["Te espero a las 15:00"]), validos) == ["invariante: mostro una hora en formato 24 h"]
    # "9:00" a secas no es 24 h inequivoca; solo se marca de 13:00 a 23:59 sin am/pm.
    assert ejecutor.evaluar(esc(), resultado(respuestas=["a las 9:00"]), validos) == []
    inventado = ejecutor.evaluar(esc(), resultado(respuestas=["Cuesta $99.000"]), validos)
    assert inventado == ["invariante: precio inventado $99.000"]
    assert ejecutor.evaluar(replace(esc(), invariantes=False), resultado(respuestas=["15:00 $99.000"]), validos) == []


# --- ejecucion ----------------------------------------------------------------------------------------------

def test_ejecutar_escenario_con_el_grafo_real_y_un_modelo_guionado(db, monkeypatch):
    from agent import graph

    ejecutor.preparar_entorno(monkeypatch)
    medidor = ejecutor.Medidor("gpt-4.1-mini")
    monkeypatch.setattr(graph, "registrar_uso", medidor.registrar)

    def con_uso(mensaje):  # el doble no trae usage_metadata: lo agregamos para medir
        mensaje.usage_metadata = {"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050,
                                  "input_token_details": {"cache_read": 800}}
        return mensaje

    llamada = AIMessage(content="", tool_calls=[{"name": "consultar_servicios_disponibles", "args": {}, "id": "c1", "type": "tool_call"}])
    final = AIMessage(content="Hola 👋 Tenemos *Corte de cabello* por $15.000.")

    def modelo_con_medicion():
        guion = ModeloGuionado(llamada, final)
        original = guion.invoke

        def invocar(mensajes):
            respuesta = original(mensajes)
            graph.registrar_uso(negocios.BIZ_ID, 1000, 50, 1050, cached_tokens=800)
            return respuesta

        guion.invoke = invocar
        return guion

    modelo = modelo_con_medicion()
    monkeypatch.setattr(graph, "_model", modelo)
    escenario = next(e for e in ESCENARIOS if e.id == "saludo_muestra_servicios")

    r = ejecutor.ejecutar_escenario(escenario, db, graph.enviar_mensaje, medidor)
    assert r.aprobado, r.fallos
    assert r.herramientas == ["consultar_servicios_disponibles"]
    assert r.llamadas_modelo == 2 and r.tokens_entrada == 2000 and r.tokens_cacheados == 1600 and r.costo_usd > 0
    assert r.citas_nuevas == []


def test_un_error_del_modelo_queda_registrado_sin_tumbar_la_corrida(db):
    def enviar(business_id, session_id, texto):
        raise RuntimeError("OpenAI caido")

    r = ejecutor.ejecutar_escenario(ESCENARIOS[0], db, enviar, ejecutor.Medidor(), herramientas_de=lambda b, s: [])
    assert r.error == "RuntimeError: OpenAI caido" and not r.aprobado and r.fallos == []


def test_el_tope_de_gasto_omite_el_resto_de_los_escenarios(db):
    medidor = ejecutor.Medidor("gpt-4.1-mini")

    def enviar(business_id, session_id, texto):
        medidor.registrar(business_id, 1_000_000, 0, 1_000_000)  # cada turno cuesta US$ 0.40
        return "ok", None

    de_un_turno = [e for e in ESCENARIOS if len(e.turnos) == 1][:5]
    resultados = ejecutor.ejecutar_todos(de_un_turno, db, enviar, medidor, tope_usd=0.5,
                                         herramientas_de=lambda b, s: [])
    ejecutados = [r for r in resultados if not r.omitido]
    assert len(ejecutados) == 2 and all(r.omitido for r in resultados[2:])
    assert ejecutor.tope_alcanzado(medidor, 0.5) and not ejecutor.tope_alcanzado(medidor, None)


# --- reporte -----------------------------------------------------------------------------------------------

def _resultados_de_ejemplo():
    bien = resultado(escenario_id="a", categoria="agendar", llamadas_modelo=4, tokens_entrada=40000,
                     tokens_cacheados=30000, tokens_salida=500, costo_usd=0.01)
    mal = resultado(escenario_id="b", categoria="seguridad", llamadas_modelo=2, tokens_entrada=20000,
                    tokens_salida=100, costo_usd=0.008, fallos=["no debia llamar crear_cita"])
    return [bien, mal, ejecutor.Resultado(escenario_id="c", titulo="c", categoria="x", omitido=True)]


def test_resumen_del_reporte():
    r = ejecutor.resumir(_resultados_de_ejemplo(), "base", "gpt-4.1-mini", {"prompt": "v1"}, tope_usd=1.0)
    t = r["totales"]
    assert t["escenarios"] == 3 and t["ejecutados"] == 2 and t["omitidos_por_tope"] == 1 and t["aprobados"] == 1
    assert t["acierto"] == 0.5 and t["porcentaje_cache"] == 50.0 and t["costo_usd"] == 0.018
    assert r["por_categoria"] == {"agendar": {"escenarios": 1, "aprobados": 1}, "seguridad": {"escenarios": 1, "aprobados": 0}}


def test_reporte_vacio_no_divide_por_cero():
    t = ejecutor.resumir([], "x", "m", {})["totales"]
    assert t["acierto"] is None and t["porcentaje_cache"] is None and t["llamadas_por_escenario"] is None


def test_guardar_y_comparar_reportes(tmp_path):
    base = ejecutor.resumir(_resultados_de_ejemplo(), "base", "m", {"prompt": "v1"})
    ruta = ejecutor.guardar_reporte(base, tmp_path)
    assert ruta.parent == tmp_path and "base" in ruta.name
    assert json.loads(ruta.read_text(encoding="utf-8"))["totales"]["aprobados"] == 1

    mejor = [resultado(escenario_id="a", categoria="agendar", llamadas_modelo=4, tokens_entrada=20000, costo_usd=0.005),
             resultado(escenario_id="b", categoria="seguridad", llamadas_modelo=2, tokens_entrada=10000, costo_usd=0.004)]
    nuevo = ejecutor.resumir(mejor, "v2", "m", {"prompt": "v2"})
    c = ejecutor.comparar(base, nuevo)
    assert c["tokens_entrada"]["variacion_pct"] == -50.0 and c["mejoras"] == ["b"] and c["regresiones"] == []
    assert c["acierto"] == {"antes": 0.5, "despues": 1.0}

    peor = ejecutor.resumir([resultado(escenario_id="a", categoria="agendar", fallos=["x"]),
                             resultado(escenario_id="b", categoria="seguridad", fallos=["y"])], "v3", "m", {})
    assert ejecutor.comparar(base, peor)["regresiones"] == ["a"]


def test_comparar_desde_la_linea_de_comandos(tmp_path, capsys):
    from evaluacion import comparar as cli

    base = ejecutor.guardar_reporte(ejecutor.resumir(_resultados_de_ejemplo(), "base", "m", {"prompt": "v1"}), tmp_path)
    peor = ejecutor.guardar_reporte(
        ejecutor.resumir([resultado(escenario_id="a", categoria="agendar", fallos=["x"]),
                          resultado(escenario_id="b", categoria="seguridad", fallos=["y"])], "v2", "m", {"prompt": "v2"}),
        tmp_path)
    assert cli.main(["x", str(base), str(base)]) == 0  # sin regresiones
    assert cli.main(["x", str(base), str(peor)]) == 1  # "a" paso de aprobado a fallido
    assert "configuracion antes" in capsys.readouterr().out
    assert cli.main(["x"]) == 2


def test_las_citas_previas_del_escenario_se_siembran_y_se_reportan(db):
    escenario = next(e for e in ESCENARIOS if e.id == "reprogramar_cita")

    def enviar(business_id, session_id, texto):
        # Simula una reprogramacion hecha por la herramienta real: misma cita, nueva fecha.
        db.filas("appointments")[0]["scheduled_at"] = "2026-10-10T11:00:00"
        return "Listo, quedo para el sabado a las 11:00 am", None

    r = ejecutor.ejecutar_escenario(escenario, db, enviar, ejecutor.Medidor(),
                                    herramientas_de=lambda b, s: ["reprogramar_cita"])
    assert set(r.citas) == {"apt-1"} and r.citas_nuevas == []
    assert r.citas["apt-1"]["client_phone"] == "573001234567"
    assert r.aprobado, r.fallos
