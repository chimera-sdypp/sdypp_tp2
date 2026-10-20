"""Módulo de Observabilidad y Métricas para `/stats` (Requisito R4).

Principios Fundamentales de Observabilidad:
1. EXPOSICIÓN RESTRINGIDA (RBAC): El endpoint `/stats` solo es accesible por usuarios
   con rol `admin`. Un usuario común (`rol=usuario`) es rechazado con HTTP 403 ROL_INSUFICIENTE.
2. OBSERVABILIDAD STATELESS Y VOLÁTIL: Las métricas residen exclusivamente en memoria
   del proceso del servidor. No requieren bases de datos externas ni persistencia en disco,
   reiniciándose limpiamente con el ciclo de vida del contenedor.
3. DISCRIMINACIÓN DE ERRORES:
   - `pedidos_por_codigo`: Contabiliza todas las respuestas HTTP (200, 401, 403, 429, etc.).
     Permite auditar intentos de acceso no autorizado y desbordes del limitador de tasa.
   - `tareas_ejecutadas` vs `tareas_fallidas`: Registra el resultado real dentro del contenedor
     de cálculo (ej. si la tarea remota completó el cómputo o lanzó una excepción interna).
4. LATENCIA Y PERCENTILES (P95):
   - Mantiene una ventana deslizante acotada (`collections.deque(maxlen=ventana)`) de tiempos.
   - Calcula tanto la latencia promedio como el Percentil 95 (P95), métrica industrial estándar
     para diagnosticar degradación de servicio bajo contención de cola.
5. HILO-SEGURIDAD: Todas las modificaciones y lecturas están protegidas por un cerrojo
   (`threading.Lock`), permitiendo que múltiples hilos del pool actualicen métricas concurrentemente.
"""

import math
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone


def resumir(exportaciones: list[dict]) -> dict:
    """Agrega y consolida el estado de métricas de uno o varios nodos en la respuesta de `/stats`.
    
    En Hit 2 opera sobre el nodo local. En Hit 3 permite consolidar métricas de todo el cluster.
    """
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
    # Percentil 95: el tiempo por debajo del cual se resolvió el 95% de las tareas observadas
    p95_valor = (
        round(latencias[math.ceil(0.95 * len(latencias)) - 1], 3)
        if latencias else None
    )

    return {
        "tareas_ejecutadas": ejecutadas,
        "tareas_fallidas": fallidas,
        "tareas_por_imagen": dict(sorted(por_imagen.items())),
        "latencia_s": {
            "promedio": round(sum(latencias) / len(latencias), 3) if latencias else None,
            "p95": p95_valor,
            "muestras": len(latencias),
        },
        "pedidos_por_codigo": dict(sorted(por_codigo.items())),
    }


class Estadisticas:
    """Recolector de telemetría y métricas operativas del servidor."""

    def __init__(self, ventana: int = 1000):
        """Inicializa los acumuladores y la ventana móvil de latencias.
        
        Args:
            ventana: Cantidad máxima de muestras de latencia retenidas en el deque.
        """
        self._candado = threading.Lock()
        self._ejecutadas = 0
        self._fallidas = 0
        self._por_imagen = {}
        self._latencias = deque(maxlen=ventana)
        self._por_codigo = Counter()
        self._desde = datetime.now(timezone.utc)
        self._inicio = time.monotonic()

    def registrar_tarea(self, imagen: str, ok: bool, latencia_s: float) -> None:
        """Registra la finalización de una tarea ejecutada por un worker en su contenedor."""
        with self._candado:
            if ok:
                self._ejecutadas += 1
            else:
                self._fallidas += 1
            por_imagen = self._por_imagen.setdefault(imagen, {"ejecutadas": 0, "fallidas": 0})
            por_imagen["ejecutadas" if ok else "fallidas"] += 1
            self._latencias.append(latencia_s)

    def registrar_pedido(self, codigo: int) -> None:
        """Registra el código de estado HTTP devuelto a un cliente (ej. 200, 401, 403, 429)."""
        with self._candado:
            self._por_codigo[str(codigo)] += 1

    def exportar(self) -> dict:
        """Extrae una instantánea serializable del estado crudo interno bajo lock."""
        with self._candado:
            return {
                "ejecutadas": self._ejecutadas,
                "fallidas": self._fallidas,
                "por_imagen": {i: dict(c) for i, c in self._por_imagen.items()},
                "latencias": list(self._latencias),
                "por_codigo": dict(self._por_codigo)
            }

    def instantanea(self, pool) -> dict:
        """Genera el JSON final para el endpoint `/stats` combinando métricas con el estado del pool."""
        return {
            **resumir([self.exportar()]),
            "tareas_en_cola": pool.tareas_encoladas,
            "workers_ocupados": pool.workers_activos,
            "workers_max": pool.workers_max,
            "desde": self._desde.isoformat(timespec="seconds"),
            "uptime_s": round(time.monotonic() - self._inicio)
        }

