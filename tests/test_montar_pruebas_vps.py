"""scripts/montar_pruebas_vps.sh: el ambiente de pruebas del VPS no debe tocar produccion ni los datos reales.

El script corre de verdad en un directorio temporal, con `docker`, `nginx`, `systemctl`, `cloudflared` y `curl`
reemplazados por programas que solo anotan lo que se les pidio (en `llamadas.log`)."""

import os
import pathlib
import subprocess

import pytest

RAIZ = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = RAIZ / "scripts" / "montar_pruebas_vps.sh"

CONFIG_TUNEL = """tunnel: abc
credentials-file: /x.json

ingress:
  - hostname: app.goagenda.online
    service: http://localhost:80

  - hostname: pruebas.goagenda.online
    service: http://localhost:80

  - hostname: api.goagenda.online
    service: http://localhost:80
  - service: http_status:404
"""


@pytest.fixture
def vps(tmp_path):
    """Un 'VPS' falso: carpetas de nginx/cloudflared/produccion y comandos que solo anotan lo que se les pide."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "llamadas.log"
    for nombre in ("docker", "systemctl", "curl", "cloudflared"):
        (bin_ / nombre).write_text(f'#!/bin/bash\necho "{nombre} $*" >> "{log}"\n')
    # nginx -t falla si existe la marca; asi se prueba el retroceso.
    (bin_ / "nginx").write_text(f'#!/bin/bash\necho "nginx $*" >> "{log}"\n[ -f "{tmp_path}/nginx_falla" ] && exit 1\nexit 0\n')
    for f in bin_.iterdir():
        f.chmod(0o755)

    sitios, disponibles = tmp_path / "enabled", tmp_path / "available"
    sitios.mkdir()
    disponibles.mkdir()
    (sitios / "pruebas").write_text("server { server_name pruebas.goagenda.online; root /var/www/php; }\n")
    (sitios / "goagenda").write_text("server { server_name app.goagenda.online; }\nserver { server_name api.goagenda.online; }\n")
    cf = tmp_path / "config.yml"
    cf.write_text(CONFIG_TUNEL)
    prod = tmp_path / "backend"
    prod.mkdir()
    (prod / ".env").write_text('SUPABASE_URL="https://prod.supabase.co"\n')

    entorno = {
        **os.environ,
        "PATH": f"{bin_}:{os.environ['PATH']}",
        "SUDO": "",
        "SIN_CONFIRMAR": "1",
        "DIR_PRUEBAS": str(tmp_path / "pruebas"),
        "DIR_PROD": str(prod),
        "NGINX_SITIOS": str(sitios),
        "NGINX_DISPONIBLES": str(disponibles),
        "NGINX_RESPALDOS": str(tmp_path / "respaldos"),
        "CF_CONFIG": str(cf),
    }

    class Vps:
        raiz, config_tunel, sitios_nginx, entorno_ = tmp_path, cf, sitios, entorno

        @staticmethod
        def correr(*args):
            return subprocess.run(["/bin/bash", str(SCRIPT), *args], capture_output=True, text=True, env=entorno, timeout=60)

        @staticmethod
        def llamadas():
            return log.read_text() if log.exists() else ""

        @staticmethod
        def env_pruebas(contenido):
            (tmp_path / "pruebas").mkdir(exist_ok=True)
            (tmp_path / "pruebas" / ".env").write_text(contenido)

    return Vps


def test_sin_env_de_pruebas_solo_se_levanta_el_front_y_la_api_queda_apagada(vps):
    r = vps.correr()
    assert r.returncode == 0, r.stderr + r.stdout
    assert "docker compose up -d app-pruebas" in vps.llamadas()
    assert "api-pruebas" not in vps.llamadas().split("compose up")[1].split("\n")[0]
    assert "APAGADA" in r.stdout


def test_con_el_mismo_supabase_que_produccion_la_api_de_pruebas_no_arranca(vps):
    vps.env_pruebas("SUPABASE_URL=https://prod.supabase.co\n")
    r = vps.correr()
    assert r.returncode == 0
    assert "api-pruebas" not in vps.llamadas().split("compose up")[1].split("\n")[0]
    assert "MISMO Supabase" in r.stdout


def test_la_comparacion_ignora_comillas_y_espacios(vps):
    vps.env_pruebas("SUPABASE_URL = 'https://prod.supabase.co'\nSUPABASE_URL=\"https://prod.supabase.co\"\n")
    # La linea con espacios no cuenta; la segunda (con comillas) es igual a la de produccion: queda apagada.
    r = vps.correr()
    assert "APAGADA" in r.stdout


def test_con_un_supabase_propio_arrancan_la_api_y_el_front(vps):
    vps.env_pruebas('SUPABASE_URL="https://pruebas.supabase.co"\n')
    r = vps.correr()
    assert r.returncode == 0, r.stderr
    assert "docker compose up -d api-pruebas app-pruebas" in vps.llamadas()


def test_el_compose_de_pruebas_es_un_proyecto_aparte_sin_tareas_ni_puertos_publicos(vps):
    vps.correr()
    compose = (vps.raiz / "pruebas" / "docker-compose.yml").read_text()
    assert "name: goagenda-pruebas" in compose  # el deploy de produccion usa --remove-orphans en otro proyecto
    assert "TAREAS_EN_SEGUNDO_PLANO: worker" in compose  # sin programador y sin worker: no manda recordatorios
    assert '"127.0.0.1:8001:8000"' in compose and '"127.0.0.1:8081:80"' in compose  # solo nginx local los ve
    assert "8000:8000" not in compose.replace("127.0.0.1:8001:8000", "")


def test_nginx_sirve_pruebas_y_el_sitio_php_se_respalda_sin_borrarse(vps):
    r = vps.correr()
    assert r.returncode == 0, r.stderr
    assert not (vps.sitios_nginx / "pruebas").exists()
    respaldos = list((vps.raiz / "respaldos").glob("pruebas-php.*"))
    assert len(respaldos) == 1 and "/var/www/php" in respaldos[0].read_text()
    sitio = (vps.sitios_nginx / "goagenda-pruebas").read_text()
    assert "127.0.0.1:8081" in sitio and "127.0.0.1:8001" in sitio
    assert "api-pruebas.goagenda.online" in sitio


def test_los_sitios_de_produccion_no_se_tocan(vps):
    antes = (vps.sitios_nginx / "goagenda").read_text()
    vps.correr()
    assert (vps.sitios_nginx / "goagenda").read_text() == antes


def test_el_tunel_agrega_api_pruebas_antes_de_la_regla_final_y_conserva_lo_demas(vps):
    r = vps.correr()
    assert r.returncode == 0, r.stderr
    nuevo = vps.config_tunel.read_text()
    assert nuevo.index("hostname: api-pruebas.goagenda.online") < nuevo.index("http_status:404")
    for host in ("app.goagenda.online", "pruebas.goagenda.online", "api.goagenda.online"):
        assert f"hostname: {host}" in nuevo
    assert list(vps.raiz.glob("config.yml.bak.*"))  # copia del tunel anterior
    assert "systemctl restart cloudflared" in vps.llamadas()


def test_es_idempotente_la_segunda_corrida_no_reinicia_el_tunel_ni_pisa_el_compose(vps):
    vps.correr()
    compose = (vps.raiz / "pruebas" / "docker-compose.yml")
    compose.write_text(compose.read_text() + "# editado a mano\n")
    (vps.raiz / "llamadas.log").unlink()
    r = vps.correr()
    assert r.returncode == 0
    assert "restart cloudflared" not in vps.llamadas()
    assert "# editado a mano" in compose.read_text()
    assert vps.config_tunel.read_text().count("api-pruebas.goagenda.online") == 1


def test_si_nginx_rechaza_la_configuracion_se_restaura_el_sitio_php_y_no_se_toca_el_tunel(vps):
    (vps.raiz / "nginx_falla").write_text("")
    antes = vps.config_tunel.read_text()
    r = vps.correr()
    assert r.returncode != 0
    assert (vps.sitios_nginx / "pruebas").exists()
    assert not (vps.sitios_nginx / "goagenda-pruebas").exists()
    assert vps.config_tunel.read_text() == antes
    assert "restart cloudflared" not in vps.llamadas()


def test_si_no_hay_confirmacion_el_tunel_queda_igual(vps):
    entorno = {**vps.entorno_, "SIN_CONFIRMAR": "0"}
    r = subprocess.run(["/bin/bash", str(SCRIPT)], input="n\n", capture_output=True, text=True, env=entorno, timeout=60)
    assert r.returncode != 0
    assert vps.config_tunel.read_text() == CONFIG_TUNEL


def test_revertir_apaga_pruebas_restaura_el_php_y_el_tunel(vps):
    vps.correr()
    r = vps.correr("--revertir")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "docker compose down" in vps.llamadas()
    assert (vps.sitios_nginx / "pruebas").exists()
    assert not (vps.sitios_nginx / "goagenda-pruebas").exists()
    assert vps.config_tunel.read_text() == CONFIG_TUNEL
