"""API HTTP del servidor de tareas remotas (Hit 1).

Se levanta con `uvicorn --factory app.main:crear_app`. La fábrica recibe la
configuración y el lanzador por parámetro: los tests le pasan dobles y no
necesitan Docker.
"""

import time

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import imagenes, registro
from app.config import Config
from app.errores import ErrorApi, TipoError, responder, responder_error
from app.lanzador import Lanzador
from app.validacion import validar_solicitud


def crear_app(config=None, lanzador=None):
    config = config or Config.desde_entorno()
    log, _ = registro.configurar(config.dir_logs)
    lanzador = lanzador or Lanzador(config, log)
    permitidas = imagenes.lista_blanca(config.imagenes_permitidas)
    if not permitidas:
        log.warning("TP2_IMAGENES_PERMITIDAS está vacía: se va a rechazar toda tarea")

    # redirect_slashes=False: `/health/` es un 404 en JSON, no un 307 a otra URL.
    app = FastAPI(title="TP2 · Hit 1 · Servidor de tareas remotas", redirect_slashes=False)

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

    @app.post("/getRemoteTask")
    async def getRemoteTask(request: Request):  # noqa: N802 (nombre del enunciado)
        cuerpo = await request.body()
        if len(cuerpo) > config.max_cuerpo:
            raise ErrorApi(TipoError.CUERPO_DEMASIADO_GRANDE,
                           f"el cuerpo supera los {config.max_cuerpo} bytes")
        solicitud = validar_solicitud(cuerpo, request.headers.get("content-type"))
        imagen = imagenes.parsear(solicitud.imagen)  # ya validada: no falla
        if imagen.nombre not in permitidas:
            raise ErrorApi(TipoError.IMAGEN_NO_PERMITIDA,
                           f"la imagen {imagen.nombre} no está en la lista de imágenes permitidas")

        log.info("tarea | %s | calculo=%s", imagen.referencia, solicitud.calculo)
        # En un hilo: el servidor sigue atendiendo pedidos mientras corre la tarea.
        resultado = await run_in_threadpool(lanzador.ejecutarTareaRemota, imagen, solicitud.calculo,
                                            solicitud.parametros, solicitud.datos)
        return responder(200, {"calculo": solicitud.calculo, "resultado": resultado})

    @app.get("/health")
    def health():
        docker_ok = lanzador.disponible()
        return responder(200 if docker_ok else 503,
                         {"servidor": "ok", "docker": "ok" if docker_ok else "caido"})

    return app
