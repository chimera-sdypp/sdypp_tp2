"""Tests de la API contra el contrato (hit3/contrato.md), con un lanzador falso.

Un nodo solo (sin pares): al arrancar gana su propia elección y ejecuta todo él.

Cada test afirma el **código** y el **sobre**, no sólo que "contestó".
"""

import logging
import time
import uuid
from contextlib import ExitStack

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.errores import ErrorApi, TipoError
from app.main import crear_app
from dobles import ejecucion

SECRETO = "secreto-de-prueba-de-al-menos-32-caracteres"
VALIDO = {"calculo": "suma", "parametros": {"a": 3, "b": 4}, "datos": {}, "imagen": "cerberusdistribuido/tarea:1.0"}


class LanzadorFalso:
    def __init__(self, resultado=7, error=None, disponible=True, digests=(), error_imagen=None):
        self.resultado = resultado
        self.error = error
        self._disponible = disponible
        self.digests = set(digests)
        self.error_imagen = error_imagen
        self.llamadas = []

    def preparar_imagen(self, imagen):
        if self.error_imagen:
            raise self.error_imagen
        return self.digests

    def ejecutarTareaRemota(self, imagen, calculo, parametros, datos):  # noqa: N802
        self.llamadas.append((imagen.referencia, calculo, parametros, datos))
        if self.error:
            raise self.error
        return ejecucion(self.resultado)

    def disponible(self):
        return self._disponible

    def limpiar_huerfanos(self):
        return 0


def token(usuario="ana", rol="usuario", vence_en=300, secreto=SECRETO, **claims):
    """Un JWT firmado como lo haría quien emite los tokens."""
    ahora = int(time.time())
    return jwt.encode({"sub": usuario, "role": rol, "iat": ahora, "exp": ahora + vence_en, **claims},
                      secreto, algorithm="HS256")


def con(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def armar(tmp_path):
    """El cliente de prueba entra como usuario `ana` salvo que se pida `sin_token=True` o se
    mande otra cabecera `Authorization`. El límite por usuario está apagado salvo que se pida."""
    with ExitStack() as pila:
        def _armar(lanzador=None, sin_token=False, **cambios):
            opciones = {"dir_logs": str(tmp_path), "imagenes_permitidas": ("cerberusdistribuido/tarea",),
                        "jwt_secreto": SECRETO, "limite_pedidos_min": 0, **cambios}
            lanzador = lanzador or LanzadorFalso()
            app = crear_app(Config(**opciones), lanzador)
            # Con `with` corre el ciclo de vida: la elección y el hilo de heartbeats.
            cliente = pila.enter_context(TestClient(app, raise_server_exceptions=False,
                                                    headers=None if sin_token else con(token())))
            return cliente, lanzador
        yield _armar


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
    contenido = _sobre(_post(cliente, VALIDO), 200)
    assert (contenido["calculo"], contenido["resultado"], contenido["nodo"]) == ("suma", 7, 1)
    # La imagen llega normalizada y el trabajo, tal cual.
    assert lanzador.llamadas == [("docker.io/cerberusdistribuido/tarea:1.0", "suma", {"a": 3, "b": 4}, {})]


def test_datos_es_opcional(armar):
    cliente, lanzador = armar()
    _sobre(_post(cliente, {k: v for k, v in VALIDO.items() if k != "datos"}), 200)
    assert lanzador.llamadas[0][3] == {}


def test_la_respuesta_trae_la_metadata_de_la_ejecucion(armar):
    cliente, _ = armar()
    contenido = _sobre(_post(cliente, VALIDO), 200)
    assert contenido.keys() == {"calculo", "resultado", "task_id", "imagen", "digest", "imagen_id", "contenedor",
                                "tiempos", "nodo"}
    assert uuid.UUID(contenido["task_id"]).version == 4
    assert contenido["imagen"] == "docker.io/cerberusdistribuido/tarea:1.0"
    assert contenido["digest"] == "sha256:" + "b" * 64
    assert contenido["imagen_id"] == "sha256:" + "a" * 64
    assert contenido["contenedor"] == "c0ffee123456"
    tiempos = contenido["tiempos"]
    assert tiempos.keys() == {"en_cola_s", "ejecucion_s", "total_s"}
    assert tiempos["ejecucion_s"] == 0.01
    assert contenido["nodo"] == 1
    assert 0 <= tiempos["en_cola_s"] <= tiempos["total_s"]    # la ejecución es la del doble: no se compara


def test_cada_tarea_tiene_su_task_id(armar):
    cliente, _ = armar()
    ids = {_sobre(_post(cliente, VALIDO), 200)["task_id"] for _ in range(3)}
    assert len(ids) == 3


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


def test_imagen_inexistente_es_422_y_no_se_ejecuta(armar):
    error = ErrorApi(TipoError.IMAGEN_INEXISTENTE, "la imagen no existe o no es accesible",
                     interno="manifest unknown")
    cliente, lanzador = armar(LanzadorFalso(error_imagen=error))
    respuesta = _post(cliente, {**VALIDO, "imagen": "cerberusdistribuido/tarea:9.9.9"})
    _sobre(respuesta, 422, "IMAGEN_INEXISTENTE")
    assert "manifest unknown" not in respuesta.text
    assert lanzador.llamadas == []


DIGEST = "sha256:" + "a" * 64


def test_imagen_con_el_digest_fijado_se_ejecuta(armar):
    cliente, lanzador = armar(LanzadorFalso(digests={DIGEST}),
                              imagenes_permitidas=(f"cerberusdistribuido/tarea@{DIGEST}",))
    _sobre(_post(cliente, VALIDO), 200)
    assert len(lanzador.llamadas) == 1


@pytest.mark.parametrize("digests", [{"sha256:" + "f" * 64}, set()])
def test_imagen_con_otro_digest_que_el_fijado_es_403(armar, digests):
    cliente, lanzador = armar(LanzadorFalso(digests=digests),
                              imagenes_permitidas=(f"cerberusdistribuido/tarea@{DIGEST}",))
    contenido = _sobre(_post(cliente, VALIDO), 403, "IMAGEN_NO_PERMITIDA")
    assert "digest" in contenido["error"]["mensaje"]
    assert lanzador.llamadas == []


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
    contenido = _sobre(cliente.get("/health"), 200)
    assert (contenido["servidor"], contenido["docker"]) == ("ok", "ok")
    cluster = contenido["cluster"]
    assert (cluster["nodo"], cluster["coordinador"], cluster["rol"], cluster["tareas_en_curso"]) == \
        (1, 1, "coordinador", 0)
    assert cluster["nodos"]["1"]["estado"] == "vivo"


def test_health_sin_docker_es_503(armar):
    cliente, _ = armar(LanzadorFalso(disponible=False))
    contenido = _sobre(cliente.get("/health"), 503)
    assert (contenido["servidor"], contenido["docker"]) == ("ok", "caido")


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


# ---------------------------------------------------------------- entre nodos
def test_eleccion_contesta_ok(armar):
    cliente, _ = armar()
    assert _sobre(cliente.post("/cluster/eleccion", json={"de": 1}), 200) == {"ok": 1}


def test_coordinador_menor_es_rechazado(armar):
    cliente, _ = armar(nodo_id=3)
    _sobre(cliente.post("/cluster/coordinador", json={"de": 2}), 409, "COORDINADOR_RECHAZADO")


def test_coordinador_mayor_es_aceptado(armar):
    cliente, _ = armar()
    assert _sobre(cliente.post("/cluster/coordinador", json={"de": 5}), 200) == {"coordinador": 5}
    assert _sobre(cliente.get("/health"), 200)["cluster"]["rol"] == "worker"


def test_heartbeat_y_asignacion_en_el_coordinador(armar):
    cliente, _ = armar()
    assert _sobre(cliente.post("/cluster/heartbeat", json={"de": 2, "tareas_en_curso": 0}), 200) == \
        {"coordinador": 1}
    assert _sobre(cliente.post("/cluster/asignar", json={"de": 2, "excluir": [1]}), 200) == {"nodo": 2}
    _sobre(cliente.post("/cluster/asignar", json={"de": 2, "excluir": [1, 2]}), 503, "CLUSTER_NO_DISPONIBLE")


def test_heartbeat_y_asignacion_en_un_no_coordinador_son_409(armar):
    cliente, _ = armar()
    cliente.post("/cluster/coordinador", json={"de": 5})
    _sobre(cliente.post("/cluster/heartbeat", json={"de": 2, "tareas_en_curso": 0}), 409, "NO_ES_COORDINADOR")
    _sobre(cliente.post("/cluster/asignar", json={"de": 2}), 409, "NO_ES_COORDINADOR")


@pytest.mark.parametrize("ruta, cuerpo", [
    ("/cluster/eleccion", {"de": "1"}),
    ("/cluster/eleccion", {}),
    ("/cluster/heartbeat", {"de": 1, "tareas_en_curso": -1}),
    ("/cluster/coordinador", {"de": 1, "otro": 1}),
])
def test_mensajes_del_cluster_mal_formados_son_422(armar, ruta, cuerpo):
    cliente, _ = armar()
    _sobre(cliente.post(ruta, json=cuerpo), 422, "PAYLOAD_INVALIDO")


def test_ejecutar_corre_local_con_las_mismas_validaciones(armar):
    cliente, lanzador = armar()
    assert _sobre(cliente.post("/cluster/ejecutar", json=VALIDO), 200) == {
        "resultado": 7, "contenedor": "c0ffee123456", "imagen_id": "sha256:" + "a" * 64,
        "digest": "sha256:" + "b" * 64, "ejecucion_s": 0.01, "nodo": 1}
    _sobre(cliente.post("/cluster/ejecutar", json={**VALIDO, "imagen": "alguien/minero:1.0"}), 403,
           "IMAGEN_NO_PERMITIDA")
    assert len(lanzador.llamadas) == 1


# ---------------------------------------------------------------- autenticación por request (R4)
def test_sin_token_es_401_y_no_ejecuta_nada(armar):
    cliente, lanzador = armar(sin_token=True)
    respuesta = _post(cliente, VALIDO)
    _sobre(respuesta, 401, "NO_AUTORIZADO")
    assert respuesta.headers["www-authenticate"] == "Bearer"
    assert lanzador.llamadas == []


@pytest.mark.parametrize("cabecera", [
    "Bearer basura",
    "Basic YW5hOnNlY3JldG8=",
    "Bearer",
    "",
])
def test_token_mal_formado_es_401(armar, cabecera):
    cliente, lanzador = armar(sin_token=True)
    _sobre(_post(cliente, VALIDO, headers={"Authorization": cabecera}), 401, "NO_AUTORIZADO")
    assert lanzador.llamadas == []


def test_token_firmado_con_otro_secreto_es_401(armar):
    cliente, lanzador = armar(sin_token=True)
    falso = token(secreto="otro-secreto-que-tambien-tiene-mas-de-32-caracteres")
    _sobre(_post(cliente, VALIDO, headers=con(falso)), 401, "NO_AUTORIZADO")
    assert lanzador.llamadas == []


def test_token_vencido_es_401_y_lo_dice(armar):
    cliente, lanzador = armar(sin_token=True)
    contenido = _sobre(_post(cliente, VALIDO, headers=con(token(vence_en=-5))), 401, "NO_AUTORIZADO")
    assert "venció" in contenido["error"]["mensaje"]
    assert lanzador.llamadas == []


def test_token_sin_vencimiento_es_401(armar):
    cliente, _ = armar(sin_token=True)
    sin_exp = jwt.encode({"sub": "ana", "role": "usuario"}, SECRETO, algorithm="HS256")
    _sobre(_post(cliente, VALIDO, headers=con(sin_exp)), 401, "NO_AUTORIZADO")


def test_token_sin_firma_alg_none_es_401(armar):
    cliente, lanzador = armar(sin_token=True)
    sin_firma = jwt.encode({"sub": "ana", "role": "admin", "exp": int(time.time()) + 300}, None, algorithm="none")
    _sobre(_post(cliente, VALIDO, headers=con(sin_firma)), 401, "NO_AUTORIZADO")
    assert lanzador.llamadas == []


def test_token_con_rol_desconocido_es_403(armar):
    cliente, lanzador = armar(sin_token=True)
    _sobre(_post(cliente, VALIDO, headers=con(token(rol="root"))), 403, "ROL_INSUFICIENTE")
    assert lanzador.llamadas == []


def test_el_usuario_y_el_admin_ejecutan_tareas(armar):
    cliente, lanzador = armar(sin_token=True)
    _sobre(_post(cliente, VALIDO, headers=con(token("ana", "usuario"))), 200)
    _sobre(_post(cliente, VALIDO, headers=con(token("root", "admin"))), 200)
    assert len(lanzador.llamadas) == 2


def test_health_es_publico(armar):
    cliente, _ = armar(sin_token=True)
    _sobre(cliente.get("/health"), 200)


def test_stats_sin_token_es_401(armar):
    cliente, _ = armar(sin_token=True)
    _sobre(cliente.get("/stats"), 401, "NO_AUTORIZADO")


def test_stats_con_token_de_usuario_es_403(armar):
    cliente, _ = armar()
    contenido = _sobre(cliente.get("/stats"), 403, "ROL_INSUFICIENTE")
    assert "admin" in contenido["error"]["mensaje"]


def test_stats_con_token_de_admin_es_200(armar):
    cliente, _ = armar(sin_token=True)
    contenido = _sobre(cliente.get("/stats", headers=con(token("root", "admin"))), 200)
    assert contenido.keys() >= {"tareas_ejecutadas", "tareas_fallidas", "tareas_por_imagen", "latencia_s",
                                "tareas_en_cola", "workers_ocupados", "workers_max", "pedidos_por_codigo",
                                "nodos", "nodos_sin_respuesta", "atendido_por"}
    assert contenido["tareas_ejecutadas"] == 0
    assert contenido["latencia_s"] == {"promedio": None, "p95": None, "muestras": 0}
    assert contenido["nodos"] == {"1": {"tareas_ejecutadas": 0, "tareas_fallidas": 0, "workers_ocupados": 0}}
    assert (contenido["nodos_sin_respuesta"], contenido["atendido_por"]) == ([], 1)


def test_stats_cuenta_tareas_por_imagen_y_pedidos_por_codigo(armar):
    lanzador = LanzadorFalso()
    cliente, _ = armar(lanzador)
    for _ in range(3):
        _sobre(_post(cliente, VALIDO), 200)
    lanzador.error = ErrorApi(TipoError.TAREA_FALLIDA, "falló")
    _sobre(_post(cliente, VALIDO), 502, "TAREA_FALLIDA")
    _sobre(_post(cliente, {**VALIDO, "imagen": "otra/cosa:1"}), 403, "IMAGEN_NO_PERMITIDA")
    _sobre(_post(cliente, VALIDO, headers={"Authorization": "Bearer x"}), 401, "NO_AUTORIZADO")

    stats = _sobre(cliente.get("/stats", headers=con(token("root", "admin"))), 200)
    assert stats["tareas_ejecutadas"] == 3
    assert stats["tareas_fallidas"] == 1
    assert stats["tareas_por_imagen"] == {"docker.io/cerberusdistribuido/tarea:1.0": {"ejecutadas": 3, "fallidas": 1}}
    assert stats["pedidos_por_codigo"] == {"200": 3, "401": 1, "403": 1, "502": 1}
    assert stats["latencia_s"]["muestras"] == 4
    assert stats["nodos"]["1"] == {"tareas_ejecutadas": 3, "tareas_fallidas": 1, "workers_ocupados": 0}
    assert (stats["tareas_en_cola"], stats["workers_ocupados"]) == (0, 0)


def test_las_rutas_entre_nodos_no_piden_token_y_sus_stats_son_crudas(armar):
    """/cluster/* no lleva token: nginx no la publica y los nodos sólo se ven por la red interna."""
    cliente, _ = armar(sin_token=True)
    contenido = _sobre(cliente.get("/cluster/stats"), 200)
    assert contenido["estadisticas"].keys() == {"ejecutadas", "fallidas", "por_imagen", "latencias", "por_codigo"}
    assert contenido["tareas_en_curso"] == 0


def test_el_servidor_no_arranca_sin_secreto_o_con_uno_corto(tmp_path):
    for secreto in ("", "corto"):
        with pytest.raises(ValueError, match="TP2_JWT_SECRET"):
            crear_app(Config(dir_logs=str(tmp_path), jwt_secreto=secreto), LanzadorFalso())


# ---------------------------------------------------------------- límite por usuario (R4)
def test_pasado_el_limite_el_usuario_recibe_429_con_retry_after(armar):
    cliente, lanzador = armar(limite_pedidos_min=2)
    _sobre(_post(cliente, VALIDO), 200)
    _sobre(_post(cliente, VALIDO), 200)
    respuesta = _post(cliente, VALIDO)
    contenido = _sobre(respuesta, 429, "DEMASIADOS_PEDIDOS")
    assert int(respuesta.headers["retry-after"]) >= 1
    assert "ana" in contenido["error"]["mensaje"]
    assert len(lanzador.llamadas) == 2


def test_el_limite_es_por_usuario_y_los_rechazos_sin_token_no_lo_gastan(armar):
    cliente, _ = armar(sin_token=True, limite_pedidos_min=1)
    for _ in range(5):
        _sobre(_post(cliente, VALIDO), 401, "NO_AUTORIZADO")
    _sobre(_post(cliente, VALIDO, headers=con(token("ana"))), 200)
    _sobre(_post(cliente, VALIDO, headers=con(token("ana"))), 429, "DEMASIADOS_PEDIDOS")
    _sobre(_post(cliente, VALIDO, headers=con(token("beto"))), 200)


def test_el_limite_en_cero_lo_apaga(armar):
    cliente, lanzador = armar(limite_pedidos_min=0)
    for _ in range(20):
        _sobre(_post(cliente, VALIDO), 200)
    assert len(lanzador.llamadas) == 20
