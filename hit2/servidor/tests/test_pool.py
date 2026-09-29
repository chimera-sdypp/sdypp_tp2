"""Tests unitarios para el Pool de Workers y la Cola con Exclusión Mutua."""

import time
import threading
from unittest.mock import MagicMock

from app.pool import PoolWorkers


def test_pool_limite_workers():
    lanzador_mock = MagicMock()
    # Simular una ejecución que demora 0.1s
    def _ejecutar(imagen, calculo, parametros, datos):
        time.sleep(0.1)
        return {"resultado": 42}

    lanzador_mock.ejecutarTareaRemota.side_effect = _ejecutar

    # Pool con máximo 2 workers
    pool = PoolWorkers(lanzador_mock, workers_max=2)

    resultados = []
    def _tarea(ts):
        res = pool.ejecutar_tarea("imagen:1.0", "suma", {}, {}, lamport_ts=ts)
        resultados.append(res)

    # Disparar 5 tareas simultáneas
    hilos = [threading.Thread(target=_tarea, args=(i,)) for i in range(5)]
    for h in hilos:
        h.start()

    # Dar un pequeño tiempo para verificar que nunca hay más de 2 workers activos
    time.sleep(0.05)
    assert pool.workers_activos <= 2

    for h in hilos:
        h.join()

    assert len(resultados) == 5
    assert pool.workers_activos == 0
    assert pool.tareas_encoladas == 0


def test_pool_ordenamiento_lamport():
    lanzador_mock = MagicMock()
    orden_ejecucion = []
    lock = threading.Lock()

    def _ejecutar(imagen, calculo, parametros, datos):
        time.sleep(0.05)
        with lock:
            orden_ejecucion.append(calculo)
        return {"ok": True}

    lanzador_mock.ejecutarTareaRemota.side_effect = _ejecutar

    # Pool de 1 solo worker para forzar encolamiento y verificar orden estricto por Lamport
    pool = PoolWorkers(lanzador_mock, workers_max=1)

    # Ocupamos el único worker con una tarea inicial
    t0 = threading.Thread(target=pool.ejecutar_tarea, args=("img", "inicial", {}, {}, 10))
    t0.start()
    time.sleep(0.01)  # Asegurar que 'inicial' toma el worker

    # Encolar tareas con distintos timestamps de Lamport
    # 'tarea_alta_prioridad' tiene ts=2, 'tarea_baja_prioridad' tiene ts=50
    t1 = threading.Thread(target=pool.ejecutar_tarea, args=("img", "baja_prio", {}, {}, 50))
    t2 = threading.Thread(target=pool.ejecutar_tarea, args=("img", "alta_prio", {}, {}, 2))

    t1.start()
    t2.start()

    t0.join()
    t1.join()
    t2.join()

    # La primera fue 'inicial', luego debió ejecutarse 'alta_prio' (ts=2) antes que 'baja_prio' (ts=50)
    assert orden_ejecucion == ["inicial", "alta_prio", "baja_prio"]
