"""Contadores de `/stats` (R4), en memoria y sin sesión: se reinician con el servidor.

Una tarea "ejecutada" es la que terminó bien en su contenedor; "fallida", la que llegó a
ejecutarse y terminó mal (la tarea rechazó los parámetros, falló, no contestó, no se pudo
bajar la imagen...). Los pedidos que se rechazan antes de ejecutar (401, 403, 413, 422 de
validación...) no son tareas: están en `pedidos_por_codigo`. La latencia es el tiempo
total de la tarea, cola incluida, sobre las últimas `ventana` tareas.

`exportar()` da el estado crudo y `resumir()` lo convierte en la respuesta: el Hit 3 junta
el estado de sus tres nodos y lo resume una sola vez.
"""

import math
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone


def resumir(exportaciones):
    """Junta el estado de uno o más nodos en los números de `/stats`."""
    por_imagen, por_codigo, latencias = {}, Counter(), []
    ejecutadas = fallidas = 0
    for e in exportaciones:
        ejecutadas += e["ejecutadas"]
        fallidas += e["fallidas"]
        for imagen, contadores in e["por_imagen"].items():
            total = por_imagen.setdefault(imagen, {"ejecutadas": 0, "fallidas": 0})
            for clave, n in contadores.items():
                total[clave] += n
        por_codigo.update(e["por_codigo"])
        latencias.extend(e["latencias"])
    latencias.sort()
    return {
        "tareas_ejecutadas": ejecutadas,
        "tareas_fallidas": fallidas,
        "tareas_por_imagen": dict(sorted(por_imagen.items())),
        "latencia_s": {
            "promedio": round(sum(latencias) / len(latencias), 3) if latencias else None,
            "p95": round(latencias[math.ceil(0.95 * len(latencias)) - 1], 3) if latencias else None,
            "muestras": len(latencias),
        },
        "pedidos_por_codigo": dict(sorted(por_codigo.items())),
    }


class Estadisticas:
    def __init__(self, ventana=1000):
        self._candado = threading.Lock()
        self._ejecutadas = 0
        self._fallidas = 0
        self._por_imagen = {}
        self._latencias = deque(maxlen=ventana)
        self._por_codigo = Counter()
        self._desde = datetime.now(timezone.utc)
        self._inicio = time.monotonic()

    def registrar_tarea(self, imagen, ok, latencia_s):
        with self._candado:
            if ok:
                self._ejecutadas += 1
            else:
                self._fallidas += 1
            por_imagen = self._por_imagen.setdefault(imagen, {"ejecutadas": 0, "fallidas": 0})
            por_imagen["ejecutadas" if ok else "fallidas"] += 1
            self._latencias.append(latencia_s)

    def registrar_pedido(self, codigo):
        with self._candado:
            self._por_codigo[str(codigo)] += 1

    def exportar(self):
        """El estado crudo, serializable: lo que un nodo le manda al que arma `/stats`."""
        with self._candado:
            return {"ejecutadas": self._ejecutadas, "fallidas": self._fallidas,
                    "por_imagen": {i: dict(c) for i, c in self._por_imagen.items()},
                    "latencias": list(self._latencias), "por_codigo": dict(self._por_codigo)}

    def instantanea(self, pool):
        """`/stats` de un servidor solo; `pool` aporta cola y workers."""
        return {**resumir([self.exportar()]),
                "tareas_en_cola": pool.tareas_encoladas,
                "workers_ocupados": pool.workers_activos,
                "workers_max": pool.workers_max,
                "desde": self._desde.isoformat(timespec="seconds"),
                "uptime_s": round(time.monotonic() - self._inicio)}
