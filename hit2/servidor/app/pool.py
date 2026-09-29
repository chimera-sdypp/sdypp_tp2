"""Pool de Workers y Cola de Tareas con Exclusión Mutua.

Gestiona la concurrencia de tareas asignadas a contenedores Docker.
Garantiza exclusión mutua al acceder a la cola compartida y ordena
las tareas según su timestamp de Lamport.
"""

import heapq
import itertools
import threading
from concurrent.futures import Future


class TareaEncolada:
    """Representa una tarea en espera en la cola con su timestamp de Lamport."""

    def __init__(self, id_tarea: str, lamport_ts: int, orden_llegada: int, ejecutor_fn, futuro: Future):
        self.id_tarea = id_tarea
        self.lamport_ts = lamport_ts
        self.orden_llegada = orden_llegada
        self.ejecutor_fn = ejecutor_fn
        self.futuro = futuro

    def __lt__(self, otro: "TareaEncolada") -> bool:
        # Menor timestamp de Lamport se atiende primero.
        # En caso de empate en timestamp, se respeta FIFO (orden de llegada).
        if self.lamport_ts == otro.lamport_ts:
            return self.orden_llegada < otro.orden_llegada
        return self.lamport_ts < otro.lamport_ts


class PoolWorkers:
    """Pool de workers con límite configurable y cola protegida por mutex."""

    def __init__(self, lanzador, workers_max: int = 4, logger=None):
        self.lanzador = lanzador
        self.workers_max = max(1, workers_max)
        self.log = logger
        self._mutex = threading.Lock()
        self._cola: list[TareaEncolada] = []
        self._contador_llegada = itertools.count()
        self._workers_activos = 0

    @property
    def workers_activos(self) -> int:
        with self._mutex:
            return self._workers_activos

    @property
    def tareas_encoladas(self) -> int:
        with self._mutex:
            return len(self._cola)

    def ejecutar_tarea(self, imagen, calculo, parametros, datos, lamport_ts: int = 0):
        """Encola la tarea bajo exclusión mutua y espera a que un worker disponible la ejecute."""
        futuro = Future()
        with self._mutex:
            orden = next(self._contador_llegada)
            tarea = TareaEncolada(
                id_tarea=f"tarea-{orden}",
                lamport_ts=lamport_ts,
                orden_llegada=orden,
                ejecutor_fn=lambda: self.lanzador.ejecutarTareaRemota(imagen, calculo, parametros, datos),
                futuro=futuro
            )
            heapq.heappush(self._cola, tarea)
            if self.log:
                self.log.info("pool | encolada %s | ts_lamport=%d | encoladas=%d | activos=%d/%d",
                              tarea.id_tarea, lamport_ts, len(self._cola), self._workers_activos, self.workers_max)
            self._intentar_despachar_bajo_lock()

        # Se espera el resultado fuera de la sección crítica para no bloquear la cola
        return futuro.result()

    def _intentar_despachar_bajo_lock(self):
        """Asigna tareas a workers libres. Debe invocarse con self._mutex adquirido."""
        while self._cola and self._workers_activos < self.workers_max:
            tarea = heapq.heappop(self._cola)
            self._workers_activos += 1
            if self.log:
                self.log.info("pool | despachando %s | ts_lamport=%d | activos=%d/%d",
                              tarea.id_tarea, tarea.lamport_ts, self._workers_activos, self.workers_max)
            threading.Thread(target=self._ejecutar_worker, args=(tarea,), daemon=True).start()

    def _ejecutar_worker(self, tarea: TareaEncolada):
        """Ejecuta la tarea en un contenedor Docker y libera el worker al finalizar."""
        try:
            resultado = tarea.ejecutor_fn()
            tarea.futuro.set_result(resultado)
        except Exception as exc:
            tarea.futuro.set_exception(exc)
        finally:
            with self._mutex:
                self._workers_activos -= 1
                if self.log:
                    self.log.info("pool | finalizada %s | activos=%d/%d | encoladas=%d",
                                  tarea.id_tarea, self._workers_activos, self.workers_max, len(self._cola))
                self._intentar_despachar_bajo_lock()
