"""Tests de integración: el servidor real, Docker real y la tarea de prueba.

Los corre `tests/integracion.sh`, que levanta todo desde cero. A mano:
    TP2_URL=http://127.0.0.1:8080 python -m pytest -m integracion -v
Suponen la lista blanca y TP2_TIMEOUT_EJECUCION=4 que configura el script: tarea-prueba,
no-es-tarea (una imagen que termina enseguida) y tarea-fijada con un digest que no es el suyo.
"""

import json
import os
import time
import urllib.error
import urllib.request

import docker
import pytest

pytestmark = pytest.mark.integracion

URL = os.environ.get("TP2_URL", "http://127.0.0.1:8080").rstrip("/")
IMAGEN = "cerberusdistribuido/tarea-prueba:test"
# Los emite integracion.sh con el secreto del servidor (R4).
TOKEN = os.environ.get("TP2_TOKEN", "")
TOKEN_ADMIN = os.environ.get("TP2_TOKEN_ADMIN", "")


def pedir(metodo, ruta, cuerpo=None, token=TOKEN):
    datos = None if cuerpo is None else json.dumps(cuerpo).encode()
    cabeceras = {"Content-Type": "application/json"}
    if token:
        cabeceras["Authorization"] = f"Bearer {token}"
    pedido = urllib.request.Request(URL + ruta, data=datos, method=metodo, headers=cabeceras)
    try:
        with urllib.request.urlopen(pedido, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def tarea(calculo, parametros, imagen=IMAGEN):
    return pedir("POST", "/getRemoteTask", {"calculo": calculo, "parametros": parametros, "imagen": imagen})


def test_health():
    codigo, cuerpo = pedir("GET", "/health")
    assert codigo == 200
    assert cuerpo["contenido"]["servidor"] == "ok"
    assert cuerpo["contenido"]["docker"] == "ok"
    assert "pool" in cuerpo["contenido"]


def test_caso_feliz():
    codigo, cuerpo = tarea("suma", {"a": 3, "b": 4})
    assert codigo == 200
    assert cuerpo["contenido"]["calculo"] == "suma"
    assert cuerpo["contenido"]["resultado"] == 7
    assert "lamport_ts" in cuerpo["contenido"]


@pytest.mark.parametrize("calculo, parametros, codigo, tipo", [
    ("division", {"a": 1, "b": 0}, 422, "TAREA_RECHAZADA"),
    ("explotar", {}, 502, "TAREA_FALLIDA"),
    ("dormir", {"segundos": 10}, 504, "TAREA_SIN_RESPUESTA"),
])
def test_casos_no_felices_de_la_tarea(calculo, parametros, codigo, tipo):
    obtenido, cuerpo = tarea(calculo, parametros)
    assert obtenido == codigo, cuerpo
    assert cuerpo["contenido"]["error"]["tipo"] == tipo


def test_imagen_fuera_de_la_lista():
    codigo, cuerpo = tarea("suma", {"a": 1, "b": 1}, imagen="alpine:3.20")
    assert codigo == 403
    assert cuerpo["contenido"]["error"]["tipo"] == "IMAGEN_NO_PERMITIDA"


def test_tag_inexistente_de_un_repo_permitido():
    """Necesita Internet: el servidor intenta el pull y Docker Hub dice que no existe."""
    codigo, cuerpo = tarea("suma", {"a": 1, "b": 1}, imagen="cerberusdistribuido/tarea-prueba:no-existe-9")
    assert codigo == 422
    assert cuerpo["contenido"]["error"]["tipo"] == "IMAGEN_INEXISTENTE"


@pytest.mark.parametrize("imagen", ["Cerberus/Tarea:1.0", "cerberusdistribuido/tarea-prueba",
                                    "cerberusdistribuido/tarea-prueba:latest", "tarea-prueba:1;rm -rf /"])
def test_imagen_mal_formada(imagen):
    codigo, cuerpo = tarea("suma", {"a": 1, "b": 1}, imagen=imagen)
    assert codigo == 422
    assert cuerpo["contenido"]["error"]["tipo"] == "PAYLOAD_INVALIDO"
    assert cuerpo["contenido"]["error"]["detalles"][0]["campo"] == "imagen"


def test_digest_distinto_del_fijado():
    codigo, cuerpo = tarea("suma", {"a": 1, "b": 1}, imagen="cerberusdistribuido/tarea-fijada:test")
    assert codigo == 403
    assert "digest" in cuerpo["contenido"]["error"]["mensaje"]


def test_imagen_que_no_es_un_servicio_tarea():
    """El contenedor termina enseguida: error claro sin esperar los 30 s de arranque."""
    inicio = time.monotonic()
    codigo, cuerpo = tarea("suma", {"a": 1, "b": 1}, imagen="cerberusdistribuido/no-es-tarea:test")
    assert codigo == 502
    assert cuerpo["contenido"]["error"]["tipo"] == "TAREA_FALLIDA"
    assert "no es un servicio tarea" in cuerpo["contenido"]["error"]["mensaje"]
    assert time.monotonic() - inicio < 10


@pytest.mark.parametrize("imagen", [IMAGEN, "cerberusdistribuido/no-es-tarea:test"])
def test_no_quedan_contenedores_tarea(imagen):
    """Va último (pytest respeta el orden del archivo): después de todo lo anterior."""
    cliente = docker.from_env()
    restantes = cliente.containers.list(all=True, filters={"ancestor": imagen})
    assert restantes == [], [c.name for c in restantes]


def test_sin_token_o_con_token_de_usuario_no_se_pasa():
    codigo, cuerpo = pedir("POST", "/getRemoteTask", {"calculo": "suma", "parametros": {"a": 1, "b": 1}, "imagen": IMAGEN}, token="")
    assert (codigo, cuerpo["contenido"]["error"]["tipo"]) == (401, "NO_AUTORIZADO")
    codigo, cuerpo = pedir("GET", "/stats")
    assert (codigo, cuerpo["contenido"]["error"]["tipo"]) == (403, "ROL_INSUFICIENTE")
    codigo, _ = pedir("GET", "/stats", token="")
    assert codigo == 401


def test_stats_cuenta_lo_que_se_ejecuto():
    tarea("suma", {"a": 1, "b": 2})
    tarea("division", {"a": 1, "b": 0})            # la tarea la rechaza: fallida
    codigo, cuerpo = pedir("GET", "/stats", token=TOKEN_ADMIN)
    stats = cuerpo["contenido"]
    assert codigo == 200
    assert stats["tareas_ejecutadas"] >= 1 and stats["tareas_fallidas"] >= 1
    assert any(imagen.endswith("tarea-prueba:test") for imagen in stats["tareas_por_imagen"])
    assert stats["latencia_s"]["p95"] is not None
    assert (stats["workers_max"], stats["tareas_en_cola"]) == (4, 0)
