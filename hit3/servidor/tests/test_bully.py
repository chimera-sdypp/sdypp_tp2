"""Tests del algoritmo Bully con una red en memoria: los mensajes van directo de un
`Bully` a otro, con los mismos códigos que los endpoints de main.py."""

import pytest

from app.bully import Bully
from app.config import Config
from app.lanzador import SinConexion, Vencido
from dobles import logger_silencioso


class Red:
    """Conecta los nodos `ids`. Un nodo caído no contesta (SinConexion)."""

    def __init__(self, ids, timeout_coordinador=0.05):
        self.caidos = set()
        self.mensajes = []
        self.nodos = {}
        for nodo in ids:
            pares = tuple((n, f"http://nodo{n}") for n in ids if n != nodo)
            config = Config(nodo_id=nodo, pares=pares, timeout_coordinador=timeout_coordinador)
            # Sin hilos: lo que iría en segundo plano corre en el momento, y así
            # cada elección termina antes de que el test mire el resultado.
            self.nodos[nodo] = Bully(config, logger_silencioso(), http=self._http,
                                     en_segundo_plano=lambda f: f())

    def _http(self, metodo, url, cuerpo=None, timeout=5.0):
        destino, ruta = url.removeprefix("http://nodo").split("/", 1)
        destino = int(destino)
        self.mensajes.append((cuerpo["de"], destino, ruta))
        if destino in self.caidos:
            raise SinConexion("connection refused")
        nodo = self.nodos[destino]
        if ruta == "cluster/eleccion":
            nodo.recibir_eleccion(cuerpo["de"])
            return 200, None
        if ruta == "cluster/coordinador":
            return (200 if nodo.recibir_coordinador(cuerpo["de"]) else 409), None
        if ruta == "cluster/heartbeat":
            return (200 if nodo.soy_coordinador() else 409), None
        raise AssertionError(ruta)

    def coordinadores(self):
        """Quién cree cada nodo vivo que es el coordinador."""
        return {n: b.coordinador for n, b in self.nodos.items() if n not in self.caidos}

    def caer(self, nodo):
        self.caidos.add(nodo)

    def volver(self, nodo):
        """Vuelve como un proceso nuevo: sin saber quién es el coordinador."""
        self.caidos.discard(nodo)
        viejo = self.nodos[nodo]
        self.nodos[nodo] = Bully(viejo._config, logger_silencioso(), http=self._http,
                                 en_segundo_plano=lambda f: f())


@pytest.fixture
def red():
    red = Red([1, 2, 3])
    red.nodos[1].iniciar_eleccion("arranque")
    return red


def test_al_arrancar_gana_el_id_mas_alto(red):
    assert red.coordinadores() == {1: 3, 2: 3, 3: 3}


def test_si_cae_el_coordinador_el_heartbeat_lo_detecta_y_gana_el_siguiente(red):
    red.caer(3)
    red.nodos[1].latir(tareas_en_curso=0)
    assert red.coordinadores() == {1: 2, 2: 2}


def test_la_secuencia_de_mensajes_es_la_de_bully(red):
    red.caer(3)
    red.mensajes.clear()
    red.nodos[1].latir(tareas_en_curso=0)
    # Los mensajes a varios nodos salen en paralelo: se compara sin orden.
    assert sorted(red.mensajes) == sorted([
        (1, 3, "cluster/heartbeat"),    # falla: el 1 detecta la caída
        (1, 2, "cluster/eleccion"),     # ELECCION a los mayores...
        (1, 3, "cluster/eleccion"),     # ...el 3 no contesta, el 2 contesta OK
        (2, 3, "cluster/eleccion"),     # el 2 arranca la suya: nadie mayor contesta
        (2, 1, "cluster/coordinador"),  # el 2 gana y se anuncia a todos
        (2, 3, "cluster/coordinador"),
    ])


def test_si_cae_tambien_el_segundo_queda_el_ultimo(red):
    red.caer(3)
    red.caer(2)
    red.nodos[1].latir(tareas_en_curso=0)
    assert red.coordinadores() == {1: 1}


def test_cuando_vuelve_el_mayor_recupera_el_puesto(red):
    red.caer(3)
    red.nodos[1].latir(tareas_en_curso=0)
    red.volver(3)
    red.nodos[3].iniciar_eleccion("arranque")
    assert red.coordinadores() == {1: 3, 2: 3, 3: 3}


def test_un_coordinador_menor_es_rechazado_y_el_mayor_se_impone(red):
    # El 3 está vivo pero el 2 se anuncia (p. ej. no le llegó a tiempo la respuesta del 3).
    for nodo in (1, 3):
        red.nodos[2]._http("POST", f"http://nodo{nodo}/cluster/coordinador", {"de": 2})
    assert red.coordinadores() == {1: 3, 2: 3, 3: 3}


def test_si_tras_el_ok_no_llega_coordinador_se_repite_la_eleccion():
    red = Red([1, 2])
    nodo2 = red.nodos[2]

    def contesta_ok_y_muere(de):
        red.caer(2)  # contesta OK pero muere antes de anunciarse

    nodo2.recibir_eleccion = contesta_ok_y_muere
    red.nodos[1].iniciar_eleccion("arranque")
    assert red.coordinadores() == {1: 1}


def test_heartbeat_a_un_nodo_que_no_es_coordinador_dispara_eleccion(red):
    # El 1 cree que el coordinador es el 2, pero el 2 sabe que es el 3.
    red.nodos[1]._coordinador = 2
    red.nodos[1].latir(tareas_en_curso=0)
    assert red.coordinadores() == {1: 3, 2: 3, 3: 3}


def test_si_mientras_late_llega_otro_coordinador_no_hay_segunda_eleccion():
    # El heartbeat sale hacia el 3 (que murió) y, antes de que falle, el 2 se anuncia.
    red = Red([1, 2, 3])
    red.nodos[3].iniciar_eleccion("arranque")
    red.caer(3)
    original = red._http

    def anuncio_en_vuelo(metodo, url, cuerpo=None, timeout=5.0):
        if url == "http://nodo3/cluster/heartbeat":
            red.nodos[1].recibir_coordinador(2)
        return original(metodo, url, cuerpo, timeout)

    red.nodos[1]._http = anuncio_en_vuelo
    red.mensajes.clear()
    red.nodos[1].latir(tareas_en_curso=0)
    assert red.nodos[1].coordinador == 2
    assert [m for m in red.mensajes if m[2] == "cluster/eleccion"] == []


def test_un_timeout_tambien_cuenta_como_caida():
    red = Red([1, 2])
    red.nodos[2].iniciar_eleccion("arranque")
    red.nodos[1]._coordinador = 2
    original = red._http

    def lento(metodo, url, cuerpo=None, timeout=5.0):
        if "nodo2" in url:
            raise Vencido("timed out")
        return original(metodo, url, cuerpo, timeout)

    red.nodos[1]._http = lento
    red.nodos[1].latir(tareas_en_curso=0)
    assert red.nodos[1].coordinador == 1


def test_el_coordinador_no_se_manda_heartbeats(red):
    red.mensajes.clear()
    red.nodos[3].latir(tareas_en_curso=0)
    assert red.mensajes == []


def test_al_asumir_se_avisa_una_vez():
    asumidos = []
    bully = Bully(Config(nodo_id=1), logger_silencioso(), al_asumir=lambda: asumidos.append(1))
    bully.iniciar_eleccion("arranque")
    assert bully.soy_coordinador() and asumidos == [1]


def test_no_arranca_dos_elecciones_a_la_vez():
    llamadas = []
    bully = None

    def http(metodo, url, cuerpo=None, timeout=5.0):
        llamadas.append(url)
        bully.iniciar_eleccion("otra vez, mientras sigue la primera")  # se ignora
        raise SinConexion("refused")

    bully = Bully(Config(nodo_id=1, pares=((2, "http://nodo2"),)), logger_silencioso(), http=http)
    bully.iniciar_eleccion("arranque")
    assert [u for u in llamadas if u.endswith("/eleccion")] == ["http://nodo2/cluster/eleccion"]


def test_esperar_coordinador_devuelve_el_actual_o_none_si_vence():
    bully = Bully(Config(nodo_id=1), logger_silencioso())
    assert bully.esperar_coordinador(timeout=0.01) is None
    bully.iniciar_eleccion("arranque")
    assert bully.esperar_coordinador(timeout=0.01) == 1
