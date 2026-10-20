from app.cluster import RegistroNodos
from dobles import RelojFalso


def _registro(azar=min):
    reloj = RelojFalso()
    return RegistroNodos(vencimiento=3.0, reloj=reloj, azar=azar), reloj


def test_elige_el_vivo_con_menos_tareas():
    registro, _ = _registro()
    registro.actualizar(1, 2)
    registro.actualizar(2, 0)
    registro.actualizar(3, 1)
    assert registro.elegir() == 2


def test_cuenta_la_tarea_asignada_sin_esperar_al_heartbeat():
    registro, _ = _registro()
    registro.actualizar(1, 0)
    registro.actualizar(2, 0)
    # Dos tareas seguidas no van al mismo nodo.
    assert {registro.elegir(), registro.elegir()} == {1, 2}
    registro.actualizar(1, 0)  # el heartbeat trae el número real
    assert registro.estado()["1"]["tareas_en_curso"] == 0


def test_empate_se_resuelve_con_el_azar():
    registro, _ = _registro(azar=max)
    registro.actualizar(1, 0)
    registro.actualizar(3, 0)
    assert registro.elegir() == 3


def test_no_elige_los_excluidos():
    registro, _ = _registro()
    registro.actualizar(1, 0)
    registro.actualizar(2, 5)
    assert registro.elegir(excluir={1}) == 2


def test_un_nodo_sin_heartbeat_se_da_por_caido():
    registro, reloj = _registro()
    registro.actualizar(1, 0)
    reloj.dormir(2)
    registro.actualizar(2, 9)
    reloj.dormir(1.5)  # el 1 lleva 3,5 s sin heartbeat; el 2, 1,5 s
    assert registro.elegir() == 2
    assert registro.estado()["1"]["estado"] == "caido"
    assert registro.estado()["2"] == {"estado": "vivo", "tareas_en_curso": 10, "ultimo_heartbeat_hace": 1.5}


def test_sin_nodos_vivos_no_elige_ninguno():
    registro, _ = _registro()
    assert registro.elegir() is None
    registro.actualizar(1, 0)
    assert registro.elegir(excluir={1}) is None


def test_reiniciar_lo_vacia():
    registro, _ = _registro()
    registro.actualizar(1, 0)
    registro.reiniciar()
    assert registro.estado() == {}


def test_conocer_no_pisa_lo_que_trajo_un_heartbeat():
    registro, _ = _registro()
    registro.actualizar(2, 5)
    registro.conocer(2)
    registro.conocer(3)
    assert {n: e["tareas_en_curso"] for n, e in registro.estado().items()} == {"2": 5, "3": 0}


def test_asumir_coordinador_atomico_limpia_epoca_anterior_y_anota_al_lider():
    registro, reloj = _registro()
    registro.actualizar(2, 3)
    registro.actualizar(3, 1)
    reloj.dormir(1.0)
    # Asume como nuevo coordinador con 0 tareas en curso
    registro.asumir_coordinador(1, 0)
    estado = registro.estado()
    assert list(estado.keys()) == ["1"]
    assert estado["1"]["tareas_en_curso"] == 0
    assert estado["1"]["estado"] == "vivo"


def test_asumir_coordinador_concurrente_con_heartbeats():
    import threading
    registro, _ = _registro()

    def mandar_heartbeats():
        for i in range(50):
            registro.actualizar(2, i % 5)
            registro.actualizar(3, i % 3)

    t = threading.Thread(target=mandar_heartbeats)
    t.start()
    registro.asumir_coordinador(1, 0)
    t.join()

    # El coordinador propio debe seguir registrado y vivo, más los heartbeats recibidos
    estado = registro.estado()
    assert "1" in estado and estado["1"]["estado"] == "vivo"
    assert estado["1"]["tareas_en_curso"] == 0
