import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from docker.errors import APIError, DockerException, NotFound

from app.config import Config
from app.errores import ErrorApi, TipoError
from app.imagenes import parsear
from app.lanzador import Lanzador, SinConexion, Vencido, http_json
from dobles import DockerFalso, HttpFalso, RelojFalso, logger_silencioso, sobre

IMAGEN = parsear("cerberusdistribuido/tarea:1.0")
CONFIG = Config(timeout_arranque=5, timeout_ejecucion=10)


def _lanzador(docker_falso=None, http=None, config=CONFIG, fabrica=None):
    reloj = RelojFalso()
    docker_falso = docker_falso or DockerFalso(presentes={IMAGEN.referencia})
    lanzador = Lanzador(config, logger_silencioso(), fabrica_docker=fabrica or (lambda: docker_falso),
                        http=http or HttpFalso(), reloj=reloj, dormir=reloj.dormir)
    return lanzador, docker_falso


def _ejecutar(lanzador, imagen=IMAGEN):
    return lanzador.ejecutarTareaRemota(imagen, "suma", {"a": 3, "b": 4}, {})


def _falla(lanzador, imagen=IMAGEN):
    with pytest.raises(ErrorApi) as info:
        _ejecutar(lanzador, imagen)
    return info.value


# ---------------------------------------------------------------- caso feliz
def test_devuelve_el_resultado_y_borra_el_contenedor():
    http = HttpFalso()
    lanzador, docker_falso = _lanzador(http=http)
    assert _ejecutar(lanzador) == 7
    [contenedor] = docker_falso.creados
    assert contenedor.imagen == "docker.io/cerberusdistribuido/tarea:1.0"
    assert contenedor.network == "tp2-tareas"
    assert contenedor.borrado
    assert docker_falso.pulls == []  # ya estaba en el host
    metodo, url, cuerpo, timeout = http.pedidos[-1]
    assert (metodo, url) == ("POST", "http://172.30.0.5:8080/ejecutarTarea")
    assert cuerpo == {"calculo": "suma", "parametros": {"a": 3, "b": 4}, "datos": {}}
    assert timeout == CONFIG.timeout_ejecucion


def test_espera_el_health_antes_de_mandar_el_trabajo():
    http = HttpFalso(salud=[SinConexion("refused"), (503, None), (200, None)])
    lanzador, _ = _lanzador(http=http)
    _ejecutar(lanzador)
    assert [p[0] for p in http.pedidos] == ["GET", "GET", "GET", "POST"]


# ---------------------------------------------------------------- descarga
def test_descarga_la_imagen_si_falta_con_credenciales_del_servidor():
    docker_falso = DockerFalso()
    lanzador, _ = _lanzador(docker_falso, config=Config(registry_usuario="cerberus",
                                                        registry_token="dckr_pat_x"))
    _ejecutar(lanzador)
    assert docker_falso.pulls == [{"nombre": "docker.io/cerberusdistribuido/tarea", "tag": "1.0",
                                   "auth": {"username": "cerberus", "password": "dckr_pat_x"}}]


def test_no_manda_el_token_a_otro_registry():
    docker_falso = DockerFalso()
    lanzador, _ = _lanzador(docker_falso, config=Config(registry_usuario="cerberus",
                                                        registry_token="dckr_pat_x"))
    _ejecutar(lanzador, parsear("ghcr.io/otro/tarea:1.0"))
    assert docker_falso.pulls[0]["auth"] is None


@pytest.mark.parametrize("error, tipo", [
    (NotFound("pull access denied"), TipoError.IMAGEN_INEXISTENTE),
    (APIError("unauthorized: incorrect username or password"), TipoError.REGISTRY_NO_DISPONIBLE),
])
def test_errores_de_descarga(error, tipo):
    docker_falso = DockerFalso(error_pull=error)
    lanzador, _ = _lanzador(docker_falso)
    assert _falla(lanzador).tipo is tipo
    assert docker_falso.creados == []


# ---------------------------------------------------------------- fallas de la tarea
@pytest.mark.parametrize("respuesta, tipo, mensaje", [
    (sobre(422, {"error": {"tipo": "X", "mensaje": "división por cero"}}), TipoError.TAREA_RECHAZADA,
     "división por cero"),
    ((400, None), TipoError.TAREA_RECHAZADA, "rechazó los parámetros"),
    (sobre(500, {"error": {"tipo": "X", "mensaje": "boom"}}), TipoError.TAREA_FALLIDA, "falló"),
    (sobre(200, {"otra": 1}), TipoError.TAREA_FALLIDA, "respuesta inválida"),
    ((200, {"resultado": 7}), TipoError.TAREA_FALLIDA, "respuesta inválida"),  # sin sobre
    (Vencido("timed out"), TipoError.TAREA_SIN_RESPUESTA, "no respondió en 10 s"),
    (SinConexion("reset"), TipoError.TAREA_FALLIDA, "se perdió la conexión"),
])
def test_respuestas_de_la_tarea(respuesta, tipo, mensaje):
    lanzador, docker_falso = _lanzador(http=HttpFalso(tarea=respuesta))
    error = _falla(lanzador)
    assert error.tipo is tipo
    assert mensaje in error.mensaje
    assert docker_falso.creados[0].borrado


def test_tarea_que_nunca_queda_lista_vence_y_se_borra():
    lanzador, docker_falso = _lanzador(http=HttpFalso(salud=SinConexion("refused")))
    error = _falla(lanzador)
    assert error.tipo is TipoError.TAREA_SIN_RESPUESTA
    assert "5 s" in error.mensaje
    assert docker_falso.creados[0].borrado


def test_si_falla_el_start_igual_se_borra():
    docker_falso = DockerFalso(presentes={IMAGEN.referencia}, falla_start=True)
    lanzador, _ = _lanzador(docker_falso)
    assert _falla(lanzador).tipo is TipoError.TAREA_FALLIDA
    assert docker_falso.creados[0].borrado


def test_error_inesperado_igual_borra_el_contenedor():
    lanzador, docker_falso = _lanzador(http=HttpFalso(tarea=RuntimeError("bug")))
    with pytest.raises(RuntimeError):
        _ejecutar(lanzador)
    assert docker_falso.creados[0].borrado


# ---------------------------------------------------------------- Docker caído
def test_sin_docker_es_servicio_no_disponible():
    def fabrica():
        raise DockerException("Error while fetching server API version")

    lanzador, _ = _lanzador(fabrica=fabrica)
    assert not lanzador.disponible()
    assert _falla(lanzador).tipo is TipoError.SERVICIO_NO_DISPONIBLE


# ---------------------------------------------------------------- http_json real
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/lento":
            threading.Event().wait(0.5)
        estado, cuerpo = {"/ok": (200, b'{"a": 1}'), "/roto": (200, b"no es json"),
                          "/rechazo": (422, b'{"b": 2}')}.get(self.path, (200, b"{}"))
        self.send_response(estado)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, *args):
        pass


def test_http_json_contra_un_servidor_real():
    servidor = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{servidor.server_address[1]}"
    try:
        assert http_json("GET", url + "/ok") == (200, {"a": 1})
        assert http_json("GET", url + "/roto") == (200, None)
        assert http_json("GET", url + "/rechazo") == (422, {"b": 2})
        with pytest.raises(Vencido):
            http_json("GET", url + "/lento", timeout=0.1)
    finally:
        servidor.shutdown()
    with pytest.raises(SinConexion):
        http_json("GET", "http://127.0.0.1:9/health", timeout=1)
