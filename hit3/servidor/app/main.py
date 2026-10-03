"""API HTTP de un nodo del cluster de tareas remotas (Hit 3).

Es el servidor del Hit 1 más los endpoints `/cluster/*`, que usan los nodos
entre sí (elección Bully, heartbeats, asignación y ejecución de tareas). nginx
publica sólo `/getRemoteTask` y `/health`.

Se levanta con `uvicorn --factory app.main:crear_app`. La fábrica recibe la
configuración, el lanzador y el nodo por parámetro: los tests le pasan dobles y
no necesitan Docker.
"""

import json
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import imagenes, registro
from app.config import Config
from app.errores import ErrorApi, TipoError, responder, responder_error
from app.lanzador import Lanzador
from app.nodo import Nodo
from app.validacion import validar_solicitud


class _Mensaje(BaseModel):
    """Mensaje entre nodos: ELECCION, COORDINADOR."""
    model_config = ConfigDict(extra="forbid", strict=True)
    de: int = Field(ge=1)


class _Heartbeat(_Mensaje):
    tareas_en_curso: int = Field(ge=0)


class _PedidoAsignacion(_Mensaje):
    excluir: list[int] = Field(default_factory=list, max_length=64)


async def _leer_mensaje(request, modelo):
    try:
        return modelo.model_validate(json.loads(await request.body()))
    except (ValueError, ValidationError):  # JSONDecodeError y ValidationError son ValueError
        raise ErrorApi(TipoError.PAYLOAD_INVALIDO, "mensaje del cluster inválido") from None


def crear_app(config=None, lanzador=None, nodo=None):
    config = config or Config.desde_entorno()
    log, _ = registro.configurar(config.dir_logs)
    lanzador = lanzador or Lanzador(config, log)
    nodo = nodo or Nodo(config, log, lanzador)
    permitidas = imagenes.lista_blanca(config.imagenes_permitidas)
    if not permitidas:
        log.warning("TP2_IMAGENES_PERMITIDAS está vacía: se va a rechazar toda tarea")

    @asynccontextmanager
    async def ciclo_de_vida(app):
        log.info("cluster | arranca el nodo %d | pares: %s", config.nodo_id,
                 [n for n, _ in config.pares] or "ninguno")
        nodo.iniciar()
        yield
        nodo.detener()

    # redirect_slashes=False: `/health/` es un 404 en JSON, no un 307 a otra URL.
    app = FastAPI(title="TP2 · Hit 3 · Nodo del cluster de tareas remotas", redirect_slashes=False,
                  lifespan=ciclo_de_vida)

    @app.middleware("http")
    async def bitacora(request, call_next):
        """Una línea de log por pedido. Un error inesperado sale como 500 en el
        sobre, sin detalles internos: el detalle queda en el log."""
        inicio = time.perf_counter()
        try:
            respuesta = await call_next(request)
        except Exception:
            log.exception("%s %s | error inesperado", request.method, request.url.path)
            respuesta = responder_error(ErrorApi(TipoError.ERROR_INTERNO, "error interno del servidor"))
        log.info("%s %s | %d | %.0f ms", request.method, request.url.path, respuesta.status_code,
                 (time.perf_counter() - inicio) * 1000)
        return respuesta

    @app.exception_handler(ErrorApi)
    async def _error_api(request, error):
        if error.interno:
            log.warning("%s %s | %s | %s", request.method, request.url.path, error.tipo.value,
                        error.interno)
        return responder_error(error)

    @app.exception_handler(StarletteHTTPException)
    async def _error_http(request, error):
        # El router sólo lanza estos dos: ruta que no existe y método no soportado.
        if error.status_code == 405:
            return responder_error(ErrorApi(
                TipoError.METODO_NO_PERMITIDO,
                f"{request.method} no está permitido en {request.url.path}", cabeceras=error.headers))
        return responder_error(ErrorApi(TipoError.RUTA_INEXISTENTE, f"no existe la ruta {request.url.path}"))

    async def _solicitud(request):
        """El cuerpo de una tarea, validado y con la imagen en la lista blanca."""
        cuerpo = await request.body()
        if len(cuerpo) > config.max_cuerpo:
            raise ErrorApi(TipoError.CUERPO_DEMASIADO_GRANDE,
                           f"el cuerpo supera los {config.max_cuerpo} bytes")
        solicitud = validar_solicitud(cuerpo, request.headers.get("content-type"))
        imagen = imagenes.parsear(solicitud.imagen)  # ya validada: no falla
        if imagen.nombre not in permitidas:
            raise ErrorApi(TipoError.IMAGEN_NO_PERMITIDA,
                           f"la imagen {imagen.nombre} no está en la lista de imágenes permitidas")
        return solicitud, imagen

    @app.post("/getRemoteTask")
    async def getRemoteTask(request: Request):  # noqa: N802 (nombre del enunciado)
        solicitud, imagen = await _solicitud(request)
        log.info("tarea | %s | calculo=%s", imagen.referencia, solicitud.calculo)
        # En un hilo: el servidor sigue atendiendo pedidos mientras corre la tarea.
        resultado, ejecutor = await run_in_threadpool(nodo.ejecutar, imagen, solicitud.calculo,
                                                      solicitud.parametros, solicitud.datos)
        return responder(200, {"calculo": solicitud.calculo, "resultado": resultado, "nodo": ejecutor})

    @app.get("/health")
    def health():
        docker_ok = lanzador.disponible()
        coordinador = nodo.bully.coordinador
        cluster = {"nodo": nodo.mi_id, "coordinador": coordinador,
                   "rol": "coordinador" if coordinador == nodo.mi_id else "worker",
                   "tareas_en_curso": nodo.tareas_en_curso}
        if coordinador == nodo.mi_id:
            cluster["nodos"] = nodo.registro.estado()
        return responder(200 if docker_ok else 503,
                         {"servidor": "ok", "docker": "ok" if docker_ok else "caido", "cluster": cluster})

    # ------------------------------------------------- entre nodos (nginx no las publica)
    @app.post("/cluster/eleccion")
    async def eleccion(request: Request):
        mensaje = await _leer_mensaje(request, _Mensaje)
        nodo.bully.recibir_eleccion(mensaje.de)
        return responder(200, {"ok": nodo.mi_id})

    @app.post("/cluster/coordinador")
    async def coordinador(request: Request):
        mensaje = await _leer_mensaje(request, _Mensaje)
        if not nodo.bully.recibir_coordinador(mensaje.de):
            raise ErrorApi(TipoError.COORDINADOR_RECHAZADO,
                           f"el nodo {nodo.mi_id} es mayor que {mensaje.de}: convoca una elección")
        return responder(200, {"coordinador": mensaje.de})

    def _no_es_coordinador():
        return ErrorApi(TipoError.NO_ES_COORDINADOR, f"el nodo {nodo.mi_id} no es el coordinador",
                        detalles=[{"coordinador": nodo.bully.coordinador}])

    @app.post("/cluster/heartbeat")
    async def heartbeat(request: Request):
        mensaje = await _leer_mensaje(request, _Heartbeat)
        if not nodo.recibir_heartbeat(mensaje.de, mensaje.tareas_en_curso):
            raise _no_es_coordinador()
        return responder(200, {"coordinador": nodo.mi_id})

    @app.post("/cluster/asignar")
    async def asignar(request: Request):
        mensaje = await _leer_mensaje(request, _PedidoAsignacion)
        if not nodo.bully.soy_coordinador():
            raise _no_es_coordinador()
        elegido = nodo.asignar(mensaje.excluir)
        if elegido is None:
            raise ErrorApi(TipoError.CLUSTER_NO_DISPONIBLE, "no hay nodos vivos para la tarea")
        return responder(200, {"nodo": elegido})

    @app.post("/cluster/ejecutar")
    async def ejecutar(request: Request):
        # Mismo cuerpo y mismas validaciones que /getRemoteTask: la lista blanca se
        # vuelve a mirar acá, por si el pedido no viene de un nodo.
        solicitud, imagen = await _solicitud(request)
        log.info("tarea | ejecuta en el nodo %d | %s | calculo=%s", nodo.mi_id, imagen.referencia,
                 solicitud.calculo)
        resultado = await run_in_threadpool(nodo.ejecutar_local, imagen, solicitud.calculo,
                                            solicitud.parametros, solicitud.datos)
        return responder(200, {"resultado": resultado, "nodo": nodo.mi_id})

    return app
