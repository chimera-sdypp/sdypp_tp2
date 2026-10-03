"""Registro de estado de los nodos: lo mantiene el coordinador.

Es estado *blando*: se arma sólo con los heartbeats. Un coordinador nuevo arranca
con el registro vacío y en uno o dos heartbeats ya lo tiene completo, así que no
hace falta replicarlo ni recuperarlo del coordinador caído.
"""

import random
import threading
import time
from dataclasses import dataclass


@dataclass
class EstadoNodo:
    tareas_en_curso: int
    ultimo_heartbeat: float


class RegistroNodos:
    def __init__(self, vencimiento, reloj=time.monotonic, azar=random.choice):
        self._vencimiento = vencimiento
        self._reloj = reloj
        self._azar = azar
        self._lock = threading.Lock()
        self._nodos = {}

    def reiniciar(self):
        """Al asumir como coordinador: lo que hubiera es de otra época."""
        with self._lock:
            self._nodos.clear()

    def actualizar(self, nodo, tareas_en_curso):
        """Un heartbeat: el nodo está vivo y tiene `tareas_en_curso` tareas."""
        with self._lock:
            self._nodos[nodo] = EstadoNodo(tareas_en_curso, self._reloj())

    def conocer(self, nodo):
        """Lo anota como vivo y sin tareas, si todavía no mandó heartbeat."""
        with self._lock:
            self._nodos.setdefault(nodo, EstadoNodo(0, self._reloj()))

    def _vivo(self, estado, ahora):
        return ahora - estado.ultimo_heartbeat <= self._vencimiento

    def elegir(self, excluir=()):
        """El nodo vivo con menos tareas en curso (empate: al azar), o None si no hay.

        Le suma la tarea al elegido en el momento: si no, todas las tareas que
        llegan entre dos heartbeats irían al mismo nodo. El heartbeat siguiente
        trae el número real y lo corrige.
        """
        with self._lock:
            ahora = self._reloj()
            candidatos = {n: e for n, e in self._nodos.items() if n not in excluir and self._vivo(e, ahora)}
            if not candidatos:
                return None
            minimo = min(e.tareas_en_curso for e in candidatos.values())
            elegido = self._azar(sorted(n for n, e in candidatos.items() if e.tareas_en_curso == minimo))
            candidatos[elegido].tareas_en_curso += 1
            return elegido

    def estado(self):
        """Para /health: {id: {estado, tareas_en_curso, ultimo_heartbeat_hace}}."""
        with self._lock:
            ahora = self._reloj()
            return {str(n): {"estado": "vivo" if self._vivo(e, ahora) else "caido",
                             "tareas_en_curso": e.tareas_en_curso,
                             "ultimo_heartbeat_hace": round(ahora - e.ultimo_heartbeat, 2)}
                    for n, e in sorted(self._nodos.items())}
