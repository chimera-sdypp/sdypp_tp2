"""Módulo Nodo: Orquestador Distribuido Multirrol (Entrada, Worker y Coordinador).

En el Hit 3, la arquitectura distribuida es simétrica y no jerárquica a nivel de software:
todos los nodos ejecutan el mismo código base y pueden asumir dinámicamente tres roles simultáneos:

1. ROL DE ENTRADA (Ingress / Gateway):
   - Nginx balancea las solicitudes externas de los clientes HTTP entre cualquiera de los nodos vivos.
   - El nodo receptor se convierte en el "dueño" (owner) de la petición:
     a) Consulta al coordinador actual a qué nodo worker asignarle la tarea (`POST /cluster/asignar`).
     b) Reenvía la solicitud al worker asignado (`POST /cluster/ejecutar`).
     c) Si el worker colapsa a mitad de ejecución (`_EjecutorCaido`), el nodo de entrada detecta
        la falla, agrega al nodo caído a su lista de exclusión y solicita transparentemente una nueva
        asignación. Esto garantiza tolerancia a fallos sin que el cliente perciba el error.

2. ROL DE WORKER (Ejecutor):
   - Atiende peticiones internas de ejecución (`POST /cluster/ejecutar`) o locales.
   - Incrementa su contador `tareas_en_curso` y levanta un contenedor Docker efímero y aislado (R5).
   - Envía periódicamente latidos (`heartbeats`) al coordinador informando su carga de trabajo activa.

3. ROL DE COORDINADOR (Líder del Cluster):
   - Si el nodo gana la elección de Bully (ID más alto activo), asume el liderazgo:
     a) Mantiene el registro de membresía y salud de todos los workers (`RegistroNodos`).
     b) Balancea la carga asignando tareas mediante la heurística Least-Loaded (mínima carga).
     c) Recolecta métricas distribuidas agregadas para el endpoint `/stats`.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

from app.bully import Bully
from app.cluster import RegistroNodos
from app.errores import ErrorApi, TipoError
from app.lanzador import Ejecucion, SinConexion, Vencido, http_json


class Despacho(NamedTuple):
    """Estructura que encapsula el resultado del despacho de una tarea en el cluster.
    
    Campos:
    - ejecucion: Objeto Ejecucion con el valor devuelto, hash del contenedor y tiempo neto.
    - nodo: ID del nodo worker que ejecutó físicamente el contenedor Docker.
    - asignacion_s: Tiempo total en segundos que demoró obtener la asignación del coordinador.
    """
    ejecucion: Ejecucion
    nodo: int
    asignacion_s: float


class _EjecutorCaido(Exception):
    """Excepción interna lanzada cuando se interrumpe la conexión TCP con el worker asignado."""


class Nodo:
    """Representa una instancia activa de un nodo dentro del cluster distribuido."""

    def __init__(self, config, logger, lanzador, http=http_json, bully=None, registro=None,
                 reloj=time.monotonic, dormir=time.sleep):
        """Inicializa los componentes del nodo con inyección de dependencias para tests."""
        self.mi_id = config.nodo_id
        self._config = config
        self._log = logger
        self._lanzador = lanzador
        self._http = http
        self._reloj = reloj
        self._dormir = dormir
        # Directorio de estado blando de workers (activo principalmente cuando es coordinador)
        self.registro = registro or RegistroNodos(config.vencimiento_nodo)
        # Módulo del algoritmo de elección Bully (García-Molina 1982)
        self.bully = bully or Bully(config, logger, http=http, al_asumir=self._al_asumir,
                                    al_anunciarse=self._al_anunciarse)
        # Cerrojo para proteger el contador de tareas locales en curso
        self._lock = threading.Lock()
        self._en_curso = 0
        # Evento de control para detención limpia (graceful shutdown) del hilo de heartbeat
        self._detener = threading.Event()

    @property
    def tareas_en_curso(self):
        """Cantidad de tareas ejecutándose localmente bajo exclusión mutua."""
        with self._lock:
            return self._en_curso

    # --------------------------------------------------------------------------
    # Ciclo de Vida y Monitoreo (Heartbeats)
    # --------------------------------------------------------------------------
    def iniciar(self):
        """Inicia el nodo: purga contenedores huérfanos y arranca el hilo de latidos/elección."""
        self._lanzador.limpiar_huerfanos()
        # Arrancamos el bucle demonio de latidos y monitoreo
        threading.Thread(target=self._bucle, name="heartbeat", daemon=True).start()

    def detener(self):
        """Señala la detención inmediata del hilo de monitoreo."""
        self._detener.set()

    def _bucle(self):
        """Hilo continuo de mantenimiento del cluster.
        
        Al arrancar, todo nodo convoca inmediatamente a una elección de Bully:
        si su ID es el más alto del cluster, se proclama líder de inmediato.
        Posteriormente, emite latidos periódicos cada `intervalo_heartbeat` segundos.
        """
        self.bully.iniciar_eleccion("arranque")
        while not self._detener.wait(self._config.intervalo_heartbeat):
            try:
                self.latir()
            except Exception:
                # El hilo nunca debe morir: si muriera, el nodo no detectaría caídas del líder
                self._log.exception("cluster | error en el heartbeat")

    def latir(self):
        """Ejecuta una ronda de latido según el rol actual del nodo.
        
        - Si es Coordinador: Actualiza su propio registro local con su carga en curso.
        - Si es Worker: Envía un latido HTTP POST al coordinador informando su carga.
        """
        if self.bully.soy_coordinador():
            self.registro.actualizar(self.mi_id, self.tareas_en_curso)
        else:
            self.bully.latir(self.tareas_en_curso)

    def _al_asumir(self):
        """Callback ejecutado cuando el nodo se proclama legítimo coordinador."""
        self.registro.asumir_coordinador(self.mi_id, self.tareas_en_curso)

    def _al_anunciarse(self, aceptaron):
        """Optimización de convergencia: registra de inmediato a los nodos que respondieron al COORDINADOR.
        
        Evita que durante el primer segundo posterior a una elección el nuevo líder crea
        que está solo en el cluster y se asigne todas las tareas entrantes a sí mismo.
        """
        for nodo in aceptaron:
            self.registro.conocer(nodo)

    def estadisticas_de_los_pares(self):
        """Consulta en paralelo el endpoint `/cluster/stats` de todos los pares del cluster.
        
        Retorna:
          Tupla: ({id_nodo: metricas_dict}, [ids_nodos_que_no_respondieron])
        """
        def consultar(par):
            nodo, url = par
            try:
                estado, respuesta = self._http("GET", url + "/cluster/stats", timeout=self._config.timeout_mensaje)
            except (SinConexion, Vencido):
                return nodo, None
            contenido = respuesta.get("contenido") if isinstance(respuesta, dict) else None
            return nodo, contenido if estado == 200 and isinstance(contenido, dict) else None

        pares = self._config.pares
        if not pares:
            return {}, []
        # Consulta concurrente mediante ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(pares)) as ejecutor:
            resultados = list(ejecutor.map(consultar, pares))
        return ({n: c for n, c in resultados if c is not None}, [n for n, c in resultados if c is None])

    # --------------------------------------------------------------------------
    # ROL DE COORDINADOR: Asignación y Heartbeats
    # --------------------------------------------------------------------------
    def recibir_heartbeat(self, de, tareas_en_curso):
        """Recibe y procesa el latido emitido por un worker."""
        if not self.bully.soy_coordinador():
            return False
        self.registro.actualizar(de, tareas_en_curso)
        return True

    def asignar(self, excluir=()):
        """Determina cuál nodo worker debe procesar la tarea entrante según menor carga."""
        nodo = self.registro.elegir(excluir=set(excluir))
        self._log.info("cluster | coordinador %d asigna la tarea al nodo %s (excluidos: %s)",
                       self.mi_id, nodo, sorted(excluir) or "ninguno")
        return nodo

    # --------------------------------------------------------------------------
    # ROL DE WORKER: Ejecución Local en Contenedor Docker Aislado
    # --------------------------------------------------------------------------
    def ejecutar_local(self, imagen, calculo, parametros, datos):
        """Ejecuta la tarea en el host local utilizando el lanzador Docker.
        
        Mantiene el contador atómico `_en_curso` para reportar métricas precisas.
        """
        with self._lock:
            self._en_curso += 1
        try:
            return self._lanzador.ejecutarTareaRemota(imagen, calculo, parametros, datos)
        finally:
            with self._lock:
                self._en_curso -= 1


    # --------------------------------------------------------------------------
    # ROL DE ENTRADA (Gateway): Orquestación y Tolerancia a Fallos
    # --------------------------------------------------------------------------
    def ejecutar(self, imagen, calculo, parametros, datos):
        """Despacha la tarea remota solicitada por el cliente hacia el cluster.
        
        Mecanismo de Resiliencia y Tolerancia a Caídas:
        1. Consulta al líder del cluster qué nodo debe procesar la tarea.
        2. Si el nodo asignado es este mismo nodo, la ejecuta localmente en Docker.
        3. Si el nodo asignado es un par remoto, le retransmite la petición vía HTTP interno.
        4. Si el worker remoto asignado colapsa a mitad de camino (`_EjecutorCaido`):
           - Se captura la falla sin propagar error al cliente.
           - Se agrega el nodo caído al conjunto `excluidos`.
           - Se vuelve a solicitar otra asignación al coordinador excluyendo al caído.
           - Como las tareas son puramente matemáticas (idempotentes y sin efectos colaterales),
             pueden re-ejecutarse con total seguridad.
        """
        excluidos = set()
        asignacion_s = 0.0
        while True:
            inicio = time.perf_counter()
            # Paso 1: Obtener un nodo asignado por el coordinador
            nodo = self._pedir_asignacion(excluidos)
            asignacion_s += time.perf_counter() - inicio
            
            # Caso A: El coordinador nos asignó la tarea a nosotros mismos
            if nodo == self.mi_id:
                return Despacho(self.ejecutar_local(imagen, calculo, parametros, datos), nodo, round(asignacion_s, 4))
            
            # Caso B: El coordinador asignó un nodo par remoto
            try:
                return Despacho(self._ejecutar_en(nodo, imagen, calculo, parametros, datos), nodo,
                                round(asignacion_s, 4))
            except _EjecutorCaido as error:
                # Tolerancia a Fallas: El worker colapsó mientras ejecutaba el contenedor
                self._log.warning("cluster | se cayó el nodo %d con la tarea en curso (%s): se reasigna",
                                  nodo, error)
                # Lo excluimos de futuras asignaciones para esta petición y reintentamos en el bucle
                excluidos.add(nodo)

    def _pedir_asignacion(self, excluidos):
        """Solicita una asignación de nodo al coordinador activo.
        
        Si el coordinador no responde (timeout o socket caído):
        1. Dispara de inmediato una elección de Bully (`iniciar_eleccion`).
        2. Espera a que el nuevo coordinador se proclame mediante `esperar_coordinador`.
        3. Reintenta la solicitud contra el nuevo líder hasta agotar `timeout_asignacion`.
        """
        limite = self._reloj() + self._config.timeout_asignacion
        while (restante := limite - self._reloj()) > 0:
            # Esperamos a que haya un coordinador conocido y estable
            coordinador = self.bully.esperar_coordinador(timeout=restante)
            if coordinador is None:
                break
            
            # Si este nodo es el coordinador, asigna directamente en memoria
            if coordinador == self.mi_id:
                nodo = self.asignar(excluidos)
                if nodo is None:
                    break
                return nodo
            
            # Si el coordinador es otro nodo, le consultamos por HTTP
            try:
                estado, respuesta = self._http("POST", self.bully.url(coordinador) + "/cluster/asignar",
                                               {"de": self.mi_id, "excluir": sorted(excluidos)},
                                               timeout=self._config.timeout_mensaje)
            except (SinConexion, Vencido) as error:
                # El coordinador cayó mientras le pedíamos asignación: iniciamos elección
                self._log.warning("cluster | el coordinador %d no responde al pedir asignación (%s)",
                                  coordinador, error)
                self.bully.iniciar_eleccion(f"el coordinador {coordinador} no asigna", si_sigue=coordinador)
                continue

            contenido = respuesta.get("contenido") if isinstance(respuesta, dict) else None
            # Caso Exitoso: El coordinador devolvió HTTP 200 con el ID del nodo asignado
            if estado == 200 and isinstance(contenido, dict) and isinstance(contenido.get("nodo"), int):
                return contenido["nodo"]
            
            # Caso 503: Hay coordinador activo, pero no hay ningún nodo vivo disponible
            if estado == 503:
                break
            
            # Caso 409: El nodo al que consultamos ya no es coordinador (hubo una re-elección reciente)
            self._dormir(0.1)
        
        # Si se agotó el tiempo límite total sin conseguir asignación
        raise ErrorApi(TipoError.CLUSTER_NO_DISPONIBLE,
                       "no hay coordinador ni nodos disponibles para ejecutar la tarea")

    def _ejecutar_en(self, nodo, imagen, calculo, parametros, datos):
        """Envía la tarea al nodo worker asignado mediante la API interna del cluster.
        
        Configura un timeout extendido considerando el tiempo de arranque de Docker,
        el tiempo de cómputo y un margen adicional de descarga de imagen.
        """
        cuerpo = {"calculo": calculo, "parametros": parametros, "datos": datos, "imagen": imagen.referencia}
        timeout = self._config.timeout_arranque + self._config.timeout_ejecucion + 60
        try:
            # Petición HTTP interna hacia /cluster/ejecutar en el nodo worker
            estado, respuesta = self._http("POST", self.bully.url(nodo) + "/cluster/ejecutar", cuerpo,
                                           timeout=timeout)
        except SinConexion as error:
            # Fallo de transporte: el worker se cayó durante la ejecución
            raise _EjecutorCaido(str(error)) from None
        except Vencido:
            # El worker no respondió en el tiempo límite asignado
            raise ErrorApi(TipoError.TAREA_SIN_RESPUESTA, "la tarea no respondió a tiempo",
                           interno=f"el nodo {nodo} no contestó en {timeout:g} s") from None

        # El worker remoto responde con el mismo sobre estándar que el servidor
        contenido = respuesta.get("contenido") if isinstance(respuesta, dict) else None
        
        # Caso de Éxito: HTTP 200 con resultado
        if estado == 200 and isinstance(contenido, dict) and "resultado" in contenido:
            return Ejecucion(contenido["resultado"], contenido.get("contenedor"), contenido.get("imagen_id"),
                             contenido.get("digest"), contenido.get("ejecucion_s"))
        
        # Caso de Error de Negocio propagado desde el worker
        error = contenido.get("error") if isinstance(contenido, dict) else None
        if isinstance(error, dict) and error.get("tipo") in TipoError.__members__:
            raise ErrorApi(TipoError[error["tipo"]], error.get("mensaje") or "", error.get("detalles"),
                           interno=f"desde el nodo {nodo}")
        
        # Error genérico inesperado
        raise ErrorApi(TipoError.ERROR_INTERNO, "error interno del servidor",
                       interno=f"el nodo {nodo} contestó HTTP {estado}: {str(respuesta)[:300]}")

