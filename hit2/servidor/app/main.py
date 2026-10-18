"""API HTTP del servidor de tareas remotas (Hit 2 — Concurrencia, Worker Pool y Lamport Clocks).

Se levanta con `uvicorn --factory app.main:crear_app`. La fábrica recibe la
configuración, el lanzador, el reloj de Lamport y el pool de workers.
"""

import math
import time
import uuid

import os

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import auth, imagenes, registro
from app.config import Config
from app.errores import ErrorApi, TipoError, responder, responder_error
from app.estadisticas import Estadisticas
from app.lamport import RelojLamport
from app.lanzador import Lanzador
from app.limitador import LimitadorPorUsuario
from app.pool import PoolWorkers
from app.validacion import validar_solicitud
from app.video_servicio import VideoServicio


def crear_app(config=None, lanzador=None, reloj=None, pool=None, video_pool=None, video_servicio=None):
    config = config or Config.desde_entorno()
    auth.validar_secreto(config.jwt_secreto)
    log, _ = registro.configurar(config.dir_logs)
    lanzador = lanzador or Lanzador(config, log)
    reloj = reloj or RelojLamport()
    pool = pool or PoolWorkers(lanzador, config.workers_max, log)
    video_pool = video_pool or PoolWorkers(lanzador, config.video_workers_max, log, cola_max=config.video_cola_max)
    video_servicio = video_servicio or VideoServicio(config, video_pool, lanzador, log)
    estadisticas = Estadisticas()
    limitador = LimitadorPorUsuario(config.limite_pedidos_min)
    permitidas = imagenes.lista_blanca(config.imagenes_permitidas)
    if not permitidas:
        log.warning("TP2_IMAGENES_PERMITIDAS está vacía: se va a rechazar toda tarea")

    app = FastAPI(title="TP2 · Hit 2 · Servidor con Pool de Workers y Relojes de Lamport", redirect_slashes=False)

    # Qué rol hace falta en cada ruta. Una ruta que no está acá pide un token válido de
    # cualquier rol; sólo `/health` es pública (lo usan el healthcheck y el despliegue).
    rutas_publicas = {"/health"}
    roles_por_ruta = {"/stats": {auth.ROL_ADMIN}}

    # --------------------------------------------------------------------------
    # 1. Middleware de Autenticación y Control de Acceso (Requisito R4)
    # --------------------------------------------------------------------------
    @app.middleware("http")
    async def autenticacion(request: Request, call_next):
        """Valida identidad, privilegios de rol (RBAC) y cuota de tasa (Token Bucket).
        
        Secuencia de Control Fail-Closed:
        1. Si la ruta es pública (`/health`), pasa sin autenticar.
        2. Extrae y valida el JWT (`Authorization: Bearer <token>`). Si falta, expiró
           o la firma no coincide, aborta de inmediato con HTTP 401 NO_AUTORIZADO.
        3. Valida el rol del usuario contra la ruta solicitada:
           - `/stats` exige estrictamente rol `admin`. Si un `usuario` intenta acceder,
             se rechaza con HTTP 403 ROL_INSUFICIENTE antes de consultar métricas.
        4. Si es un POST a `/getRemoteTask`, consulta el limitador de tasa Token Bucket.
           Si el usuario superó su cuota de `limite_pedidos_min`, rechaza con
           HTTP 429 DEMASIADOS_PEDIDOS e incluye la cabecera estándar `Retry-After`.
        """
        ruta = request.url.path
        if ruta in rutas_publicas:
            return await call_next(request)
        try:
            identidad = auth.autenticar(request.headers.get("authorization"), config.jwt_secreto)
            permitidos = roles_por_ruta.get(ruta)
            if permitidos and identidad.rol not in permitidos:
                raise ErrorApi(TipoError.ROL_INSUFICIENTE,
                               f"{ruta} es sólo para {'/'.join(sorted(permitidos))} (tu rol: {identidad.rol})")
            if ruta in ("/getRemoteTask", "/videos") and request.method == "POST":
                espera = limitador.reservar(identidad.usuario)
                if espera:
                    reintentar = math.ceil(espera)
                    raise ErrorApi(TipoError.DEMASIADOS_PEDIDOS,
                                   f"más de {config.limite_pedidos_min} pedidos por minuto para {identidad.usuario}: "
                                   f"reintentá en {reintentar} s",
                                   cabeceras={"Retry-After": str(reintentar)})
        except ErrorApi as error:
            res = responder_error(error)
            res.headers["X-Lamport-Clock"] = str(reloj.incrementar())
            return res
        request.state.identidad = identidad
        return await call_next(request)

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

        if request.url.path == "/getRemoteTask":
            estadisticas.registrar_pedido(respuesta.status_code)
        identidad = getattr(request.state, "identidad", None)
        log.info("%s %s | %d | %.0f ms | lamport_ts=%d | usuario=%s", request.method, request.url.path,
                 respuesta.status_code, (time.perf_counter() - inicio) * 1000, ts_salida,
                 identidad.usuario if identidad else "-")
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

    # --------------------------------------------------------------------------
    # 2. Endpoint de Ejecución de Tareas Remotas (Requisitos R3, R4 y R5)
    # --------------------------------------------------------------------------
    @app.post("/getRemoteTask")
    async def getRemoteTask(request: Request):  # noqa: N802 (nombre del enunciado)
        """Recepciona, valida y despacha una tarea remota al pool de workers.
        
        Flujo de Ejecución Desacoplado:
        1. CONTROL PREVIO (Fail-Fast): Valida tamaño del cuerpo, sintaxis JSON y whitelist
           de imágenes ANTES de encolar. Una petición inválida se rechaza en microsegundos y
           nunca consume memoria en la cola ni ocupa un hilo worker.
        2. DESACOPLAMIENTO TEMPORAL: La tarea se ingresa en el `PoolWorkers` con el timestamp
           lógico de Lamport del cliente (`ts_solicitud`). Si todos los workers están ocupados,
           la petición espera en la Min-Heap sin saturar hilos ni rechazar la conexión.
        3. MÉTRICAS DIFERENCIADAS (R5):
           - `en_cola_s`: Tiempo de espera en la cola hasta que un worker libre la toma.
           - `ejecucion_s`: Tiempo neto de cómputo dentro del contenedor Docker aislado.
           - `total_s`: Latencia total percibida por el cliente HTTP.
        """
        inicio_total = time.perf_counter()
        task_id = str(uuid.uuid4())
        cuerpo = await request.body()
        if len(cuerpo) > config.max_cuerpo:
            raise ErrorApi(TipoError.CUERPO_DEMASIADO_GRANDE,
                           f"el cuerpo supera los {config.max_cuerpo} bytes")
        solicitud = validar_solicitud(cuerpo, request.headers.get("content-type"))
        # Antes de encolar: una imagen inválida no ocupa lugar en la cola ni un worker.
        imagen = await _validar_imagen(solicitud.imagen)

        ts_solicitud = request.state.ts_cliente
        log.info("tarea | %s | %s | calculo=%s | lamport_ts=%d", task_id, imagen.referencia, solicitud.calculo, ts_solicitud)

        # Encolar en el pool de workers (con exclusión mutua y ordenamiento Lamport)
        inicio_tarea = time.perf_counter()
        try:
            ejecucion, en_cola_s = await run_in_threadpool(
                pool.ejecutar_tarea,
                imagen,
                solicitud.calculo,
                solicitud.parametros,
                solicitud.datos,
                ts_solicitud
            )
        except Exception:
            estadisticas.registrar_tarea(imagen.referencia, False, time.perf_counter() - inicio_tarea)
            raise
        estadisticas.registrar_tarea(imagen.referencia, True, time.perf_counter() - inicio_tarea)

        ts_respuesta = reloj.incrementar()
        return responder(
            200,
            {
                "calculo": solicitud.calculo,
                "resultado": ejecucion.resultado,
                "task_id": task_id,
                "imagen": imagen.referencia,
                "digest": ejecucion.digest,
                "imagen_id": ejecucion.imagen_id,
                "contenedor": ejecucion.contenedor,
                "tiempos": {"en_cola_s": en_cola_s, "ejecucion_s": ejecucion.ejecucion_s,
                            "total_s": round(time.perf_counter() - inicio_total, 4)},
                "lamport_ts": ts_respuesta
            },
            cabeceras={"X-Lamport-Clock": str(ts_respuesta)}
        )

    # --------------------------------------------------------------------------
    # 3. Endpoint de Observabilidad y Telemetría (Requisito R4)
    # --------------------------------------------------------------------------
    @app.get("/stats")
    def stats():
        """Devuelve telemetría operativa en vivo (exclusivo para usuarios con rol `admin`).
        
        Incluye tareas ejecutadas, fallidas, ventana de latencias con percentil P95,
        distribución por imagen, estado del pool de workers y cola de Lamport.
        """
        ts_respuesta = reloj.incrementar()
        return responder(200, {**estadisticas.instantanea(pool), "lamport_ts": ts_respuesta},
                         cabeceras={"X-Lamport-Clock": str(ts_respuesta)})

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
                "videos": {
                    "en_cola": video_pool.tareas_encoladas,
                    "procesando": video_pool.workers_activos,
                    "max_simultaneos": config.video_workers_max,
                    "max_en_cola": config.video_cola_max,
                },
                "lamport_ts": ts_respuesta
            },
            cabeceras={"X-Lamport-Clock": str(ts_respuesta)}
        )

    # --------------------------------------------------------------------------
    # 4. Endpoints de Transcodificación Asíncrona de Video (Hits 2 y 3)
    # --------------------------------------------------------------------------
    @app.post("/videos")
    async def crear_video(request: Request):
        """Acepta un trabajo de transcodificación (202 Accepted) con validación previa estricta."""
        identidad = request.state.identidad
        ts_solicitud = request.state.ts_cliente

        ct = request.headers.get("content-type", "")
        if not ct.startswith("multipart/form-data") and not ct.startswith("application/json"):
            raise ErrorApi(
                TipoError.TIPO_DE_CONTENIDO,
                "tipo de contenido no soportado, se requiere multipart/form-data o application/json"
            )

        # Control de cola saturada antes de recibir el cuerpo
        if video_pool.cola_max > 0 and video_pool.tareas_encoladas >= video_pool.cola_max:
            raise ErrorApi(
                TipoError.SERVICIO_SATURADO,
                f"cola saturada ({video_pool.tareas_encoladas} trabajos en espera, máximo {video_pool.cola_max})",
                cabeceras={"Retry-After": "30"}
            )

        # Reserva atómica previa de cuota (R3): previene carreras TOCTOU antes de procesar el cuerpo
        id_trabajo = uuid.uuid4().hex
        video_servicio.reservar_cuota_usuario(identidad.usuario, id_trabajo)
        dir_trabajo = os.path.join(config.video_dir, id_trabajo)

        try:
            if ct.startswith("multipart/form-data"):
                nombre_archivo, bytes_totales, calidades_str = await video_servicio.recibir_video_multipart(
                    request, dir_trabajo
                )
                calidades = video_servicio.validar_calidades(calidades_str)
                ruta_entrada = os.path.join(dir_trabajo, "entrada")
                meta_video = await run_in_threadpool(
                    video_servicio.validar_metadatos_video, ruta_entrada, calidades
                )
                trabajo = video_servicio.crear_trabajo(
                    usuario=identidad.usuario,
                    nombre_archivo=nombre_archivo,
                    bytes_totales=bytes_totales,
                    meta_video=meta_video,
                    calidades=calidades,
                    ts_solicitud=ts_solicitud,
                    id_trabajo=id_trabajo,
                )
            else:
                # application/json (modo URL)
                cuerpo_bytes = await request.body()
                if not cuerpo_bytes:
                    raise ErrorApi(TipoError.CUERPO_VACIO, "el cuerpo no puede estar vacío")
                import json
                try:
                    datos = json.loads(cuerpo_bytes)
                except Exception:
                    raise ErrorApi(TipoError.JSON_INVALIDO, "JSON mal formado")
                if not isinstance(datos, dict):
                    raise ErrorApi(TipoError.JSON_INVALIDO, "se esperaba un objeto JSON")

                if "video" in datos and "url" in datos:
                    raise ErrorApi(TipoError.PAYLOAD_INVALIDO, "no se pueden enviar 'video' y 'url' simultáneamente")
                if "url" not in datos:
                    raise ErrorApi(TipoError.PAYLOAD_INVALIDO, "falta el campo 'url'", detalles=["url"])
                if "calidades" not in datos:
                    raise ErrorApi(TipoError.PAYLOAD_INVALIDO, "falta el campo 'calidades'", detalles=["calidades"])

                for k in datos:
                    if k not in ("url", "calidades"):
                        raise ErrorApi(TipoError.PAYLOAD_INVALIDO, f"campo no permitido: {k}", detalles=[k])

                url = datos["url"]
                if not isinstance(url, str) or not url.strip():
                    raise ErrorApi(TipoError.URL_INVALIDA, "la URL no puede estar vacía")

                calidades = video_servicio.validar_calidades(datos["calidades"])
                meta_video = await run_in_threadpool(
                    video_servicio.validar_url_y_metadatos, url, calidades
                )
                trabajo = video_servicio.crear_trabajo(
                    usuario=identidad.usuario,
                    nombre_archivo=meta_video["nombre"],
                    bytes_totales=meta_video["bytes"],
                    meta_video=meta_video,
                    calidades=calidades,
                    ts_solicitud=ts_solicitud,
                    id_trabajo=id_trabajo,
                    url=url,
                )
        except Exception:
            # En caso de cualquier error durante recepción o validación, liberar cuota y limpiar disco
            video_servicio.liberar_cuota_usuario(identidad.usuario, id_trabajo)
            import shutil
            shutil.rmtree(dir_trabajo, ignore_errors=True)
            raise

        ts_respuesta = reloj.incrementar()
        trabajo["lamport_ts"] = ts_respuesta
        return responder(
            202,
            trabajo,
            cabeceras={
                "Location": f"/videos/{trabajo['id']}",
                "X-Lamport-Clock": str(ts_respuesta)
            }
        )

    @app.get("/videos/{id_trabajo}")
    def consultar_video(id_trabajo: str, request: Request):
        """Consulta el estado del trabajo respetando propiedad y agregando Retry-After."""
        identidad = request.state.identidad
        es_admin = identidad.rol == auth.ROL_ADMIN
        contenido, retry_after = video_servicio.consultar_trabajo(
            id_trabajo=id_trabajo,
            usuario_solicitante=identidad.usuario,
            es_admin=es_admin
        )
        ts_respuesta = reloj.incrementar()
        contenido["lamport_ts"] = ts_respuesta
        cabeceras = {"X-Lamport-Clock": str(ts_respuesta)}
        if retry_after:
            cabeceras["Retry-After"] = str(retry_after)
        return responder(200, contenido, cabeceras=cabeceras)

    @app.get("/videos/{id_trabajo}/{calidad}")
    def descargar_video(id_trabajo: str, calidad: str, request: Request):
        """Descarga el video transcodificado en formato MP4 si el trabajo finalizó con éxito."""
        identidad = request.state.identidad
        es_admin = identidad.rol == auth.ROL_ADMIN
        try:
            calidad_num = int(calidad)
        except ValueError:
            raise ErrorApi(
                TipoError.CALIDAD_NO_SOLICITADA,
                f"calidad inválida: {calidad}"
            )

        ruta_archivo, nombre_descarga = video_servicio.preparar_descarga(
            id_trabajo=id_trabajo,
            calidad=calidad_num,
            usuario_solicitante=identidad.usuario,
            es_admin=es_admin
        )
        ts_respuesta = reloj.incrementar()
        return FileResponse(
            path=ruta_archivo,
            media_type="video/mp4",
            filename=nombre_descarga,
            headers={
                "X-Lamport-Clock": str(ts_respuesta),
                "Content-Disposition": f'attachment; filename="{nombre_descarga}"'
            }
        )

    return app
