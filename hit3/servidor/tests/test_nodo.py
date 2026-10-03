"""Tests del flujo de una tarea por el cluster: asignación, reenvío y redistribución."""

import pytest

from app.config import Config
from app.errores import ErrorApi, TipoError
from app.imagenes import parsear
from app.lanzador import SinConexion, Vencido
from app.nodo import Nodo
from dobles import logger_silencioso, sobre

IMAGEN = parsear("cerberusdistribuido/tarea:1.0")
PARES = ((2, "http://nodo2"), (3, "http://nodo3"))


class LanzadorFalso:
    def __init__(self):
        self.llamadas = []
        self.huerfanos_limpiados = False
        self.al_ejecutar = None

    def ejecutarTareaRemota(self, imagen, calculo, parametros, datos):  # noqa: N802
        self.llamadas.append((imagen.referencia, calculo))
        if self.al_ejecutar:
            self.al_ejecutar()
        return 7

    def limpiar_huerfanos(self):
        self.huerfanos_limpiados = True
        return 0


class Http:
    """Contesta según la URL: una respuesta, una excepción o una lista (una por llamada).
    Una URL sin respuesta configurada no contesta (SinConexion)."""

    def __init__(self, rutas=None):
        self.rutas = rutas or {}
        self.pedidos = []

    def __call__(self, metodo, url, cuerpo=None, timeout=5.0):
        self.pedidos.append((url, cuerpo))
        respuesta = self.rutas.get(url, SinConexion("connection refused"))
        if isinstance(respuesta, list):
            respuesta = respuesta.pop(0) if len(respuesta) > 1 else respuesta[0]
        if isinstance(respuesta, BaseException):
            raise respuesta
        return respuesta

    def a(self, url):
        return [cuerpo for u, cuerpo in self.pedidos if u == url]


def _nodo(http=None, coordinador=None, **cambios):
    config = Config(nodo_id=1, pares=PARES, timeout_asignacion=0.2, **cambios)
    lanzador = LanzadorFalso()
    nodo = Nodo(config, logger_silencioso(), lanzador, http=http or Http(), dormir=lambda s: None)
    if coordinador == 1:
        nodo.bully._proclamarse()
    elif coordinador:
        nodo.bully._coordinador = coordinador
    return nodo, lanzador


def _ejecutar(nodo):
    return nodo.ejecutar(IMAGEN, "suma", {"a": 3, "b": 4}, {})


# ---------------------------------------------------------------- caso feliz
def test_nodo_solo_ejecuta_local():
    nodo, lanzador = _nodo(coordinador=1)
    assert _ejecutar(nodo) == (7, 1)
    assert lanzador.llamadas == [("docker.io/cerberusdistribuido/tarea:1.0", "suma")]
    assert nodo.tareas_en_curso == 0


def test_cuenta_las_tareas_en_curso_mientras_corren():
    nodo, lanzador = _nodo(coordinador=1)
    vistas = []
    lanzador.al_ejecutar = lambda: vistas.append(nodo.tareas_en_curso)
    _ejecutar(nodo)
    assert vistas == [1] and nodo.tareas_en_curso == 0


def test_pide_asignacion_al_coordinador_y_reenvia_al_elegido():
    http = Http({"http://nodo3/cluster/asignar": sobre(200, {"nodo": 2}),
                 "http://nodo2/cluster/ejecutar": sobre(200, {"resultado": 7, "nodo": 2})})
    nodo, lanzador = _nodo(http, coordinador=3)
    assert _ejecutar(nodo) == (7, 2)
    assert http.a("http://nodo3/cluster/asignar") == [{"de": 1, "excluir": []}]
    assert http.a("http://nodo2/cluster/ejecutar") == [
        {"calculo": "suma", "parametros": {"a": 3, "b": 4}, "datos": {}, "imagen": IMAGEN.referencia}]
    assert lanzador.llamadas == []  # no corrió nada local


def test_si_el_coordinador_asigna_a_la_entrada_ejecuta_local():
    nodo, lanzador = _nodo(Http({"http://nodo3/cluster/asignar": sobre(200, {"nodo": 1})}), coordinador=3)
    assert _ejecutar(nodo) == (7, 1)
    assert len(lanzador.llamadas) == 1


# ---------------------------------------------------------------- redistribución
def test_si_se_cae_el_ejecutor_se_reasigna_sin_el():
    http = Http({"http://nodo3/cluster/asignar": [sobre(200, {"nodo": 2}), sobre(200, {"nodo": 3})],
                 "http://nodo2/cluster/ejecutar": SinConexion("connection reset"),
                 "http://nodo3/cluster/ejecutar": sobre(200, {"resultado": 7, "nodo": 3})})
    nodo, _ = _nodo(http, coordinador=3)
    assert _ejecutar(nodo) == (7, 3)
    assert http.a("http://nodo3/cluster/asignar") == [{"de": 1, "excluir": []}, {"de": 1, "excluir": [2]}]


def test_si_se_cae_el_coordinador_hay_eleccion_y_sigue_con_el_nuevo():
    # Se cayeron el 3 (coordinador) y el 2: nadie contesta. El 1 gana y la ejecuta él.
    http = Http()
    nodo, lanzador = _nodo(http, coordinador=3)
    assert _ejecutar(nodo) == (7, 1)
    assert nodo.bully.coordinador == 1
    assert {u for u, _ in http.pedidos} >= {"http://nodo3/cluster/asignar", "http://nodo2/cluster/eleccion",
                                            "http://nodo3/cluster/eleccion"}


def test_si_el_nuevo_coordinador_es_otro_le_pide_a_el():
    # Cae el 3. En la elección el 2 contesta OK y se anuncia; la asignación va al 2.
    nodo = None

    def anunciar_2():
        nodo.bully.recibir_coordinador(2)
        return sobre(200, {"ok": 2})

    class HttpConAnuncio(Http):
        def __call__(self, metodo, url, cuerpo=None, timeout=5.0):
            if url == "http://nodo2/cluster/eleccion":
                self.pedidos.append((url, cuerpo))
                return anunciar_2()
            return super().__call__(metodo, url, cuerpo, timeout)

    http = HttpConAnuncio({"http://nodo2/cluster/asignar": sobre(200, {"nodo": 2}),
                           "http://nodo2/cluster/ejecutar": sobre(200, {"resultado": 7, "nodo": 2})})
    nodo, _ = _nodo(http, coordinador=3)
    assert _ejecutar(nodo) == (7, 2)
    assert nodo.bully.coordinador == 2


def test_sin_coordinador_a_tiempo_es_503():
    nodo, _ = _nodo()  # nunca hubo elección
    with pytest.raises(ErrorApi) as info:
        _ejecutar(nodo)
    assert info.value.tipo is TipoError.CLUSTER_NO_DISPONIBLE


def test_si_el_coordinador_no_tiene_nodos_vivos_es_503():
    nodo, _ = _nodo(Http({"http://nodo3/cluster/asignar": sobre(503, {"error": {}})}), coordinador=3)
    with pytest.raises(ErrorApi) as info:
        _ejecutar(nodo)
    assert info.value.tipo is TipoError.CLUSTER_NO_DISPONIBLE


# ---------------------------------------------------------------- errores del ejecutor
def test_el_error_del_ejecutor_llega_tal_cual():
    rechazo = sobre(422, {"error": {"tipo": "TAREA_RECHAZADA", "mensaje": "división por cero", "detalles": []}})
    http = Http({"http://nodo3/cluster/asignar": sobre(200, {"nodo": 2}), "http://nodo2/cluster/ejecutar": rechazo})
    nodo, _ = _nodo(http, coordinador=3)
    with pytest.raises(ErrorApi) as info:
        _ejecutar(nodo)
    assert (info.value.tipo, info.value.mensaje) == (TipoError.TAREA_RECHAZADA, "división por cero")


@pytest.mark.parametrize("respuesta, tipo", [
    (Vencido("timed out"), TipoError.TAREA_SIN_RESPUESTA),  # puede seguir corriendo: no se re-ejecuta
    ((500, None), TipoError.ERROR_INTERNO),
    (sobre(500, {"error": {"tipo": "INVENTADO"}}), TipoError.ERROR_INTERNO),
])
def test_respuestas_raras_del_ejecutor(respuesta, tipo):
    http = Http({"http://nodo3/cluster/asignar": sobre(200, {"nodo": 2}), "http://nodo2/cluster/ejecutar": respuesta})
    nodo, _ = _nodo(http, coordinador=3)
    with pytest.raises(ErrorApi) as info:
        _ejecutar(nodo)
    assert info.value.tipo is tipo
    assert len(http.a("http://nodo3/cluster/asignar")) == 1


# ---------------------------------------------------------------- coordinador
def test_heartbeats_solo_los_recibe_el_coordinador():
    nodo, _ = _nodo(coordinador=3)
    assert not nodo.recibir_heartbeat(2, 0)
    nodo, _ = _nodo(coordinador=1)
    assert nodo.recibir_heartbeat(2, 4)
    assert nodo.registro.estado()["2"]["tareas_en_curso"] == 4


def test_el_coordinador_se_anota_a_si_mismo_en_el_registro():
    nodo, _ = _nodo(coordinador=1)
    nodo.recibir_heartbeat(2, 0)
    nodo.latir()
    assert set(nodo.registro.estado()) == {"1", "2"}


def test_al_asumir_el_registro_arranca_de_cero():
    nodo, _ = _nodo()
    nodo.registro.actualizar(3, 9)  # de una época anterior
    nodo.bully._proclamarse()
    assert set(nodo.registro.estado()) == {"1"}


def test_al_asumir_anota_a_los_que_aceptaron_el_anuncio():
    # El 2 acepta el COORDINADOR; el 3 está caído. El 2 entra al registro sin esperar heartbeat.
    nodo, _ = _nodo(Http({"http://nodo2/cluster/coordinador": sobre(200, {"coordinador": 1})}))
    nodo.bully._proclamarse()
    assert set(nodo.registro.estado()) == {"1", "2"}


def test_iniciar_limpia_huerfanos_y_convoca_eleccion():
    nodo, lanzador = _nodo(intervalo_heartbeat=0.01)
    nodo.iniciar()
    try:
        assert nodo.bully.esperar_coordinador(timeout=5) == 1  # el 2 y el 3 no contestan
        assert lanzador.huerfanos_limpiados
    finally:
        nodo.detener()
