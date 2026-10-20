"""API HTTP de un nodo del cluster de tareas remotas (Hit 3).

Es el servidor del Hit 1 más los endpoints `/cluster/*`, que usan los nodos
entre sí (elección Bully, heartbeats, asignación y ejecución de tareas). nginx
publica sólo `/getRemoteTask` y `/health`.

Se levanta con `uvicorn --factory app.main:crear_app`. La fábrica recibe la
configuración, el lanzador y el nodo por parámetro: los tests le pasan dobles y
no necesitan Docker.
"""

import json
import math
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import auth, imagenes, registro
from app.config import Config
from app.errores import ErrorApi, TipoError, responder, responder_error
from app.estadisticas import Estadisticas, resumir
from app.lanzador import Lanzador
from app.limitador import LimitadorPorUsuario
from app.nodo import Nodo
from app.validacion import validar_solicitud


class _Mensaje(BaseModel):
    """Esquema base de mensaje inter-nodo del protocolo de cluster (ELECCION, COORDINADOR).
    
    Campos:
    - de: Identificador entero único del nodo emisor (debe ser >= 1).
    Configurado en modo estricto rechazando campos no reconocidos para evitar inyecciones.
    """
    model_config = ConfigDict(extra="forbid", strict=True)
    de: int = Field(ge=1)


class _Heartbeat(_Mensaje):
    """Mensaje de latido periódico emitido por los workers hacia el coordinador activo.
    
    Campos adicionales:
    - tareas_en_curso: Contador de tareas que el worker está procesando en ese instante.
    """
    tareas_en_curso: int = Field(ge=0)


class _PedidoAsignacion(_Mensaje):
    """Solicitud de asignación de nodo para una nueva tarea despachada.
    
    Campos adicionales:
    - excluir: Lista opcional de IDs de nodos caídos o saturados que deben evitarse.
    """
    excluir: list[int] = Field(default_factory=list, max_length=64)


async def _leer_mensaje(request, modelo):
    """Parsea y valida estrictamente el cuerpo JSON de un mensaje del protocolo interno de cluster."""
    try:
        return modelo.model_validate(json.loads(await request.body()))
    except (ValueError, ValidationError):
        # Mapeamos cualquier error de sintaxis o tipo a un error de protocolo estructurado
        raise ErrorApi(TipoError.PAYLOAD_INVALIDO, "mensaje del cluster inválido") from None


def crear_app(config=None, lanzador=None, nodo=None):
    """Fábrica de la aplicación FastAPI para un nodo del cluster distribuido (Hit 3).
    
    Inicializa todos los subsistemas requeridos por la arquitectura simétrica:
    - Configuración del nodo e información de los pares del cluster.
    - Sistema de logging rotativo y consola.
    - Lanzador de contenedores Docker con cgroups y salud activa.
    - Instancia de Nodo con orquestador Bully y registro de estado blando.
    - Métricas locales y limitador de tasa Token Bucket por usuario.
    """
    config = config or Config.desde_entorno()
    # Verificación de robustez del secreto de firma JWT
    auth.validar_secreto(config.jwt_secreto)
    log, _ = registro.configurar(config.dir_logs)
    lanzador = lanzador or Lanzador(config, log)
    nodo = nodo or Nodo(config, log, lanzador)
    estadisticas = Estadisticas()
    limitador = LimitadorPorUsuario(config.limite_pedidos_min)
    permitidas = imagenes.lista_blanca(config.imagenes_permitidas)
    if not permitidas:
        log.warning("TP2_IMAGENES_PERMITIDAS está vacía: se va a rechazar toda tarea")

    @asynccontextmanager
    async def ciclo_de_vida(app):
        """Gestor de ciclo de vida del nodo: inicia hilos de monitoreo al arrancar y los detiene al salir."""
        log.info("cluster | arranca el nodo %d | pares: %s", config.nodo_id,
                 [n for n, _ in config.pares] or "ninguno")
        # Inicia limpieza de contenedores y lanza la primera elección de Bully
        nodo.iniciar()
        yield
        # Detiene de forma ordenada el hilo demonio de heartbeats
        nodo.detener()

    # FastAPI sin redirección de barras para asegurar respuestas JSON canónicas en 404
    app = FastAPI(title="TP2 · Hit 3 · Nodo del cluster de tareas remotas", redirect_slashes=False,
                  lifespan=ciclo_de_vida)

    # Definición de permisos RBAC por ruta:
    # Rutas públicas que no exigen autenticación
    rutas_publicas = {"/health"}
    # Rutas privilegiadas restringidas a administradores
    roles_por_ruta = {"/stats": {auth.ROL_ADMIN}}

    # --------------------------------------------------------------------------
    # 1. Middleware de Autenticación y Control de Tasa (Requisito R4)
    # --------------------------------------------------------------------------
    @app.middleware("http")
    async def autenticacion(request, call_next):
        """Valida identidad JWT, rol y cuota Token Bucket antes de consumir recursos del cluster.
        
        Las rutas internas `/cluster/*` están exentas de autenticación porque circulan
        exclusivamente por la red privada interna de Docker (`cluster`) y nunca son expuestas por Nginx.
        """
        ruta = request.url.path
        if ruta in rutas_publicas or ruta.startswith("/cluster/"):
            return await call_next(request)
        try:
            # Validación criptográfica del token Bearer JWT
            identidad = auth.autenticar(request.headers.get("authorization"), config.jwt_secreto)
            permitidos = roles_por_ruta.get(ruta)
            if permitidos and identidad.rol not in permitidos:
                raise ErrorApi(TipoError.ROL_INSUFICIENTE,
                               f"{ruta} es sólo para {'/'.join(sorted(permitidos))} (tu rol: {identidad.rol})")
            
            # Limitación de tasa Token Bucket en el endpoint público de tareas
            if ruta == "/getRemoteTask" and request.method == "POST":
                espera = limitador.reservar(identidad.usuario)
                if espera:
                    reintentar = math.ceil(espera)
                    raise ErrorApi(TipoError.DEMASIADOS_PEDIDOS,
                                   f"más de {config.limite_pedidos_min} pedidos por minuto para {identidad.usuario} "
                                   f"en este nodo: reintentá en {reintentar} s",
                                   cabeceras={"Retry-After": str(reintentar)})
        except ErrorApi as error:
            return responder_error(error)
        
        request.state.identidad = identidad
        return await call_next(request)

    # --------------------------------------------------------------------------
    # 2. Middleware de Auditoría y Bitácora
    # --------------------------------------------------------------------------
    @app.middleware("http")
    async def bitacora(request, call_next):
        """Mide la latencia de procesamiento, captura errores imprevistos y registra auditoría."""
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

    # --------------------------------------------------------------------------
    # Manejadores de Excepciones Globales
    # --------------------------------------------------------------------------
    @app.exception_handler(ErrorApi)
    async def _error_api(request, error):
        """Captura errores de dominio estructurados y los formatea según contrato §5."""
        if error.interno:
            log.warning("%s %s | %s | %s", request.method, request.url.path, error.tipo.value,
                        error.interno)
        return responder_error(error)

    @app.exception_handler(StarletteHTTPException)
    async def _error_http(request, error):
        """Maneja errores HTTP estándar de Starlette (404 RUTA_INEXISTENTE y 405 METODO_NO_PERMITIDO)."""
        if error.status_code == 405:
            return responder_error(ErrorApi(
                TipoError.METODO_NO_PERMITIDO,
                f"{request.method} no está permitido en {request.url.path}", cabeceras=error.headers))
        return responder_error(ErrorApi(TipoError.RUTA_INEXISTENTE, f"no existe la ruta {request.url.path}"))

    async def _solicitud(request):
        """Valida previamente el cuerpo de la tarea y la imagen antes de consultar al coordinador.
        
        Control de Fallo Temprano (Fail-Fast):
        Si la imagen no está en la lista blanca o no existe en el registry, se rechaza
        en el nodo de entrada en microsegundos, evitando consultar al líder o saturar a un worker.
        """
        cuerpo = await request.body()
        if len(cuerpo) > config.max_cuerpo:
            raise ErrorApi(TipoError.CUERPO_DEMASIADO_GRANDE,
                           f"el cuerpo supera los {config.max_cuerpo} bytes")
        solicitud = validar_solicitud(cuerpo, request.headers.get("content-type"))
        imagen = imagenes.parsear(solicitud.imagen)
        if imagen.nombre not in permitidas:
            raise ErrorApi(TipoError.IMAGEN_NO_PERMITIDA,
                           f"la imagen {imagen.nombre} no está en la lista de imágenes permitidas")
        digests = await run_in_threadpool(lanzador.preparar_imagen, imagen)
        fijados = permitidas[imagen.nombre]
        if fijados and not fijados & digests:
            raise ErrorApi(TipoError.IMAGEN_NO_PERMITIDA,
                           f"la imagen {imagen.referencia} no coincide con ningún digest permitido",
                           interno=f"digests de la imagen: {sorted(digests) or 'ninguno'}")
        return solicitud, imagen

    # --------------------------------------------------------------------------
    # Endpoints Públicos (Expuestos por Nginx hacia los Clientes)
    # --------------------------------------------------------------------------
    @app.post("/getRemoteTask")
    async def getRemoteTask(request: Request):  # noqa: N802 (nombre del enunciado)
        """Punto de entrada público para ejecución de tareas distribuidas en el cluster.
        
        Flujo de Ejecución:
        1. Valida payload e imagen localmente (fail-fast).
        2. Invoca `nodo.ejecutar()` mediante el threadpool:
           - Pide asignación al coordinador actual.
           - Si el nodo asignado es local, levanta el contenedor aquí.
           - Si es remoto, le envía la tarea vía `POST /cluster/ejecutar`.
           - Si el worker remoto cae a mitad de camino, reasigna transparentemente.
        3. Retorna el sobre de respuesta indicando qué nodo físico ejecutó el contenedor.
        """
        inicio_total = time.perf_counter()
        task_id = str(uuid.uuid4())
        solicitud, imagen = await _solicitud(request)
        log.info("tarea | %s | %s | calculo=%s", task_id, imagen.referencia, solicitud.calculo)
        
        inicio_tarea = time.perf_counter()
        try:
            despacho = await run_in_threadpool(nodo.ejecutar, imagen, solicitud.calculo,
                                               solicitud.parametros, solicitud.datos)
        except Exception:
            estadisticas.registrar_tarea(imagen.referencia, False, time.perf_counter() - inicio_tarea)
            raise
        
        estadisticas.registrar_tarea(imagen.referencia, True, time.perf_counter() - inicio_tarea)
        ejecucion = despacho.ejecucion
        return responder(200, {
            "calculo": solicitud.calculo,
            "resultado": ejecucion.resultado,
            "task_id": task_id,
            "imagen": imagen.referencia,
            "digest": ejecucion.digest,
            "imagen_id": ejecucion.imagen_id,
            "nodo": despacho.nodo,
            "contenedor": ejecucion.contenedor,
            # 'en_cola_s' representa el tiempo de espera de asignación (coordinador + posibles re-elecciones)
            "tiempos": {"en_cola_s": despacho.asignacion_s, "ejecucion_s": ejecucion.ejecucion_s,
                        "total_s": round(time.perf_counter() - inicio_total, 4)},
        })

    @app.get("/stats")
    async def stats():
        """Métricas consolidadas de todo el cluster (exclusivo para usuarios con rol `admin`).
        
        Consulta en paralelo a los pares del cluster y genera un resumen global agregando
        tareas ejecutadas, tareas fallidas, percentil P95 unificado y desglose por nodo.
        """
        pares, sin_respuesta = await run_in_threadpool(nodo.estadisticas_de_los_pares)
        propio = {"estadisticas": estadisticas.exportar(), "tareas_en_curso": nodo.tareas_en_curso}
        por_nodo = {nodo.mi_id: propio, **{n: c for n, c in pares.items()}}
        contenido = resumir([c["estadisticas"] for c in por_nodo.values()])
        contenido.update({
            "tareas_en_cola": 0,    # En Hit 3 el coordinador asigna inmediatamente al nodo con menor carga
            "workers_ocupados": sum(c["tareas_en_curso"] for c in por_nodo.values()),
            "workers_max": None,
            "nodos": {str(n): {"tareas_ejecutadas": c["estadisticas"]["ejecutadas"],
                               "tareas_fallidas": c["estadisticas"]["fallidas"],
                               "workers_ocupados": c["tareas_en_curso"]} for n, c in sorted(por_nodo.items())},
            "nodos_sin_respuesta": sorted(sin_respuesta),
            "atendido_por": nodo.mi_id,
        })
        return responder(200, contenido)

    @app.get("/health")
    def health():
        """Sondeo de salud y estado de membresía del cluster.
        
        Informa disponibilidad del daemon Docker local, estado del algoritmo Bully
        (quién es el líder actual, rol del nodo local) y la tabla de estado de los pares.
        """
        docker_ok = lanzador.disponible()
        coordinador = nodo.bully.coordinador
        cluster = {"nodo": nodo.mi_id, "coordinador": coordinador,
                   "rol": "coordinador" if coordinador == nodo.mi_id else "worker",
                   "tareas_en_curso": nodo.tareas_en_curso}
        if coordinador == nodo.mi_id:
            cluster["nodos"] = nodo.registro.estado()
        return responder(200 if docker_ok else 503,
                         {"servidor": "ok", "docker": "ok" if docker_ok else "caido", "cluster": cluster})

    # --------------------------------------------------------------------------
    # Endpoints Internos de Coordinación Inter-Nodo (/cluster/*)
    # (Aislados en la red interna de Docker, no expuestos por Nginx)
    # --------------------------------------------------------------------------
    @app.get("/cluster/stats")
    def stats_del_nodo():
        """Exporta las métricas locales de este nodo para consolidación por el coordinador."""
        return responder(200, {"estadisticas": estadisticas.exportar(), "tareas_en_curso": nodo.tareas_en_curso})

    @app.post("/cluster/eleccion")
    async def eleccion(request: Request):
        """Mensaje ELECCION recibido de un nodo con ID inferior.
        
        Responde OK (HTTP 200 con su propio ID) e inicia su propia elección en background.
        """
        mensaje = await _leer_mensaje(request, _Mensaje)
        nodo.bully.recibir_eleccion(mensaje.de)
        return responder(200, {"ok": nodo.mi_id})

    @app.post("/cluster/coordinador")
    async def coordinador(request: Request):
        """Mensaje COORDINADOR anunciando la victoria de un líder.
        
        Si el proclamado tiene un ID menor al propio, rechaza con HTTP 409 e inicia elección.
        """
        mensaje = await _leer_mensaje(request, _Mensaje)
        if not nodo.bully.recibir_coordinador(mensaje.de):
            raise ErrorApi(TipoError.COORDINADOR_RECHAZADO,
                           f"el nodo {nodo.mi_id} es mayor que {mensaje.de}: convoca una elección")
        return responder(200, {"coordinador": mensaje.de})

    def _no_es_coordinador():
        """Generador estandarizado de error cuando un worker recibe peticiones exclusivas de líder."""
        return ErrorApi(TipoError.NO_ES_COORDINADOR, f"el nodo {nodo.mi_id} no es el coordinador",
                        detalles=[{"coordinador": nodo.bully.coordinador}])

    @app.post("/cluster/heartbeat")
    async def heartbeat(request: Request):
        """Endpoint receptor de latidos de los workers (sólo atendido si es coordinador)."""
        mensaje = await _leer_mensaje(request, _Heartbeat)
        if not nodo.recibir_heartbeat(mensaje.de, mensaje.tareas_en_curso):
            raise _no_es_coordinador()
        return responder(200, {"coordinador": nodo.mi_id})

    @app.post("/cluster/asignar")
    async def asignar(request: Request):
        """Asigna un nodo para ejecutar una tarea entrante según la heurística Least-Loaded."""
        mensaje = await _leer_mensaje(request, _PedidoAsignacion)
        if not nodo.bully.soy_coordinador():
            raise _no_es_coordinador()
        elegido = nodo.asignar(mensaje.excluir)
        if elegido is None:
            raise ErrorApi(TipoError.CLUSTER_NO_DISPONIBLE, "no hay nodos vivos para la tarea")
        return responder(200, {"nodo": elegido})

    @app.post("/cluster/ejecutar")
    async def ejecutar(request: Request):
        """Endpoint worker para ejecutar una tarea delegada por otro nodo del cluster.
        
        Aplica las mismas validaciones de seguridad e inicia el contenedor efímero en Docker.
        """
        solicitud, imagen = await _solicitud(request)
        log.info("tarea | ejecuta en el nodo %d | %s | calculo=%s", nodo.mi_id, imagen.referencia,
                 solicitud.calculo)
        ejecucion = await run_in_threadpool(nodo.ejecutar_local, imagen, solicitud.calculo,
                                            solicitud.parametros, solicitud.datos)
        return responder(200, {**ejecucion._asdict(), "nodo": nodo.mi_id})

    return app

