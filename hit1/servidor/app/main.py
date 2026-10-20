"""Servidor Principal de Tareas Remotas - API HTTP (Hit 1).

Punto de entrada de la aplicación FastAPI.
Se ejecuta habitualmente con:
    uvicorn --factory app.main:crear_app --host 0.0.0.0 --port 8080

Patrón de Diseño Aplicado (Application Factory):
- La función `crear_app(config=None, lanzador=None)` permite instanciar el servidor
  con parámetros inyectados. En entornos de testeo unitario o de integración, se pasan
  dobles (mocks) de Docker para verificar la API sin requerir un daemon Docker real.

Arquitectura de la Pila de Middleware (Flujo por Petición):
1. `bitacora` (Logging): Mide latencia total y registra en log estructurado cada petición.
2. `autenticacion` (JWT + RBAC + Rate Limit):
   - Valida la cabecera `Authorization: Bearer <token>`.
   - Controla permisos por rol (RBAC: 'admin' para `/stats`, 'usuario' para `/getRemoteTask`).
   - Aplica limitación de tasa (Token Bucket) sobre `POST /getRemoteTask`.
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
    """Rastreador concurrente de tareas en ejecución activa.

    Diferencia Arquitectónica Clave (Hit 1 vs Hit 2):
    - En Hit 1 NO existe un pool acotado de workers ni cola de espera:
      cada pedido entrante que supera la validación lanza un hilo dedicado inmediatamente.
    - Por ende, en Hit 1:
      `workers_max = None` (sin límite prefijado)
      `tareas_encoladas = 0` (no hay encolamiento previo)
      `workers_activos` = número exacto de tareas corriendo en este instante.
    """

    workers_max = None
    tareas_encoladas = 0

    def __init__(self):
        self._candado = threading.Lock()
        self._n = 0

    @property
    def workers_activos(self) -> int:
        """Cantidad de hilos/contenedores actualmente en ejecución."""
        with self._candado:
            return self._n

    def __enter__(self):
        """Incrementa el contador al comenzar la ejecución de una tarea."""
        with self._candado:
            self._n += 1

    def __exit__(self, *_):
        """Decrementa el contador de forma segura al finalizar la tarea (éxito o error)."""
        with self._candado:
            self._n -= 1


def crear_app(config=None, lanzador=None) -> FastAPI:
    """Fábrica de la aplicación FastAPI.

    Inicializa los subsistemas de autenticación, logging, límites, whitelist de imágenes,
    middlewares, manejadores de excepción y rutas HTTP.
    """
    # 1. Carga de configuración (desde variables de entorno si no fue inyectada)
    config = config or Config.desde_entorno()

    # 2. Validación Fail-Closed del secreto JWT: si no tiene >= 32 caracteres, aborta el arranque
    auth.validar_secreto(config.jwt_secreto)

    # 3. Inicialización del subsistema de logging multi-destino (consola, RAM y disco rotativo)
    log, _ = registro.configurar(config.dir_logs)

    # 4. Inicialización del orquestador Docker
    lanzador = lanzador or Lanzador(config, log)

    # 5. Componentes de estado en memoria
    estadisticas = Estadisticas()
    en_curso = _EnCurso()
    limitador = LimitadorPorUsuario(config.limite_pedidos_min)

    # 6. Carga y parsing de la lista blanca de imágenes permitidas (R2)
    permitidas = imagenes.lista_blanca(config.imagenes_permitidas)
    if not permitidas:
        log.warning("TP2_IMAGENES_PERMITIDAS está vacía: se va a rechazar toda tarea")

    # 7. Creación de la instancia FastAPI
    # Nota técnica: `redirect_slashes=False` evita que peticiones como `/health/` respondan
    # con un redirect HTTP 307 a `/health`. En su lugar, responden con 404 estricto en JSON.
    app = FastAPI(title="TP2 · Hit 1 · Servidor de tareas remotas", redirect_slashes=False)

    # Definición de políticas de acceso por endpoint
    rutas_publicas = {"/health"}
    roles_por_ruta = {"/stats": {auth.ROL_ADMIN}}

    # --------------------------------------------------------------------------
    # Middleware 1: Autenticación, Control de Acceso (RBAC) y Rate Limiting (R4)
    # --------------------------------------------------------------------------
    @app.middleware("http")
    async def autenticacion(request: Request, call_next):
        """Interpreta la identidad del cliente antes de permitir el acceso al endpoint."""
        ruta = request.url.path

        # Endpoints públicos (healthcheck para orquestadores y balanceadores)
        if ruta in rutas_publicas:
            return await call_next(request)

        try:
            # Paso 1: Validar el token Bearer JWT contra el secreto criptográfico
            identidad = auth.autenticar(request.headers.get("authorization"), config.jwt_secreto)

            # Paso 2: Control de Autorización por Rol (RBAC)
            permitidos = roles_por_ruta.get(ruta)
            if permitidos and identidad.rol not in permitidos:
                raise ErrorApi(
                    TipoError.ROL_INSUFICIENTE,
                    f"{ruta} es sólo para {'/'.join(sorted(permitidos))} (tu rol: {identidad.rol})",
                )

            # Paso 3: Rate Limiting por usuario (Token Bucket) sobre creación de tareas
            if ruta == "/getRemoteTask" and request.method == "POST":
                espera = limitador.reservar(identidad.usuario)
                if espera:
                    reintentar = math.ceil(espera)
                    raise ErrorApi(
                        TipoError.DEMASIADOS_PEDIDOS,
                        f"más de {config.limite_pedidos_min} pedidos por minuto para {identidad.usuario}: "
                        f"reintentá en {reintentar} s",
                        cabeceras={"Retry-After": str(reintentar)},
                    )
        except ErrorApi as error:
            return responder_error(error)

        # Inyectar la identidad validada en el estado de la petición
        request.state.identidad = identidad
        return await call_next(request)

    # --------------------------------------------------------------------------
    # Middleware 2: Bitácora de Auditoría y Trazabilidad de Peticiones
    # --------------------------------------------------------------------------
    @app.middleware("http")
    async def bitacora(request: Request, call_next):
        """Registra una línea estructurada por cada petición con método, ruta, código, latencia y usuario."""
        inicio = time.perf_counter()
        try:
            respuesta = await call_next(request)
        except Exception:
            # Captura de fallas imprevistas no controladas: loguea stack trace completo
            log.exception("%s %s | error inesperado", request.method, request.url.path)
            # Devuelve 500 genérico al cliente sin filtrar detalles internos del servidor (CWE-209)
            respuesta = responder_error(ErrorApi(TipoError.ERROR_INTERNO, "error interno del servidor"))

        # Registro de telemetría de códigos de respuesta para `/stats`
        if request.url.path == "/getRemoteTask":
            estadisticas.registrar_pedido(respuesta.status_code)

        identidad = getattr(request.state, "identidad", None)
        log.info(
            "%s %s | %d | %.0f ms | usuario=%s",
            request.method,
            request.url.path,
            respuesta.status_code,
            (time.perf_counter() - inicio) * 1000,
            identidad.usuario if identidad else "-",
        )
        return respuesta

    # --------------------------------------------------------------------------
    # Manejadores Globales de Excepciones
    # --------------------------------------------------------------------------
    @app.exception_handler(ErrorApi)
    async def _error_api(request: Request, error: ErrorApi):
        """Atrapa excepciones de negocio controladas y las empaqueta en el sobre oficial."""
        if error.interno:
            log.warning(
                "%s %s | %s | %s",
                request.method,
                request.url.path,
                error.tipo.value,
                error.interno,
            )
        return responder_error(error)

    @app.exception_handler(StarletteHTTPException)
    async def _error_http(request: Request, error: StarletteHTTPException):
        """Traduce errores del router de Starlette (404 Not Found y 405 Method Not Allowed) al sobre JSON."""
        if error.status_code == 405:
            return responder_error(
                ErrorApi(
                    TipoError.METODO_NO_PERMITIDO,
                    f"{request.method} no está permitido en {request.url.path}",
                    cabeceras=error.headers,
                )
            )
        return responder_error(
            ErrorApi(TipoError.RUTA_INEXISTENTE, f"no existe la ruta {request.url.path}")
        )

    # --------------------------------------------------------------------------
    # Funciones Auxiliares de Validación de Imágenes (R2)
    # --------------------------------------------------------------------------
    async def _validar_imagen(texto: str):
        """Verifica que la imagen cumpla con la whitelist y los digests autorizados.

        Pasos:
        1. Parsear el nombre y tag (garantiza formato OCI y rechazo de 'latest').
        2. Validar pertenencia del repositorio a la lista blanca (403 IMAGEN_NO_PERMITIDA).
        3. Descargar/preparar la imagen y consultar sus digests reales en el registry.
        4. Si la whitelist fijaba digests criptográficos específicos, verificar que coincidan.
        """
        imagen = imagenes.parsear(texto)
        if imagen.nombre not in permitidas:
            raise ErrorApi(
                TipoError.IMAGEN_NO_PERMITIDA,
                f"la imagen {imagen.nombre} no está en la lista de imágenes permitidas",
            )

        # La preparación de la imagen puede requerir un `docker pull` por red (operación bloqueante),
        # por lo que se ejecuta en el threadpool de FastAPI para no frenar el event loop async.
        digests = await run_in_threadpool(lanzador.preparar_imagen, imagen)
        fijados = permitidas[imagen.nombre]

        if fijados and not fijados & digests:
            raise ErrorApi(
                TipoError.IMAGEN_NO_PERMITIDA,
                f"la imagen {imagen.referencia} no coincide con ningún digest permitido",
                interno=f"digests de la imagen: {sorted(digests) or 'ninguno'}",
            )
        return imagen

    # --------------------------------------------------------------------------
    # Rutas y Endpoints de la API
    # --------------------------------------------------------------------------
    @app.post("/getRemoteTask")
    async def getRemoteTask(request: Request):  # noqa: N802 (nombre del enunciado)
        """Endpoint principal: ejecuta una tarea computacional en un contenedor efímero.

        Flujo Contractual:
        1. Verificación de tamaño máximo del cuerpo HTTP (`config.max_cuerpo` -> 413).
        2. Validación sintáctica y semántica del JSON recibido (`validar_solicitud` -> 400/415/422).
        3. Verificación de seguridad de la imagen Docker solicitada (`_validar_imagen` -> 403).
        4. Despacho al threadpool para orquestar el contenedor en Docker.
        5. Registro de métricas de telemetría (éxito/falla y duración).
        6. Retorno de sobre HTTP 200 con resultado, metadatos y desglose de tiempos.
        """
        inicio_total = time.perf_counter()
        task_id = str(uuid.uuid4())

        # 1. Límite de tamaño de payload
        cuerpo = await request.body()
        if len(cuerpo) > config.max_cuerpo:
            raise ErrorApi(
                TipoError.CUERPO_DEMASIADO_GRANDE,
                f"el cuerpo supera los {config.max_cuerpo} bytes",
            )

        # 2. Validación estricta del esquema del payload
        solicitud = validar_solicitud(cuerpo, request.headers.get("content-type"))

        # 3. Validación de la imagen Docker en la whitelist
        imagen = await _validar_imagen(solicitud.imagen)

        log.info("tarea | %s | %s | calculo=%s", task_id, imagen.referencia, solicitud.calculo)

        # 4. Ejecución en threadpool concurrente
        inicio_tarea = time.perf_counter()
        try:
            with en_curso:
                ejecucion = await run_in_threadpool(
                    lanzador.ejecutarTareaRemota,
                    imagen,
                    solicitud.calculo,
                    solicitud.parametros,
                    solicitud.datos,
                )
        except Exception:
            # Si la tarea falla o crashea, se computa como tarea fallida en /stats
            estadisticas.registrar_tarea(imagen.referencia, False, time.perf_counter() - inicio_tarea)
            raise

        # Registro exitoso
        estadisticas.registrar_tarea(imagen.referencia, True, time.perf_counter() - inicio_tarea)

        # 5. Respuesta empaquetada con todos los campos exigidos por el contrato §3
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
                # En Hit 1 la espera en cola siempre es 0.0 s porque corre de inmediato
                "tiempos": {
                    "en_cola_s": 0.0,
                    "ejecucion_s": ejecucion.ejecucion_s,
                    "total_s": round(time.perf_counter() - inicio_total, 4),
                },
            },
        )

    @app.get("/stats")
    def stats():
        """Retorna estadísticas operativas del servidor (exclusivo para usuarios con rol `admin`)."""
        return responder(200, estadisticas.instantanea(en_curso))

    @app.get("/health")
    def health():
        """Chequeo de salud del servicio (Liveness & Readiness probe).

        Retorna:
            - 200 OK: Servidor y Docker operativos.
            - 503 SERVICE UNAVAILABLE: Daemon de Docker inalcanzable.
        """
        docker_ok = lanzador.disponible()
        return responder(
            200 if docker_ok else 503,
            {"servidor": "ok", "docker": "ok" if docker_ok else "caido"},
        )

    return app
