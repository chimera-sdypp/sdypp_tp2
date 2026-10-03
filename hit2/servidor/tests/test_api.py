"""Tests de la API contra el contrato (hit1/contrato.md), con un lanzador falso.

Cada test afirma el **código** y el **sobre**, no sólo que "contestó".
"""

import logging

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.errores import ErrorApi, TipoError
from app.main import crear_app

VALIDO = {"calculo": "suma", "parametros": {"a": 3, "b": 4}, "datos": {}, "imagen": "cerberusdistribuido/tarea:1.0"}


class LanzadorFalso:
    def __init__(self, resultado=7, error=None, disponible=True):
        self.resultado = resultado
        self.error = error
        self._disponible = disponible
        self.llamadas = []

    def ejecutarTareaRemota(self, imagen, calculo, parametros, datos):  # noqa: N802
        self.llamadas.append((imagen.referencia, calculo, parametros, datos))
        if self.error:
            raise self.error
        return self.resultado

    def disponible(self):
        return self._disponible


@pytest.fixture
def armar(tmp_path):
    def _armar(lanzador=None, **cambios):
        opciones = {"dir_logs": str(tmp_path), "imagenes_permitidas": ("cerberusdistribuido/tarea",), **cambios}
        lanzador = lanzador or LanzadorFalso()
        return TestClient(crear_app(Config(**opciones), lanzador), raise_server_exceptions=False), lanzador
    return _armar


def _sobre(respuesta, codigo, tipo=None):
    """Toda respuesta: el código pedido, JSON y el mismo sobre salga bien o mal.
    Devuelve el `contenido`; si es un error, es exactamente {"error": {...}}."""
    assert respuesta.status_code == codigo, respuesta.text
    assert respuesta.headers["content-type"] == "application/json"
    cuerpo = respuesta.json()
    assert cuerpo.keys() == {"codigo", "contenido"}
    assert cuerpo["codigo"] == codigo
    contenido = cuerpo["contenido"]
    if tipo is None:
        assert "error" not in contenido
    else:
        assert contenido.keys() == {"error"}
        assert contenido["error"].keys() == {"tipo", "mensaje", "detalles"}
        assert contenido["error"]["tipo"] == tipo
    return contenido


def _post(cliente, cuerpo=None, **kwargs):
    if cuerpo is not None:
        kwargs["json"] = cuerpo
    return cliente.post("/getRemoteTask", **kwargs)


# ---------------------------------------------------------------- caso feliz
def test_ejecuta_la_tarea_y_devuelve_el_resultado(armar):
    cliente, lanzador = armar()
    res = _post(cliente, VALIDO)
    contenido = _sobre(res, 200)
    assert contenido["calculo"] == "suma"
    assert contenido["resultado"] == 7
    assert "lamport_ts" in contenido
    assert "X-Lamport-Clock" in res.headers
    # La imagen llega normalizada y el trabajo, tal cual.
    assert lanzador.llamadas == [("docker.io/cerberusdistribuido/tarea:1.0", "suma", {"a": 3, "b": 4}, {})]


def test_reloj_de_lamport_en_la_respuesta(armar):
    cliente, _ = armar()
    res = _post(cliente, VALIDO, headers={"X-Lamport-Clock": "41"})
    # Recepción: max(0, 41) + 1 = 42. Envío: 43, el mismo en el cuerpo y en la cabecera.
    assert _sobre(res, 200)["lamport_ts"] == 43
    assert res.headers["X-Lamport-Clock"] == "43"


def test_la_cola_recibe_el_timestamp_del_cliente(tmp_path):
    """La cola ordena por el timestamp del envío del cliente, no por el del servidor al recibir."""
    recibidos = []

    class PoolFalso:
        workers_max, workers_activos, tareas_encoladas = 1, 0, 0

        def ejecutar_tarea(self, imagen, calculo, parametros, datos, lamport_ts=0):
            recibidos.append(lamport_ts)
            return 7

    config = Config(dir_logs=str(tmp_path), imagenes_permitidas=("cerberusdistribuido/tarea",))
    cliente = TestClient(crear_app(config, LanzadorFalso(), pool=PoolFalso()))
    _post(cliente, VALIDO, headers={"X-Lamport-Clock": "100"})
    _post(cliente, VALIDO, headers={"X-Lamport-Clock": "5"})
    assert recibidos == [100, 5]


def test_datos_es_opcional(armar):
    cliente, lanzador = armar()
    _sobre(_post(cliente, {k: v for k, v in VALIDO.items() if k != "datos"}), 200)
    assert lanzador.llamadas[0][3] == {}


# ---------------------------------------------------------------- errores del cliente
def test_sin_content_type_es_415(armar):
    cliente, _ = armar()
    _sobre(_post(cliente, content=b'{"calculo": "suma"}'), 415, "TIPO_DE_CONTENIDO")


@pytest.mark.parametrize("contenido, tipo", [
    (b"", "CUERPO_VACIO"),
    (b"{roto", "JSON_INVALIDO"),
    (b'{"calculo": NaN}', "JSON_INVALIDO"),
    (b'{"imagen": "a:1", "imagen": "b:1"}', "JSON_INVALIDO"),
])
def test_cuerpos_mal_formados_son_400(armar, contenido, tipo):
    cliente, lanzador = armar()
    _sobre(_post(cliente, content=contenido, headers={"Content-Type": "application/json"}), 400, tipo)
    assert lanzador.llamadas == []


def test_cuerpo_demasiado_grande_es_413(armar):
    cliente, _ = armar(max_cuerpo=100)
    _sobre(_post(cliente, {**VALIDO, "datos": {"x": "a" * 200}}), 413, "CUERPO_DEMASIADO_GRANDE")


@pytest.mark.parametrize("cambios, campo, problema", [
    ({"calculo": None}, "calculo", "no puede ser null"),
    ({"parametros": "a=1"}, "parametros", "tiene que ser un objeto"),
    ({"imagen": "cerberusdistribuido/tarea"}, "imagen", "latest"),
    ({"password": "x"}, "password", "campo no permitido"),
])
def test_payload_invalido_es_422_con_detalle(armar, cambios, campo, problema):
    cliente, lanzador = armar()
    contenido = _sobre(_post(cliente, {**VALIDO, **cambios}), 422, "PAYLOAD_INVALIDO")
    detalles = {d["campo"]: d["problema"] for d in contenido["error"]["detalles"]}
    assert problema in detalles[campo]
    assert lanzador.llamadas == []


def test_imagen_fuera_de_la_lista_blanca_es_403(armar):
    cliente, lanzador = armar()
    contenido = _sobre(_post(cliente, {**VALIDO, "imagen": "alguien/minero:1.0"}), 403, "IMAGEN_NO_PERMITIDA")
    assert "docker.io/alguien/minero" in contenido["error"]["mensaje"]
    assert lanzador.llamadas == []


def test_sin_lista_blanca_se_rechaza_todo(armar):
    cliente, _ = armar(imagenes_permitidas=())
    _sobre(_post(cliente, VALIDO), 403, "IMAGEN_NO_PERMITIDA")


# ---------------------------------------------------------------- errores de la tarea
@pytest.mark.parametrize("error, codigo", [
    (ErrorApi(TipoError.IMAGEN_INEXISTENTE, "no existe", interno="pull access denied"), 422),
    (ErrorApi(TipoError.TAREA_RECHAZADA, "división por cero"), 422),
    (ErrorApi(TipoError.TAREA_FALLIDA, "falló", interno="172.30.0.5 exit=1"), 502),
    (ErrorApi(TipoError.REGISTRY_NO_DISPONIBLE, "sin registry", interno="dial tcp"), 502),
    (ErrorApi(TipoError.SERVICIO_NO_DISPONIBLE, "sin docker", interno="/var/run/docker.sock"), 503),
    (ErrorApi(TipoError.TAREA_SIN_RESPUESTA, "no respondió"), 504),
])
def test_errores_de_la_tarea_llegan_con_su_codigo(armar, error, codigo):
    cliente, _ = armar(LanzadorFalso(error=error))
    respuesta = _post(cliente, VALIDO)
    _sobre(respuesta, codigo, error.tipo.value)
    # Lo interno (IPs, rutas, errores de Docker) no sale del servidor.
    if error.interno:
        assert error.interno not in respuesta.text


def test_error_inesperado_es_500_generico(armar):
    cliente, _ = armar(LanzadorFalso(error=RuntimeError("secreto: /home/app/x.py línea 3")))
    respuesta = _post(cliente, VALIDO)
    _sobre(respuesta, 500, "ERROR_INTERNO")
    assert "secreto" not in respuesta.text


# ---------------------------------------------------------------- health
def test_health_ok(armar):
    cliente, _ = armar()
    res = cliente.get("/health")
    contenido = _sobre(res, 200)
    assert contenido["servidor"] == "ok"
    assert contenido["docker"] == "ok"
    assert "pool" in contenido
    assert contenido["pool"]["workers_max"] == 4


def test_health_sin_docker_es_503(armar):
    cliente, _ = armar(LanzadorFalso(disponible=False))
    res = cliente.get("/health")
    contenido = _sobre(res, 503)
    assert contenido["servidor"] == "ok"
    assert contenido["docker"] == "caido"
    assert "pool" in contenido



# ---------------------------------------------------------------- rutas y métodos
@pytest.mark.parametrize("metodo, ruta", [("GET", "/"), ("GET", "/noexiste"), ("GET", "/health/")])
def test_ruta_inexistente_es_404_en_json(armar, metodo, ruta):
    cliente, _ = armar()
    _sobre(cliente.request(metodo, ruta), 404, "RUTA_INEXISTENTE")


@pytest.mark.parametrize("metodo, ruta, permitido", [
    ("GET", "/getRemoteTask", "POST"),
    ("PUT", "/getRemoteTask", "POST"),
    ("POST", "/health", "GET"),
])
def test_metodo_no_permitido_es_405_con_allow_en_json(armar, metodo, ruta, permitido):
    cliente, _ = armar()
    respuesta = cliente.request(metodo, ruta)
    _sobre(respuesta, 405, "METODO_NO_PERMITIDO")
    assert respuesta.headers["allow"] == permitido


# ---------------------------------------------------------------- logs
def test_logs_en_disco_y_en_memoria(armar, tmp_path):
    cliente, _ = armar()
    _post(cliente, VALIDO)
    memoria = next(h for h in logging.getLogger("tp2.servidor").handlers if hasattr(h, "registros"))
    assert any("POST /getRemoteTask | 200" in linea for linea in memoria.registros)
    assert "POST /getRemoteTask | 200" in (tmp_path / "servidor.log").read_text(encoding="utf-8")
