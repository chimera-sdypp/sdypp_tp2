"""Los contadores de /stats (R4)."""

from app.estadisticas import Estadisticas


class PoolFalso:
    tareas_encoladas = 2
    workers_activos = 4
    workers_max = 4


def test_vacias():
    stats = Estadisticas().instantanea(PoolFalso())
    assert stats["tareas_ejecutadas"] == stats["tareas_fallidas"] == 0
    assert stats["latencia_s"] == {"promedio": None, "p95": None, "muestras": 0}
    assert (stats["tareas_en_cola"], stats["workers_ocupados"]) == (2, 4)


def test_promedio_y_p95_por_rango():
    estadisticas = Estadisticas()
    for i in range(1, 101):
        estadisticas.registrar_tarea("img:1", True, i / 100)   # 0.01 .. 1.00
    latencia = estadisticas.instantanea(PoolFalso())["latencia_s"]
    assert latencia == {"promedio": 0.505, "p95": 0.95, "muestras": 100}


def test_la_ventana_se_queda_con_las_ultimas():
    estadisticas = Estadisticas(ventana=3)
    for latencia in (100, 1, 2, 3):
        estadisticas.registrar_tarea("img:1", True, latencia)
    stats = estadisticas.instantanea(PoolFalso())
    assert stats["latencia_s"]["muestras"] == 3
    assert stats["latencia_s"]["promedio"] == 2.0
    assert stats["tareas_ejecutadas"] == 4    # los contadores no se acotan


def test_por_imagen_y_por_codigo():
    estadisticas = Estadisticas()
    estadisticas.registrar_tarea("a:1", True, 0.1)
    estadisticas.registrar_tarea("a:1", False, 0.1)
    estadisticas.registrar_tarea("b:2", True, 0.1)
    for codigo in (200, 200, 401):
        estadisticas.registrar_pedido(codigo)
    stats = estadisticas.instantanea(PoolFalso())
    assert stats["tareas_por_imagen"] == {"a:1": {"ejecutadas": 1, "fallidas": 1},
                                          "b:2": {"ejecutadas": 1, "fallidas": 0}}
    assert stats["pedidos_por_codigo"] == {"200": 2, "401": 1}
