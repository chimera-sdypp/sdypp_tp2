r"""Telemetría Operativa y Agregación de Métricas en el Cluster (Hit 3 & Requisito R4).

Métricas en Memoria y Agregación Distribuida:
1. Agregación Multi-Nodo (`resumir`):
   - Cada nodo del cluster contabiliza localmente las tareas que él mismo despachó como gateway.
   - Cuando un administrador solicita `GET /stats`, el nodo receptor consulta en paralelo
     a todos sus pares (`GET /cluster/stats`) y ejecuta `resumir()` sobre las exportaciones.
   - Suma linealmente tareas ejecutadas y fallidas, fusiona los diccionarios por imagen,
     agrega los contadores de códigos HTTP y concatena las muestras de latencia para
     calcular el percentil P95 consolidado de todo el cluster.

2. Percentil P95 y Latencia:
   - Se mantiene una ventana deslizante acotada (`deque(maxlen=1000)`) de las últimas latencias totales.
   - El percentil 95 ($P_{95}$) se calcula ordenando el conjunto combinado de latencias y tomando
     el valor en la posición $\lceil 0.95 \times N \rceil - 1$, garantizando que el 95% de los pedidos
     tuvieron una latencia menor o igual a ese valor.

3. Discriminación de Eventos:
   - `tareas_ejecutadas`: Cómputos que corrieron exitosamente en el contenedor Docker.
   - `tareas_fallidas`: Cómputos que alcanzaron a instanciar el contenedor pero arrojaron error de cálculo o timeout.
   - `pedidos_por_codigo`: Conteo exhaustivo de todas las respuestas HTTP (incluyendo 401, 403, 413, 422 y 429).
"""

import math
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone


def resumir(exportaciones):
    """Consolida las métricas de múltiples nodos del cluster en un resumen único.
    
    Parámetros:
    - exportaciones: Lista de diccionarios retornados por `exportar()` de cada nodo par.
    
    Retorna:
    - Diccionario estructurado con métricas consolidadas conforme al contrato §7.
    """
    por_imagen, por_codigo, latencias = {}, Counter(), []
    ejecutadas = fallidas = 0
    # Iteramos sobre los datos exportados por cada nodo del cluster
    for e in exportaciones:
        ejecutadas += e["ejecutadas"]
        fallidas += e["fallidas"]
        # Fusión acumulativa de conteos por imagen Docker
        for imagen, contadores in e["por_imagen"].items():
            total = por_imagen.setdefault(imagen, {"ejecutadas": 0, "fallidas": 0})
            for clave, n in contadores.items():
                total[clave] += n
        # Agregamos los contadores de códigos de respuesta HTTP
        por_codigo.update(e["por_codigo"])
        # Unificamos las ventanas de latencia
        latencias.extend(e["latencias"])
    
    # Ordenamiento global para cálculo de estadísticos de orden
    latencias.sort()
    return {
        "tareas_ejecutadas": ejecutadas,
        "tareas_fallidas": fallidas,
        "tareas_por_imagen": dict(sorted(por_imagen.items())),
        "latencia_s": {
            "promedio": round(sum(latencias) / len(latencias), 3) if latencias else None,
            # Índice P95: ceil(0.95 * N) - 1 en indexación 0
            "p95": round(latencias[math.ceil(0.95 * len(latencias)) - 1], 3) if latencias else None,
            "muestras": len(latencias),
        },
        "pedidos_por_codigo": dict(sorted(por_codigo.items())),
    }


class Estadisticas:
    """Acumulador en memoria de telemetría y métricas operativas por nodo."""

    def __init__(self, ventana=1000):
        """Inicializa los acumuladores con ventana deslizante protegida por lock."""
        self._candado = threading.Lock()
        self._ejecutadas = 0
        self._fallidas = 0
        self._por_imagen = {}
        # Búfer circular en memoria para evitar consumo ilimitado de RAM bajo ráfagas intensas
        self._latencias = deque(maxlen=ventana)
        self._por_codigo = Counter()
        self._desde = datetime.now(timezone.utc)
        self._inicio = time.monotonic()

    def registrar_tarea(self, imagen, ok, latencia_s):
        """Registra la finalización de una tarea (exitosa o fallida) y su latencia total."""
        with self._candado:
            if ok:
                self._ejecutadas += 1
            else:
                self._fallidas += 1
            por_imagen = self._por_imagen.setdefault(imagen, {"ejecutadas": 0, "fallidas": 0})
            por_imagen["ejecutadas" if ok else "fallidas"] += 1
            self._latencias.append(latencia_s)

    def registrar_pedido(self, codigo):
        """Registra el código de estado HTTP devuelto al cliente."""
        with self._candado:
            self._por_codigo[str(codigo)] += 1

    def exportar(self):
        """Retorna una instantánea serializable de las métricas crudas de este nodo."""
        with self._candado:
            return {"ejecutadas": self._ejecutadas, "fallidas": self._fallidas,
                    "por_imagen": {i: dict(c) for i, c in self._por_imagen.items()},
                    "latencias": list(self._latencias), "por_codigo": dict(self._por_codigo)}

    def instantanea(self, pool):
        """Genera el reporte de /stats para ejecución mononodo."""
        return {**resumir([self.exportar()]),
                "tareas_en_cola": pool.tareas_encoladas,
                "workers_ocupados": pool.workers_activos,
                "workers_max": pool.workers_max,
                "desde": self._desde.isoformat(timespec="seconds"),
                "uptime_s": round(time.monotonic() - self._inicio)}

