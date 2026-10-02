"""API HTTP del servidor de tareas remotas (Hit 2 — Concurrencia, Worker Pool y Lamport Clocks).

Se levanta con `uvicorn --factory app.main:crear_app`. La fábrica recibe la
configuración, el lanzador, el reloj de Lamport y el pool de workers.
"""

import time

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import imagenes, registro
from app.config import Config
from app.errores import ErrorApi, TipoError, responder, responder_error
from app.lamport import RelojLamport
from app.lanzador import Lanzador
from app.pool import PoolWorkers
from app.validacion import validar_solicitud


def crear_app(config=None, lanzador=None, reloj=None, pool=None):
    config = config or Config.desde_entorno()
    log, _ = registro.configurar(config.dir_logs)
    lanzador = lanzador or Lanzador(config, log)
    reloj = reloj or RelojLamport()
    pool = pool or PoolWorkers(lanzador, config.workers_max, log)
    permitidas = imagenes.lista_blanca(config.imagenes_permitidas)
    if not permitidas:
        log.warning("TP2_IMAGENES_PERMITIDAS está vacía: se va a rechazar toda tarea")

    app = FastAPI(title="TP2 · Hit 2 · Servidor con Pool de Workers y Relojes de Lamport", redirect_slashes=False)

    @app.middleware("http")
    async def bitacora(request: Request, call_next):
        """Procesa pedido, extrae reloj de Lamport del cliente y actualiza el reloj local."""
        inicio = time.perf_counter()

        # Extraer timestamp de Lamport del cliente (header X-Lamport-Clock)
        header_ts = request.headers.get("x-lamport-clock") or request.headers.get("X-Lamport-Clock")
        ts_cliente = 0
        if header_ts and header_ts.isdigit():
            ts_cliente = int(header_ts)

        # Recepción: L = max(L, L_cliente) + 1. La cola ordena por el timestamp del
        # cliente (el del envío), que es el que respeta el orden de Lamport entre clientes.
        reloj.actualizar(ts_cliente)
        request.state.ts_cliente = ts_cliente

        try:
            respuesta = await call_next(request)
        except Exception:
            log.exception("%s %s | error inesperado", request.method, request.url.path)
            respuesta = responder_error(ErrorApi(TipoError.ERROR_INTERNO, "error interno del servidor"))

        # Envío: si el handler no puso el reloj (lo pone junto con el cuerpo), se pone acá.
        if "x-lamport-clock" not in respuesta.headers:
            respuesta.headers["X-Lamport-Clock"] = str(reloj.incrementar())
        ts_salida = int(respuesta.headers["x-lamport-clock"])

        log.info("%s %s | %d | %.0f ms | lamport_ts=%d", request.method, request.url.path,
                 respuesta.status_code, (time.perf_counter() - inicio) * 1000, ts_salida)
        return respuesta

    @app.exception_handler(ErrorApi)
    async def _error_api(request: Request, error: ErrorApi):
        if error.interno:
            log.warning("%s %s | %s | %s", request.method, request.url.path, error.tipo.value, error.interno)
        ts_salida = reloj.incrementar()
        res = responder_error(error)
        res.headers["X-Lamport-Clock"] = str(ts_salida)
        return res

    @app.exception_handler(StarletteHTTPException)
    async def _error_http(request: Request, error: StarletteHTTPException):
        ts_salida = reloj.incrementar()
        if error.status_code == 405:
            res = responder_error(ErrorApi(
                TipoError.METODO_NO_PERMITIDO,
                f"{request.method} no está permitido en {request.url.path}", cabeceras=error.headers))
        else:
            res = responder_error(ErrorApi(TipoError.RUTA_INEXISTENTE, f"no existe la ruta {request.url.path}"))
        res.headers["X-Lamport-Clock"] = str(ts_salida)
        return res

    @app.post("/getRemoteTask")
    async def getRemoteTask(request: Request):  # noqa: N802 (nombre del enunciado)
        cuerpo = await request.body()
        if len(cuerpo) > config.max_cuerpo:
            raise ErrorApi(TipoError.CUERPO_DEMASIADO_GRANDE,
                           f"el cuerpo supera los {config.max_cuerpo} bytes")
        solicitud = validar_solicitud(cuerpo, request.headers.get("content-type"))
        imagen = imagenes.parsear(solicitud.imagen)
        if imagen.nombre not in permitidas:
            raise ErrorApi(TipoError.IMAGEN_NO_PERMITIDA,
                           f"la imagen {imagen.nombre} no está en la lista de imágenes permitidas")

        ts_solicitud = request.state.ts_cliente
        log.info("tarea | %s | calculo=%s | lamport_ts=%d", imagen.referencia, solicitud.calculo, ts_solicitud)

        # Encolar en el pool de workers (con exclusión mutua y ordenamiento Lamport)
        resultado = await run_in_threadpool(
            pool.ejecutar_tarea,
            imagen,
            solicitud.calculo,
            solicitud.parametros,
            solicitud.datos,
            ts_solicitud
        )

        ts_respuesta = reloj.incrementar()
        return responder(
            200,
            {
                "calculo": solicitud.calculo,
                "resultado": resultado,
                "lamport_ts": ts_respuesta
            },
            cabeceras={"X-Lamport-Clock": str(ts_respuesta)}
        )

    @app.get("/health")
    def health():
        docker_ok = lanzador.disponible()
        ts_respuesta = reloj.incrementar()
        return responder(
            200 if docker_ok else 503,
            {
                "servidor": "ok",
                "docker": "ok" if docker_ok else "caido",
                "pool": {
                    "workers_activos": pool.workers_activos,
                    "workers_max": pool.workers_max,
                    "tareas_encoladas": pool.tareas_encoladas
                },
                "lamport_ts": ts_respuesta
            },
            cabeceras={"X-Lamport-Clock": str(ts_respuesta)}
        )

    return app
