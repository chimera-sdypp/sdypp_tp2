"""Registro de Estado de Nodos y Balanceo de Carga Distribuido (Hit 3).

Principios Arquitectónicos:
1. ESTADO BLANDO (Soft State):
   - El coordinador no persiste el registro de membresía en disco ni requiere replicación pesada (tipo Raft o Paxos).
   - El estado del cluster se reconstruye orgánicamente en cuestión de segundos a partir de los
     heartbeats periódicos emitidos por los nodos worker.
   - Si el coordinador colapsa y asume uno nuevo, el nuevo líder arranca con un registro limpio
     y converge rápidamente al estado real a medida que recibe los latidos de la red.

2. BALANCEO POR MÍNIMA CARGA (Least-Connections / Least-Loaded):
   - Al despachar una tarea, el coordinador selecciona el nodo vivo con menor cantidad de
     `tareas_en_curso`. En caso de empate en la carga mínima, desempata aleatoriamente de forma uniforme.
   - Contabilidad Optimista: Incrementa en memoria `tareas_en_curso += 1` en el momento exacto
     de la asignación. Esto previene que una ráfaga de peticiones concurrentes sea enviada
     toda al mismo nodo antes de que el siguiente heartbeat actualice el contador real.
"""

import random
import threading
import time
from dataclasses import dataclass


@dataclass
class EstadoNodo:
    """Metadatos de salud y carga operativa de un nodo del cluster.
    
    Campos:
    - tareas_en_curso: Número de tareas que el nodo está ejecutando activamente.
    - ultimo_heartbeat: Marca de tiempo monotónica del último latido recibido.
    """
    tareas_en_curso: int
    ultimo_heartbeat: float


class RegistroNodos:
    """Directorio de membresía y despachador de carga del coordinador del cluster."""

    def __init__(self, vencimiento, reloj=time.monotonic, azar=random.choice):
        """Inicializa el registro de nodos con inyección de dependencias para tests determinísticos."""
        self._vencimiento = vencimiento
        self._reloj = reloj
        self._azar = azar
        # Cerrojo de exclusión mutua para proteger el diccionario de nodos concurrentemente
        self._lock = threading.Lock()
        self._nodos = {}

    def reiniciar(self):
        """Limpia todo el estado blando al iniciar una nueva época (asunción de nuevo coordinador)."""
        with self._lock:
            self._nodos.clear()

    def asumir_coordinador(self, nodo, tareas_en_curso):
        """Transición atómica a coordinador sin ventanas de inconsistencia temporal.

        El reinicio del registro y la inserción del propio nodo líder ocurren bajo la misma
        sección crítica, evitando que heartbeats concurrentes de otros workers queden
        desfasados con estados de épocas anteriores.
        """
        with self._lock:
            self._nodos.clear()
            self._nodos[nodo] = EstadoNodo(tareas_en_curso, self._reloj())

    def actualizar(self, nodo, tareas_en_curso):
        """Registra o actualiza el estado de un nodo worker tras recibir un heartbeat."""
        with self._lock:
            self._nodos[nodo] = EstadoNodo(tareas_en_curso, self._reloj())

    def conocer(self, nodo):
        """Registra preliminarmente un nodo como vivo con 0 tareas si aún no emitió su primer heartbeat."""
        with self._lock:
            self._nodos.setdefault(nodo, EstadoNodo(0, self._reloj()))

    def _vivo(self, estado, ahora):
        """Verifica si el último heartbeat del nodo ocurrió dentro de la ventana de tolerancia."""
        return ahora - estado.ultimo_heartbeat <= self._vencimiento

    def elegir(self, excluir=()):
        """Selecciona el mejor nodo worker disponible para despachar una tarea.
        
        Criterios:
        1. Filtra nodos vivos cuyo último latido no haya expirado y no estén en la lista de exclusión.
        2. Determina el valor mínimo de tareas en curso.
        3. Si hay varios nodos empatados en la carga mínima, elige uno al azar de manera uniforme.
        4. Incrementa inmediatamente el contador de tareas del elegido (contabilidad optimista),
           garantizando una distribución equitativa de ráfagas concurrentes.
        """
        with self._lock:
            ahora = self._reloj()
            # Filtramos los nodos candidatos que siguen activos y no están excluidos
            candidatos = {n: e for n, e in self._nodos.items() if n not in excluir and self._vivo(e, ahora)}
            if not candidatos:
                return None
            
            # Encontramos la menor carga operativa entre los candidatos
            minimo = min(e.tareas_en_curso for e in candidatos.values())
            # Desempatamos aleatoriamente entre los nodos con carga mínima
            elegido = self._azar(sorted(n for n, e in candidatos.items() if e.tareas_en_curso == minimo))
            # Contabilidad optimista: sumamos 1 para no saturar al mismo nodo en la siguiente petición
            candidatos[elegido].tareas_en_curso += 1
            return elegido

    def estado(self):
        """Genera un reporte estructurado del estado de todos los nodos para el endpoint `/health`."""
        with self._lock:
            ahora = self._reloj()
            return {str(n): {"estado": "vivo" if self._vivo(e, ahora) else "caido",
                             "tareas_en_curso": e.tareas_en_curso,
                             "ultimo_heartbeat_hace": round(ahora - e.ultimo_heartbeat, 2)}
                    for n, e in sorted(self._nodos.items())}

