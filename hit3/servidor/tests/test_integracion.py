"""Tests de integración: el cluster real (3 nodos + nginx), Docker real y la tarea de prueba.

Los corre `tests/integracion.sh`, que levanta todo desde cero. A mano:
    TP2_URL=http://127.0.0.1:8080 TP2_URL_NODO1=http://127.0.0.1:18081 python -m pytest -m integracion -v
Suponen TP2_IMAGENES_PERMITIDAS=cerberusdistribuido/tarea-prueba y TP2_TIMEOUT_EJECUCION=4
(es lo que configura el script). Van en orden: los de caídas dejan el cluster como estaba.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request

import docker
import pytest

pytestmark = pytest.mark.integracion

URL = os.environ.get("TP2_URL", "http://127.0.0.1:8080").rstrip("/")
URL_NODO1 = os.environ.get("TP2_URL_NODO1", "http://127.0.0.1:18081").rstrip("/")
PROYECTO = "tp2-hit3"
IMAGEN = "cerberusdistribuido/tarea-prueba:test"
ETIQUETA = "tp2.hit3.nodo"


def pedir(metodo, ruta, cuerpo=None, url=URL, timeout=30):
    datos = None if cuerpo is None else json.dumps(cuerpo).encode()
    pedido = urllib.request.Request(url + ruta, data=datos, method=metodo,
                                    headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(pedido, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def tarea(calculo, parametros, imagen=IMAGEN, url=URL):
    return pedir("POST", "/getRemoteTask", {"calculo": calculo, "parametros": parametros, "imagen": imagen},
                 url=url)


def cluster():
    """El bloque `cluster` del /health de algún nodo (el que elija nginx)."""
    try:
        return pedir("GET", "/health", timeout=2)[1]["contenido"]["cluster"]
    except (OSError, ValueError, KeyError):
        return {}


def esperar(condicion, timeout=20.0):
    """Espera a que `condicion()` sea verdadera; devuelve cuánto tardó."""
    inicio = time.monotonic()
    while not condicion():
        assert time.monotonic() - inicio < timeout, "no se cumplió a tiempo"
        time.sleep(0.05)
    return time.monotonic() - inicio


def contenedor_del_nodo(nodo):
    [contenedor] = docker.from_env().containers.list(all=True, filters={
        "label": [f"com.docker.compose.project={PROYECTO}", f"com.docker.compose.service=nodo{nodo}"]})
    return contenedor


def todos_ven_al_coordinador(nodo):
    """Todos los nodos que contestan por nginx (los 3, round-robin) dicen lo mismo."""
    return all(cluster().get("coordinador") == nodo for _ in range(6))


def volver(nodo):
    """Levanta otra vez el nodo y espera a que el cluster quede como al principio."""
    contenedor_del_nodo(nodo).start()
    esperar(lambda: todos_ven_al_coordinador(3))
    esperar(lambda: len(cluster_registro()) == 3 and all(n["estado"] == "vivo" for n in cluster_registro().values()))


def cluster_registro():
    datos = cluster()
    return datos.get("nodos", {}) if datos.get("rol") == "coordinador" else _registro_del_coordinador()


def _registro_del_coordinador():
    for _ in range(6):  # round-robin: alguna vez contesta el coordinador
        datos = cluster()
        if datos.get("rol") == "coordinador":
            return datos["nodos"]
    return {}


# ---------------------------------------------------------------- funcionamiento normal
def test_health_y_coordinador():
    esperar(lambda: todos_ven_al_coordinador(3))
    vistos = {cluster().get("nodo") for _ in range(9)}
    assert vistos == {1, 2, 3}, "nginx tiene que repartir entre los 3 nodos"


def test_caso_feliz():
    codigo, cuerpo = tarea("suma", {"a": 3, "b": 4})
    assert codigo == 200, cuerpo
    assert cuerpo["contenido"]["resultado"] == 7
    assert cuerpo["contenido"]["nodo"] in {1, 2, 3}


def test_el_coordinador_reparte_las_tareas_entre_los_nodos():
    # El registro del coordinador se corrige con cada heartbeat (1 s): se espera uno
    # para que no arrastre las tareas de los tests anteriores.
    time.sleep(1.5)
    resultados = []

    def una(i):
        resultados.append(tarea("dormir", {"segundos": 1}))

    hilos = [threading.Thread(target=una, args=(i,)) for i in range(6)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()
    assert all(codigo == 200 for codigo, _ in resultados), resultados
    nodos = [c["contenido"]["nodo"] for _, c in resultados]
    # Va al menos una a cada nodo. El reparto exacto (2 y 2 y 2) no se garantiza: un
    # heartbeat que llega en medio de la ráfaga pisa la cuenta provisional.
    assert set(nodos) == {1, 2, 3}, nodos


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


@pytest.mark.parametrize("tamanio", [70 * 1024, 2 * 1024 * 1024])
def test_cuerpo_demasiado_grande_es_413_en_el_sobre(tamanio):
    # 2 MiB supera el límite por defecto de nginx (1 MiB): sin el error_page propio, sale en HTML.
    codigo, cuerpo = pedir("POST", "/getRemoteTask", {"calculo": "suma", "parametros": {"a": 1, "b": 1},
                                                      "datos": {"x": "a" * tamanio}, "imagen": IMAGEN})
    assert codigo == 413
    assert cuerpo["contenido"]["error"]["tipo"] == "CUERPO_DEMASIADO_GRANDE"


@pytest.mark.parametrize("ruta", ["/cluster/coordinador", "/cluster/eleccion", "/cluster/ejecutar"])
def test_las_rutas_del_cluster_no_se_publican(ruta):
    codigo, cuerpo = pedir("POST", ruta, {"de": 99})
    assert codigo == 404
    assert cuerpo["contenido"]["error"]["tipo"] == "RUTA_INEXISTENTE"


# ---------------------------------------------------------------- caídas
def test_si_matan_al_coordinador_se_elige_otro_y_las_tareas_siguen():
    contenedor_del_nodo(3).kill()  # SIGKILL al proceso: "matar el proceso"
    try:
        tardo = esperar(lambda: todos_ven_al_coordinador(2))
        print(f"\nnuevo coordinador en {tardo:.2f} s")
        for i in range(4):
            codigo, cuerpo = tarea("suma", {"a": i, "b": 1})
            assert codigo == 200, cuerpo
            assert cuerpo["contenido"]["nodo"] in {1, 2}
    finally:
        volver(3)  # el 3 vuelve y recupera el puesto: es el mayor


def test_si_matan_al_ejecutor_la_tarea_se_reasigna():
    """Dos tareas largas entran por el nodo 1; se mata a un ejecutor que no sea el 1
    mientras corren. Las dos terminan bien: la del nodo muerto, en otro."""
    resultados = []

    def una():
        resultados.append(tarea("dormir", {"segundos": 2}, url=URL_NODO1))

    hilos = [threading.Thread(target=una) for _ in range(2)]
    for hilo in hilos:
        hilo.start()

    def ejecutores():
        contenedores = docker.from_env().containers.list(filters={"label": ETIQUETA})
        return {int(c.labels[ETIQUETA]) for c in contenedores} - {1}

    esperar(lambda: ejecutores(), timeout=10)
    victima = min(ejecutores())  # si puede, no el coordinador (3)
    contenedor_del_nodo(victima).kill()
    try:
        for hilo in hilos:
            hilo.join()
        assert [codigo for codigo, _ in resultados] == [200, 200], resultados
        assert victima not in {c["contenido"]["nodo"] for _, c in resultados}
    finally:
        volver(victima)
    # Al volver, el nodo borró el contenedor tarea que le quedó huérfano al morir.
    esperar(lambda: not docker.from_env().containers.list(all=True, filters={"label": f"{ETIQUETA}={victima}"}),
            timeout=10)


def test_no_quedan_contenedores_tarea():
    """Va último (pytest respeta el orden del archivo): después de todo lo anterior."""
    cliente = docker.from_env()
    esperar(lambda: not cliente.containers.list(all=True, filters={"label": ETIQUETA}), timeout=10)
