"""Tests de integración: el servidor real, Docker real y la tarea de prueba.

Los corre `tests/integracion.sh`, que levanta todo desde cero. A mano:
    TP2_URL=http://127.0.0.1:8080 python -m pytest -m integracion -v
Suponen TP2_IMAGENES_PERMITIDAS=cerberusdistribuido/tarea-prueba y TP2_TIMEOUT_EJECUCION=4
(es lo que configura el script).
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


def pedir(metodo, ruta, cuerpo=None):
    datos = None if cuerpo is None else json.dumps(cuerpo).encode()
    pedido = urllib.request.Request(URL + ruta, data=datos, method=metodo,
                                    headers={"Content-Type": "application/json"})
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


def test_no_quedan_contenedores_tarea():
    """Va último (pytest respeta el orden del archivo): después de todo lo anterior."""
    cliente = docker.from_env()
    restantes = cliente.containers.list(all=True, filters={"ancestor": IMAGEN})
    assert restantes == [], [c.name for c in restantes]
