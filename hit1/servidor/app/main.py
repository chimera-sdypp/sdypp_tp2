"""API HTTP del servidor de tareas remotas (Hit 1).

Se levanta con `uvicorn --factory app.main:crear_app`. La fábrica recibe la
configuración y el lanzador por parámetro: los tests le pasan dobles y no
necesitan Docker.
"""

import math
import threading
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import auth, imagenes, registro
from app.config import Config
from app.errores import ErrorApi, TipoError, responder, responder_error
from app.estadisticas import Estadisticas
from app.lanzador import Lanzador
from app.limitador import LimitadorPorUsuario
from app.validacion import validar_solicitud


class _EnCurso:
    """Las tareas que están ejecutándose. El Hit 1 no tiene pool ni cola: cada pedido corre
    en su propio hilo, así que `/stats` informa estas como workers ocupados y cola 0."""
    workers_max = None
    tareas_encoladas = 0

    def __init__(self):
        self._candado = threading.Lock()
        self._n = 0

    @property
    def workers_activos(self):
        with self._candado:
            return self._n

    def __enter__(self):
        with self._candado:
            self._n += 1

    def __exit__(self, *_):
        with self._candado:
            self._n -= 1


def crear_app(config=None, lanzador=None):
    config = config or Config.desde_entorno()
    auth.validar_secreto(config.jwt_secreto)
    log, _ = registro.configurar(config.dir_logs)
    lanzador = lanzador or Lanzador(config, log)
    estadisticas = Estadisticas()
    en_curso = _EnCurso()
    limitador = LimitadorPorUsuario(config.limite_pedidos_min)
    permitidas = imagenes.lista_blanca(config.imagenes_permitidas)
    if not permitidas:
        log.warning("TP2_IMAGENES_PERMITIDAS está vacía: se va a rechazar toda tarea")

    # redirect_slashes=False: `/health/` es un 404 en JSON, no un 307 a otra URL.
    app = FastAPI(title="TP2 · Hit 1 · Servidor de tareas remotas", redirect_slashes=False)

    # Qué rol hace falta en cada ruta. Una ruta que no está acá pide un token válido de
    # cualquier rol; sólo `/health` es pública (lo usan el healthcheck y el despliegue).
    rutas_publicas = {"/health"}
    roles_por_ruta = {"/stats": {auth.ROL_ADMIN}}

    @app.middleware("http")
    async def autenticacion(request, call_next):
        """Cada pedido trae su identidad: se valida el token, el rol de la ruta y el límite
        por usuario, todo antes de leer el cuerpo y de ejecutar nada."""
        ruta = request.url.path
        if ruta in rutas_publicas:
            return await call_next(request)
        try:
            identidad = auth.autenticar(request.headers.get("authorization"), config.jwt_secreto)
            permitidos = roles_por_ruta.get(ruta)
            if permitidos and identidad.rol not in permitidos:
                raise ErrorApi(TipoError.ROL_INSUFICIENTE,
                               f"{ruta} es sólo para {'/'.join(sorted(permitidos))} (tu rol: {identidad.rol})")
            if ruta == "/getRemoteTask" and request.method == "POST":
                espera = limitador.reservar(identidad.usuario)
                if espera:
                    reintentar = math.ceil(espera)
                    raise ErrorApi(TipoError.DEMASIADOS_PEDIDOS,
                                   f"más de {config.limite_pedidos_min} pedidos por minuto para {identidad.usuario}: "
                                   f"reintentá en {reintentar} s",
                                   cabeceras={"Retry-After": str(reintentar)})
        except ErrorApi as error:
            return responder_error(error)
        request.state.identidad = identidad
        return await call_next(request)

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
        if request.url.path == "/getRemoteTask":
            estadisticas.registrar_pedido(respuesta.status_code)
        identidad = getattr(request.state, "identidad", None)
        log.info("%s %s | %d | %.0f ms | usuario=%s", request.method, request.url.path, respuesta.status_code,
                 (time.perf_counter() - inicio) * 1000, identidad.usuario if identidad else "-")
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

    async def _validar_imagen(texto):
        """La imagen que pidió el cliente, antes de ejecutarla: bien formada (eso ya lo
        vio `validar_solicitud`), en la lista blanca, existente y, si la lista le fija
        digests a ese repositorio, con uno de ellos."""
        imagen = imagenes.parsear(texto)  # ya validada: no falla
        if imagen.nombre not in permitidas:
            raise ErrorApi(TipoError.IMAGEN_NO_PERMITIDA,
                           f"la imagen {imagen.nombre} no está en la lista de imágenes permitidas")
        digests = await run_in_threadpool(lanzador.preparar_imagen, imagen)
        fijados = permitidas[imagen.nombre]
        if fijados and not fijados & digests:
            raise ErrorApi(TipoError.IMAGEN_NO_PERMITIDA,
                           f"la imagen {imagen.referencia} no coincide con ningún digest permitido",
                           interno=f"digests de la imagen: {sorted(digests) or 'ninguno'}")
        return imagen

    @app.post("/getRemoteTask")
    async def getRemoteTask(request: Request):  # noqa: N802 (nombre del enunciado)
        inicio_total = time.perf_counter()
        task_id = str(uuid.uuid4())
        cuerpo = await request.body()
        if len(cuerpo) > config.max_cuerpo:
            raise ErrorApi(TipoError.CUERPO_DEMASIADO_GRANDE,
                           f"el cuerpo supera los {config.max_cuerpo} bytes")
        solicitud = validar_solicitud(cuerpo, request.headers.get("content-type"))
        imagen = await _validar_imagen(solicitud.imagen)

        log.info("tarea | %s | %s | calculo=%s", task_id, imagen.referencia, solicitud.calculo)
        # En un hilo: el servidor sigue atendiendo pedidos mientras corre la tarea.
        inicio_tarea = time.perf_counter()
        try:
            with en_curso:
                ejecucion = await run_in_threadpool(lanzador.ejecutarTareaRemota, imagen, solicitud.calculo,
                                                    solicitud.parametros, solicitud.datos)
        except Exception:
            estadisticas.registrar_tarea(imagen.referencia, False, time.perf_counter() - inicio_tarea)
            raise
        estadisticas.registrar_tarea(imagen.referencia, True, time.perf_counter() - inicio_tarea)
        return responder(200, {
            "calculo": solicitud.calculo,
            "resultado": ejecucion.resultado,
            "task_id": task_id,
            "imagen": imagen.referencia,
            "digest": ejecucion.digest,
            "imagen_id": ejecucion.imagen_id,
            "contenedor": ejecucion.contenedor,
            # Sin cola: cada pedido corre ya, en su propio hilo.
            "tiempos": {"en_cola_s": 0.0, "ejecucion_s": ejecucion.ejecucion_s,
                        "total_s": round(time.perf_counter() - inicio_total, 4)},
        })

    @app.get("/stats")
    def stats():
        """Estadísticas del servidor, sólo para el rol admin (lo exige el middleware)."""
        return responder(200, estadisticas.instantanea(en_curso))

    @app.get("/health")
    def health():
        docker_ok = lanzador.disponible()
        return responder(200 if docker_ok else 503,
                         {"servidor": "ok", "docker": "ok" if docker_ok else "caido"})

    return app
