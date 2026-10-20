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
    """Fábrica de la aplicación FastAPI para Hit 2.
    
    Inicializa todos los subsistemas requeridos por la arquitectura distribuida:
    - Configuración centralizada desde variables de entorno.
    - Sistema de registro multi-destino (stdout, cola circular RAM y archivo rotativo).
    - Orquestador de Docker (`Lanzador`) con soporte de contención cgroups (R5).
    - Reloj lógico de Lamport (`RelojLamport`) para ordenamiento parcial/total causal.
    - Pool de workers (`PoolWorkers`) con cola de prioridad Min-Heap ordenada por Lamport.
    - Pool de workers y servicio de transcodificación de video asíncrono.
    - Recolector de estadísticas operativas en vivo (percentil P95, métricas por imagen).
    - Limitador de tasa por usuario basado en algoritmo Token Bucket.
    - Lista blanca estricta de imágenes y digests autorizados.
    """
    config = config or Config.desde_entorno()
    # Verificación de robustez del secreto criptográfico para firma de JWT
    auth.validar_secreto(config.jwt_secreto)
    # Configuración de bitácora centralizada con buffer circular para inspección en /stats
    log, _ = registro.configurar(config.dir_logs)
    # Instanciación del lanzador Docker con inyección de dependencias
    lanzador = lanzador or Lanzador(config, log)
    # Reloj lógico de Leslie Lamport (1978 CACM) para sincronización de causalidad
    reloj = reloj or RelojLamport()
    # Pool de workers para tareas estándar acotado a workers_max con cola Min-Heap
    pool = pool or PoolWorkers(lanzador, config.workers_max, log)
    # Pool de workers especializado para video con límite de concurrencia y cola finita
    video_pool = video_pool or PoolWorkers(lanzador, config.video_workers_max, log, cola_max=config.video_cola_max)
    # Servicio de transcodificación asíncrona de video con integración FFprobe y Docker
    video_servicio = video_servicio or VideoServicio(config, video_pool, lanzador, log)
    # Acumulador de métricas en memoria para monitoreo en tiempo real
    estadisticas = Estadisticas()
    # Limitador de tasa Token Bucket por usuario (solicitudes permitidas por minuto)
    limitador = LimitadorPorUsuario(config.limite_pedidos_min)
    # Mapeo de imágenes y digests autorizados por política de seguridad
    permitidas = imagenes.lista_blanca(config.imagenes_permitidas)
    if not permitidas:
        log.warning("TP2_IMAGENES_PERMITIDAS está vacía: se va a rechazar toda tarea")

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app_inst: FastAPI):
        """Gestor de ciclo de vida de la aplicación FastAPI.
        
        Garantiza el inicio y la detención limpia (graceful shutdown) de tareas en segundo plano,
        especialmente el hilo del recolector de basura (GC) que elimina videos vencidos en disco.
        """
        # Punto de cesión: aquí comienza a ejecutarse el servidor web
        yield
        # Fase de apagado (shutdown): detenemos el recolector de basura periódico si fue instanciado
        if hasattr(video_servicio, "detener_recolector_basura"):
            video_servicio.detener_recolector_basura()

    app = FastAPI(
        title="TP2 · Hit 2 · Servidor con Pool de Workers y Relojes de Lamport",
        redirect_slashes=False,
        lifespan=lifespan,
    )

    # Definición de políticas de acceso RBAC por ruta:
    # Rutas públicas que no requieren encabezado Authorization: Bearer
    rutas_publicas = {"/health"}
    # Rutas privilegiadas que exigen un rol específico (ej. admin para estadísticas)
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
        4. Si es un POST a `/getRemoteTask` o `/videos`, consulta el limitador de tasa Token Bucket.
           Si el usuario superó su cuota de `limite_pedidos_min`, rechaza con
           HTTP 429 DEMASIADOS_PEDIDOS e incluye la cabecera estándar `Retry-After`.
        """
        ruta = request.url.path
        # Paso 1: Verificación de rutas exentas de autenticación
        if ruta in rutas_publicas:
            return await call_next(request)
        try:
            # Paso 2: Autenticación criptográfica del token Bearer JWT
            identidad = auth.autenticar(request.headers.get("authorization"), config.jwt_secreto)
            
            # Paso 3: Control de acceso basado en roles (RBAC)
            permitidos = roles_por_ruta.get(ruta)
            if permitidos and identidad.rol not in permitidos:
                raise ErrorApi(TipoError.ROL_INSUFICIENTE,
                               f"{ruta} es sólo para {'/'.join(sorted(permitidos))} (tu rol: {identidad.rol})")
            
            # Paso 4: Limitación de tasa por usuario (Token Bucket) en endpoints de procesamiento
            if ruta in ("/getRemoteTask", "/videos") and request.method == "POST":
                espera = limitador.reservar(identidad.usuario)
                if espera:
                    reintentar = math.ceil(espera)
                    raise ErrorApi(TipoError.DEMASIADOS_PEDIDOS,
                                   f"más de {config.limite_pedidos_min} pedidos por minuto para {identidad.usuario}: "
                                   f"reintentá en {reintentar} s",
                                   cabeceras={"Retry-After": str(reintentar)})
        except ErrorApi as error:
            # En caso de rechazo temprano, construimos la respuesta de error estructurada
            res = responder_error(error)
            # Todo mensaje de salida incrementa y propaga el reloj de Lamport
            res.headers["X-Lamport-Clock"] = str(reloj.incrementar())
            return res
        
        # Inyectamos los datos del usuario autenticado en el estado de la petición
        request.state.identidad = identidad
        return await call_next(request)

    # --------------------------------------------------------------------------
    # 2. Middleware de Bitácora y Reloj Lógico de Lamport (Causalidad)
    # --------------------------------------------------------------------------
    @app.middleware("http")
    async def bitacora(request: Request, call_next):
        """Interpreta marcas temporales de Lamport, mide latencias y registra auditoría.
        
        En Starlette/FastAPI los middlewares se ejecutan en orden inverso de declaración:
        este middleware envuelve externamente a toda la petición, permitiendo:
        1. Capturar el instante de inicio exacto mediante reloj monotónico de alta resolución.
        2. Extraer el timestamp de Lamport recibido en la cabecera `X-Lamport-Clock`.
        3. Aplicar la Regla 2 de Lamport al recibir un mensaje:
           L_servidor = max(L_servidor, L_cliente) + 1
        4. Pasar la petición a los endpoints internos.
        5. Garantizar que toda respuesta contenga la cabecera `X-Lamport-Clock` actualizada (Regla 1).
        6. Registrar métricas de latencia y código de estado en el subsistema de telemetría.
        """
        inicio = time.perf_counter()

        # Extraemos el timestamp de Lamport del cliente desde la cabecera HTTP
        header_ts = request.headers.get("x-lamport-clock") or request.headers.get("X-Lamport-Clock")
        ts_cliente = 0
        if header_ts and header_ts.isdigit():
            ts_cliente = int(header_ts)

        # Regla 2 de Lamport al recibir: L = max(L, L_cliente) + 1.
        # Esto sincroniza el reloj local del servidor para que sea estrictamente posterior al del emisor.
        reloj.actualizar(ts_cliente)
        # Almacenamos el timestamp original del cliente para desempatar prioridades en el worker pool
        request.state.ts_cliente = ts_cliente

        try:
            # Transferimos el control al middleware interno o handler de la ruta
            respuesta = await call_next(request)
        except Exception:
            # Captura de contingencia ante cualquier excepción no controlada
            log.exception("%s %s | error inesperado", request.method, request.url.path)
            respuesta = responder_error(ErrorApi(TipoError.ERROR_INTERNO, "error interno del servidor"))

        # Regla 1 de Lamport al enviar: si la respuesta no trae aún el reloj, lo incrementamos e inyectamos
        if "x-lamport-clock" not in respuesta.headers:
            respuesta.headers["X-Lamport-Clock"] = str(reloj.incrementar())
        ts_salida = int(respuesta.headers["x-lamport-clock"])

        # Registro de telemetría para monitoreo de pedidos a tareas remotas
        if request.url.path == "/getRemoteTask":
            estadisticas.registrar_pedido(respuesta.status_code)
        
        identidad = getattr(request.state, "identidad", None)
        # Log estructurado con método, ruta, código HTTP, tiempo en ms, Lamport ts y usuario
        log.info("%s %s | %d | %.0f ms | lamport_ts=%d | usuario=%s", request.method, request.url.path,
                 respuesta.status_code, (time.perf_counter() - inicio) * 1000, ts_salida,
                 identidad.usuario if identidad else "-")
        return respuesta

    # --------------------------------------------------------------------------
    # Manejadores de Excepciones Globales
    # --------------------------------------------------------------------------
    @app.exception_handler(ErrorApi)
    async def _error_api(request: Request, error: ErrorApi):
        """Manejador centralizado de errores de negocio `ErrorApi`.
        
        Garantiza que la respuesta cumpla con el sobre estándar del protocolo (contrato §5),
        registre la advertencia en el log y propague el reloj de Lamport incrementado.
        """
        if error.interno:
            log.warning("%s %s | %s | %s", request.method, request.url.path, error.tipo.value, error.interno)
        ts_salida = reloj.incrementar()
        res = responder_error(error)
        res.headers["X-Lamport-Clock"] = str(ts_salida)
        return res

    @app.exception_handler(StarletteHTTPException)
    async def _error_http(request: Request, error: StarletteHTTPException):
        """Manejador centralizado de errores de enrutamiento HTTP de Starlette.
        
        Distingue claramente entre:
        - HTTP 405 (METODO_NO_PERMITIDO): Si la ruta existe pero se usó un verbo incorrecto
          (ej. GET en lugar de POST), preservando los encabezados `Allow` correspondientes.
        - HTTP 404 (RUTA_INEXISTENTE): Si la URL no coincide con ningún endpoint registrado.
        Inyecta en ambos casos el reloj de Lamport incrementado.
        """
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
        """Verifica que la imagen solicitada cumpla con las políticas de seguridad del servidor.
        
        1. Formato: Parsea nombre, versión y registry (verificado previamente en validación).
        2. Lista Blanca: Comprueba que el repositorio esté explícitamente habilitado.
        3. Existencia y Digest: Descarga la imagen si no existe y verifica que su SHA256
           coincida con los digests criptográficos autorizados en la configuración.
        """
        imagen = imagenes.parsear(texto)
        # Verificamos si el repositorio base pertenece a la lista blanca autorizada
        if imagen.nombre not in permitidas:
            raise ErrorApi(TipoError.IMAGEN_NO_PERMITIDA,
                           f"la imagen {imagen.nombre} no está en la lista de imágenes permitidas")
        # Aseguramos la imagen en el daemon Docker ejecutando la operación en el threadpool
        digests = await run_in_threadpool(lanzador.preparar_imagen, imagen)
        fijados = permitidas[imagen.nombre]
        # Si la lista blanca especificó digests inmutables requeridos, verificamos la intersección
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
        # Leemos el cuerpo crudo de la solicitud HTTP
        cuerpo = await request.body()
        # Control estricto de tamaño para evitar ataques de denegación de servicio por memoria
        if len(cuerpo) > config.max_cuerpo:
            raise ErrorApi(TipoError.CUERPO_DEMASIADO_GRANDE,
                           f"el cuerpo supera los {config.max_cuerpo} bytes")
        
        # Validación de sintaxis JSON, profundidad y esquema de la solicitud
        solicitud = validar_solicitud(cuerpo, request.headers.get("content-type"))
        
        # Validación temprana de lista blanca de imágenes (fail-fast: no gasta cupo de worker)
        imagen = await _validar_imagen(solicitud.imagen)

        # Obtenemos el timestamp de Lamport del cliente preservado por el middleware
        ts_solicitud = request.state.ts_cliente
        log.info("tarea | %s | %s | calculo=%s | lamport_ts=%d", task_id, imagen.referencia, solicitud.calculo, ts_solicitud)

        # Encolamos en el pool de workers (ordenamiento Min-Heap por reloj de Lamport)
        inicio_tarea = time.perf_counter()
        try:
            # Delegamos la ejecución bloqueante del worker pool a un hilo del threadpool de AnyIO
            ejecucion, en_cola_s = await run_in_threadpool(
                pool.ejecutar_tarea,
                imagen,
                solicitud.calculo,
                solicitud.parametros,
                solicitud.datos,
                ts_solicitud
            )
        except Exception:
            # Registramos la tarea fallida en las métricas antes de propagar la excepción
            estadisticas.registrar_tarea(imagen.referencia, False, time.perf_counter() - inicio_tarea)
            raise
        
        # Registramos la tarea exitosa con su tiempo de ejecución neto
        estadisticas.registrar_tarea(imagen.referencia, True, time.perf_counter() - inicio_tarea)

        # Incrementamos el reloj de Lamport local para el evento de envío de respuesta
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
        
        Expone métricas agregadas: total de tareas procesadas, tasa de fallos,
        latencias con percentil P95 (usando interpolación lineal), desglose por imagen,
        y el estado instantáneo de la cola y workers ocupados.
        """
        ts_respuesta = reloj.incrementar()
        return responder(200, {**estadisticas.instantanea(pool), "lamport_ts": ts_respuesta},
                         cabeceras={"X-Lamport-Clock": str(ts_respuesta)})

    @app.get("/health")
    def health():
        """Endpoint de salud del servicio (liveness / readiness probe).
        
        Público y accesible sin autenticación para monitoreo por orquestadores.
        Devuelve HTTP 200 si Docker está disponible o HTTP 503 si el daemon cayó,
        junto con la carga actual del pool de workers y de videos.
        """
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
    # 4. Endpoints de Transcodificación Asíncrona de Video (Pipeline Asíncrono)
    # --------------------------------------------------------------------------
    @app.post("/videos")
    async def crear_video(request: Request):
        """Acepta un trabajo de transcodificación (HTTP 202 Accepted) con validación previa estricta.
        
        Mecanismos de Resiliencia:
        1. Soporte Dual de Ingesta: multipart/form-data (subida directa) o application/json (URL remota).
        2. Control de Saturación: Si la cola de videos supera `video_cola_max`, rechaza con 503 y Retry-After.
        3. Reserva Atómica de Cuota (Anti-TOCTOU): Registra el trabajo bajo el usuario solicitante
           antes de procesar el archivo, impidiendo superar el límite de trabajos concurrentes por usuario.
        4. Transcodificación en Segundo Plano: El trabajo se encola en el `video_pool` y se devuelve
           de inmediato un ID con la cabecera `Location: /videos/{id}`.
        """
        identidad = request.state.identidad
        ts_solicitud = request.state.ts_cliente

        ct = request.headers.get("content-type", "")
        # Verificamos que el Content-Type sea compatible
        if not ct.startswith("multipart/form-data") and not ct.startswith("application/json"):
            raise ErrorApi(
                TipoError.TIPO_DE_CONTENIDO,
                "tipo de contenido no soportado, se requiere multipart/form-data o application/json"
            )

        # Control de contrapresión (backpressure): cola saturada
        if video_pool.cola_max > 0 and video_pool.tareas_encoladas >= video_pool.cola_max:
            raise ErrorApi(
                TipoError.SERVICIO_SATURADO,
                f"cola saturada ({video_pool.tareas_encoladas} trabajos en espera, máximo {video_pool.cola_max})",
                cabeceras={"Retry-After": "30"}
            )

        # Reserva atómica de cuota por usuario para evitar Time-Of-Check to Time-Of-Use race conditions
        id_trabajo = uuid.uuid4().hex
        video_servicio.reservar_cuota_usuario(identidad.usuario, id_trabajo)
        dir_trabajo = os.path.join(video_servicio.dir_trabajos, id_trabajo)

        try:
            if ct.startswith("multipart/form-data"):
                # Ingesta por streaming multipart directo a disco
                nombre_archivo, bytes_totales, calidades_str = await video_servicio.recibir_video_multipart(
                    request, dir_trabajo
                )
                calidades = video_servicio.validar_calidades(calidades_str)
                ruta_entrada = os.path.join(dir_trabajo, "entrada")
                # Inspección de contenedores de video mediante FFprobe aislado
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
                # Ingesta por URL remota (application/json)
                cuerpo_bytes = await request.body()
                if not cuerpo_bytes:
                    raise ErrorApi(TipoError.CUERPO_VACIO, "el cuerpo no puede estar vacío")
                import json
                try:
                    datos = json.loads(cuerpo_bytes)
                except Exception:
                    raise ErrorApi(TipoError.JSON_INVALIDO, "JSON mal formado")
                if not isinstance(datos, dict):
                    raise ErrorApi(TipoError.PAYLOAD_INVALIDO, "se esperaba un objeto JSON")

                # Regla de exclusión mutua de payload
                if "video" in datos and "url" in datos:
                    raise ErrorApi(TipoError.PAYLOAD_INVALIDO, "no se pueden enviar 'video' y 'url' simultáneamente", detalles=["video", "url"])

                faltantes = []
                if "url" not in datos:
                    faltantes.append("url")
                if "calidades" not in datos:
                    faltantes.append("calidades")
                if faltantes:
                    raise ErrorApi(
                        TipoError.PAYLOAD_INVALIDO,
                        f"faltan campos obligatorios: {', '.join(faltantes)}",
                        detalles=faltantes
                    )

                sobrantes = [k for k in datos if k not in ("url", "calidades")]
                if sobrantes:
                    raise ErrorApi(
                        TipoError.PAYLOAD_INVALIDO,
                        f"campos no permitidos: {', '.join(sobrantes)}",
                        detalles=sobrantes
                    )

                url = datos["url"]
                if not isinstance(url, str) or not url.strip():
                    raise ErrorApi(TipoError.URL_INVALIDA, "la URL no puede estar vacía")

                calidades = video_servicio.validar_calidades(datos["calidades"])
                # Validación de seguridad SSRF (no loopback, no redes privadas) y descarga controlada
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
            # Reversión de cuota y limpieza de archivos temporales si la ingesta falla
            video_servicio.liberar_cuota_usuario(identidad.usuario, id_trabajo)
            import shutil
            shutil.rmtree(dir_trabajo, ignore_errors=True)
            raise

        # Asignamos el timestamp de Lamport al sobre de confirmación 202 Accepted
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
        """Consulta el estado del trabajo respetando la titularidad de los datos.
        
        Solo el propietario del video o un usuario con rol `admin` puede ver el progreso.
        Si el trabajo sigue procesándose, incluye la cabecera `Retry-After` con una
        estimación del tiempo restante basada en la duración del video y las calidades.
        """
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
        """Descarga el video transcodificado en formato MP4 si el trabajo finalizó con éxito.
        
        Verifica:
        1. Formato numérico de la resolución (ej. 720 o 1080).
        2. Que el trabajo haya concluido y generado el archivo correspondiente.
        3. Titularidad del usuario solicitante (o privilegios de administrador).
        Retorna una respuesta de streaming de archivo (FileResponse) con headers de descarga.
        """
        identidad = request.state.identidad
        es_admin = identidad.rol == auth.ROL_ADMIN
        try:
            calidad_num = int(calidad)
        except ValueError:
            raise ErrorApi(
                TipoError.CALIDAD_NO_SOLICITADA,
                f"calidad inválida: {calidad}"
            )

        # Prepara la ruta absoluta del archivo y el nombre de descarga amigable
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

