"""Pool de Workers y Cola de Tareas con Exclusión Mutua (Hit 2 & Requisito R5).

Principios Fundamentales de Diseño:
1. DESACOPLAMIENTO TEMPORAL: La cola actúa como amortiguador (buffer) entre la
   tasa de llegada de peticiones externas y el ritmo físico de atención de los workers.
   Permite admitir solicitudes sin bloquear sockets ni rechazar prematuramente con 503.
2. ORDENAMIENTO CAUSAL (LAMPORT): Las tareas admitidas se ordenan en una Min-Heap
   según su timestamp lógico de Lamport (`X-Lamport-Clock`), priorizando relaciones
   de causa-efecto por encima del mero orden de llegada a nivel de red.
3. EXCLUSIÓN MUTUA: Un cerrojo (`threading.Lock`) protege las estructuras compartidas
   (la cola y el contador de workers activos), garantizando total hilo-seguridad.
4. CONTROL DE RECURSOS: Se utiliza un `ThreadPoolExecutor` gestionado con un número
   máximo de workers para evitar el agotamiento de threads a nivel de sistema operativo.
"""

import heapq
import itertools
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor


# ------------------------------------------------------------------------------
# 1. Elemento de la Cola: Tarea con Timestamp de Lamport
# ------------------------------------------------------------------------------
class TareaEncolada:
    """Representa una tarea en espera en la cola con su timestamp de Lamport.
    
    Implementa el operador `<` (__lt__) para ser ordenada automáticamente en la Min-Heap.
    """

    def __init__(self, id_tarea: str, lamport_ts: int, orden_llegada: int, ejecutor_fn, futuro: Future, al_iniciar=None, al_terminar=None):
        self.id_tarea = id_tarea
        self.lamport_ts = lamport_ts
        self.orden_llegada = orden_llegada
        self.ejecutor_fn = ejecutor_fn
        self.futuro = futuro
        self.al_iniciar = al_iniciar
        self.al_terminar = al_terminar
        self.encolada_en = time.perf_counter()
        self.en_cola_s = 0.0

    def __lt__(self, otro: "TareaEncolada") -> bool:
        # Criterio 1: Menor timestamp de Lamport se atiende primero (orden causal).
        # Criterio 2: En caso de empate lógico, desempata FIFO por orden de llegada física.
        if self.lamport_ts == otro.lamport_ts:
            return self.orden_llegada < otro.orden_llegada
        return self.lamport_ts < otro.lamport_ts


# ------------------------------------------------------------------------------
# 2. Gestor Concurrente: Pool de Workers y Despachador
# ------------------------------------------------------------------------------
class PoolWorkers:
    """Pool de workers con concurrencia acotada y cola protegida por Mutex."""

    def __init__(self, lanzador, workers_max: int = 4, logger=None, cola_max: int = 0):
        self.lanzador = lanzador
        self.workers_max = max(1, workers_max)
        self.cola_max = max(0, cola_max)
        self.log = logger
        
        # Sincronización y estructuras compartidas
        self._mutex = threading.Lock()
        self._cola: list[TareaEncolada] = []
        self._contador_llegada = itertools.count()
        self._workers_activos = 0
        
        # Ejecutor gestionado de hilos
        self._pool_ejecutor = ThreadPoolExecutor(
            max_workers=self.workers_max,
            thread_name_prefix="hit2-worker"
        )

    @property
    def workers_activos(self) -> int:
        """Cantidad de workers actualmente ejecutando un contenedor."""
        with self._mutex:
            return self._workers_activos

    @property
    def tareas_encoladas(self) -> int:
        """Cantidad de tareas esperando turno en la cola de Lamport."""
        with self._mutex:
            return len(self._cola)

    def posicion_en_cola(self, id_tarea: str) -> int:
        """Devuelve la posición 1-indexed de la tarea en la cola ordenada según Lamport."""
        with self._mutex:
            ordenadas = sorted(self._cola)
            for idx, t in enumerate(ordenadas, start=1):
                if t.id_tarea == id_tarea:
                    return idx
            return 1

    def cerrar(self, esperar: bool = False):
        """Detiene ordenadamente el pool de workers."""
        self._pool_ejecutor.shutdown(wait=esperar)

    def encolar(self, id_tarea: str, ejecutor_fn, lamport_ts: int = 0, al_iniciar=None, al_terminar=None) -> Future:
        """Encola una tarea bajo exclusión mutua de forma no bloqueante y retorna su Future.
        
        Verifica el límite de cola saturada (503 con Retry-After si supera cola_max).
        """
        futuro: Future = Future()
        with self._mutex:
            if self.cola_max > 0 and len(self._cola) >= self.cola_max:
                from app.errores import ErrorApi, TipoError
                raise ErrorApi(
                    TipoError.SERVICIO_SATURADO,
                    f"cola de tareas saturada ({len(self._cola)} en espera, máximo {self.cola_max})",
                    cabeceras={"Retry-After": "30"}
                )
            orden = next(self._contador_llegada)
            tarea = TareaEncolada(
                id_tarea=id_tarea,
                lamport_ts=lamport_ts,
                orden_llegada=orden,
                ejecutor_fn=ejecutor_fn,
                futuro=futuro,
                al_iniciar=al_iniciar,
                al_terminar=al_terminar
            )
            # Inserción en la Min-Heap respetando el orden de Lamport
            heapq.heappush(self._cola, tarea)
            if self.log:
                self.log.info(
                    "pool | encolada %s | ts_lamport=%d | encoladas=%d | activos=%d/%d",
                    tarea.id_tarea, lamport_ts, len(self._cola), self._workers_activos, self.workers_max
                )
            self._intentar_despachar_bajo_lock()

        return futuro

    def ejecutar_tarea(self, imagen, calculo, parametros, datos, lamport_ts: int = 0):
        """Encola la tarea de forma síncrona y bloquea esperando la resolución del Future.
        
        Retorna:
          Tupla: (resultado_del_lanzador, segundos_de_espera_en_cola)
        """
        orden = next(self._contador_llegada)
        futuro = self.encolar(
            id_tarea=f"tarea-{orden}",
            ejecutor_fn=lambda: self.lanzador.ejecutarTareaRemota(imagen, calculo, parametros, datos),
            lamport_ts=lamport_ts
        )
        # Se espera el resultado FUERA de la sección crítica para no bloquear la cola a otros hilos
        return futuro.result()

    def _intentar_despachar_bajo_lock(self):
        """Asigna tareas pendientes a workers libres. DEBE llamarse con self._mutex adquirido."""
        while self._cola and self._workers_activos < self.workers_max:
            tarea = heapq.heappop(self._cola)
            tarea.en_cola_s = round(time.perf_counter() - tarea.encolada_en, 4)
            self._workers_activos += 1
            if self.log:
                self.log.info(
                    "pool | despachando %s | ts_lamport=%d | activos=%d/%d",
                    tarea.id_tarea, tarea.lamport_ts, self._workers_activos, self.workers_max
                )
            self._pool_ejecutor.submit(self._ejecutar_worker, tarea)

    def _ejecutar_worker(self, tarea: TareaEncolada):
        """Ejecuta la tarea en un contenedor efímero y libera el worker al finalizar."""
        if tarea.al_iniciar:
            try:
                tarea.al_iniciar(tarea.id_tarea, tarea.en_cola_s)
            except Exception as e:
                if self.log:
                    self.log.warning("pool | error en callback al_iniciar %s: %s", tarea.id_tarea, e)

        resultado = None
        error = None
        try:
            resultado = tarea.ejecutor_fn()
            tarea.futuro.set_result((resultado, tarea.en_cola_s))
        except Exception as exc:
            error = exc
            tarea.futuro.set_exception(exc)
        finally:
            if tarea.al_terminar:
                try:
                    tarea.al_terminar(tarea.id_tarea, resultado, error, tarea.en_cola_s)
                except Exception as e:
                    if self.log:
                        self.log.warning("pool | error en callback al_terminar %s: %s", tarea.id_tarea, e)

            with self._mutex:
                self._workers_activos -= 1
                if self.log:
                    self.log.info(
                        "pool | finalizada %s | activos=%d/%d | encoladas=%d",
                        tarea.id_tarea, self._workers_activos, self.workers_max, len(self._cola)
                    )
                self._intentar_despachar_bajo_lock()
