"""El balde de fichas por usuario (R4), con un reloj controlado."""

from app.limitador import LimitadorPorUsuario


class RelojFalso:
    def __init__(self):
        self.ahora = 0.0

    def __call__(self):
        return self.ahora


def test_deja_pasar_hasta_el_limite_y_despues_dice_cuanto_esperar():
    reloj = RelojFalso()
    limitador = LimitadorPorUsuario(60, reloj)   # una ficha por segundo
    assert [limitador.reservar("ana") for _ in range(60)] == [0.0] * 60
    espera = limitador.reservar("ana")
    assert 0.9 < espera <= 1.0


def test_repone_fichas_con_el_tiempo_pero_no_pasa_del_tope():
    reloj = RelojFalso()
    limitador = LimitadorPorUsuario(60, reloj)
    for _ in range(60):
        limitador.reservar("ana")
    assert limitador.reservar("ana") > 0
    reloj.ahora += 1.0
    assert limitador.reservar("ana") == 0.0
    assert limitador.reservar("ana") > 0
    reloj.ahora += 3600           # una hora parada: sigue valiendo 60 fichas, no más
    assert [limitador.reservar("ana") for _ in range(60)] == [0.0] * 60
    assert limitador.reservar("ana") > 0


def test_cada_usuario_tiene_su_balde():
    limitador = LimitadorPorUsuario(1, RelojFalso())
    assert limitador.reservar("ana") == 0.0
    assert limitador.reservar("ana") > 0
    assert limitador.reservar("beto") == 0.0


def test_cero_lo_apaga():
    limitador = LimitadorPorUsuario(0, RelojFalso())
    assert all(limitador.reservar("ana") == 0.0 for _ in range(1000))
