"""Métricas del Sistema y Endpoint de Telemetría `/stats` (Requisito R4).

Mantiene en memoria RAM contadores y muestras estadísticas sin estado persistente:
se reinician al reiniciar el proceso del servidor.

Distinciones Contractuales Clave:
1. `pedidos_por_codigo`:
   Registra absolutamente TODAS las solicitudes HTTP recibidas por el endpoint
   `/getRemoteTask`, desglosadas por su código de estado (200, 400, 401, 403, 413, 415, 422, 429, 500, etc.).
   Incluye las rechazadas tempranamente antes de levantar ningún contenedor.
2. `tareas_ejecutadas` vs `tareas_fallidas`:
   Solo aplican a pedidos que SUPERARON la validación de entrada y pasaron a ejecutarse en un contenedor:
   - "ejecutada": el contenedor devolvió 200 con un resultado válido.
   - "fallida": el contenedor crasheó, timeout, rechazó los parámetros (422) o no se pudo descargar la imagen.
3. Ventana Móvil de Latencia (`deque(maxlen=1000)`):
   Almacena las últimas 1.000 muestras para calcular:
   - Promedio: media aritmética de los tiempos totales.
   - Percentil 95 (P95): el umbral por debajo del cual cayó el 95% de las tareas,
     representando la latencia en el peor de los casos (excluyendo outliers extremos).
4. Desacoplamiento para Sistemas Distribuidos:
   `exportar()` serializa el estado local y `resumir()` combina múltiples exportaciones,
   diseñado para que Hit 3 agregue los contadores de sus 3 nodos en una sola respuesta global.
"""

import math
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Any, Dict, List


def resumir(exportaciones: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Agrega las estadísticas de uno o más nodos en el formato unificado de `/stats`.

    Parámetros:
        exportaciones: Lista de diccionarios generados por el método `exportar()` de cada nodo.

    Retorna:
        Diccionario listo para serializar en el sobre de respuesta contractual.
    """
    por_imagen: Dict[str, Dict[str, int]] = {}
    por_codigo: Counter = Counter()
    latencias: List[float] = []
    ejecutadas = fallidas = 0

    # Agregación acumulativa de contadores de todos los nodos
    for e in exportaciones:
        ejecutadas += e["ejecutadas"]
        fallidas += e["fallidas"]

        # Agregación por cada imagen Docker ejecutada
        for imagen, contadores in e["por_imagen"].items():
            total = por_imagen.setdefault(imagen, {"ejecutadas": 0, "fallidas": 0})
            for clave, n in contadores.items():
                total[clave] += n

        # Suma de contadores por código HTTP
        por_codigo.update(e["por_codigo"])
        # Fusión de las muestras de latencia
        latencias.extend(e["latencias"])

    # Ordenamiento de latencias para el cálculo exacto de percentiles
    latencias.sort()
    n_muestras = len(latencias)

    # Cálculo del Percentil 95: índice = ceil(0.95 * N) - 1
    idx_p95 = math.ceil(0.95 * n_muestras) - 1 if n_muestras > 0 else None
    val_p95 = round(latencias[idx_p95], 3) if idx_p95 is not None else None
    promedio = round(sum(latencias) / n_muestras, 3) if n_muestras > 0 else None

    return {
        "tareas_ejecutadas": ejecutadas,
        "tareas_fallidas": fallidas,
        "tareas_por_imagen": dict(sorted(por_imagen.items())),
        "latencia_s": {
            "promedio": promedio,
            "p95": val_p95,
            "muestras": n_muestras,
        },
        "pedidos_por_codigo": dict(sorted(por_codigo.items())),
    }


class Estadisticas:
    """Recolector concurrente y seguro de métricas en memoria."""

    def __init__(self, ventana: int = 1000):
        """Inicializa los contadores y la ventana móvil de latencias protegidos por un Lock."""
        self._candado = threading.Lock()
        self._ejecutadas = 0
        self._fallidas = 0
        self._por_imagen: Dict[str, Dict[str, int]] = {}
        # deque con tamaño fijo: al llegar a 1000, descarta automáticamente las más antiguas
        self._latencias: deque[float] = deque(maxlen=ventana)
        self._por_codigo: Counter = Counter()
        # Marca de tiempo UTC del inicio del servidor
        self._desde = datetime.now(timezone.utc)
        # Reloj monótono para calcular el uptime sin verse afectado por cambios de hora del sistema
        self._inicio = time.monotonic()

    def registrar_tarea(self, imagen: str, ok: bool, latencia_s: float) -> None:
        """Registra la finalización de una tarea que efectivamente ejecutó en un contenedor."""
        with self._candado:
            if ok:
                self._ejecutadas += 1
            else:
                self._fallidas += 1

            # Desglose por imagen Docker
            por_imagen = self._por_imagen.setdefault(imagen, {"ejecutadas": 0, "fallidas": 0})
            por_imagen["ejecutadas" if ok else "fallidas"] += 1

            # Incorporación a la ventana móvil de latencia
            self._latencias.append(latencia_s)

    def registrar_pedido(self, codigo: int) -> None:
        """Registra el código de estado HTTP resultante de un pedido a `/getRemoteTask`."""
        with self._candado:
            self._por_codigo[str(codigo)] += 1

    def exportar(self) -> Dict[str, Any]:
        """Extrae una instantánea serializable del estado crudo local.

        Permite enviar el estado del nodo a un coordinador en Hit 3 o generar `/stats` localmente.
        """
        with self._candado:
            return {
                "ejecutadas": self._ejecutadas,
                "fallidas": self._fallidas,
                "por_imagen": {i: dict(c) for i, c in self._por_imagen.items()},
                "latencias": list(self._latencias),
                "por_codigo": dict(self._por_codigo),
            }

    def instantanea(self, pool) -> Dict[str, Any]:
        """Genera el diccionario completo de respuesta para `GET /stats` de un servidor individual.

        Parámetros:
            pool: Objeto que expone el estado de los workers y la cola (`_EnCurso` en Hit 1 o `Pool` en Hit 2).
        """
        return {
            **resumir([self.exportar()]),
            "tareas_en_cola": pool.tareas_encoladas,
            "workers_ocupados": pool.workers_activos,
            "workers_max": pool.workers_max,
            "desde": self._desde.isoformat(timespec="seconds"),
            "uptime_s": round(time.monotonic() - self._inicio),
        }
