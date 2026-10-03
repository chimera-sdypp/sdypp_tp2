"""Tests unitarios para el Reloj Lógico de Lamport."""

import threading
from app.lamport import RelojLamport


def test_reloj_incrementar():
    reloj = RelojLamport(0)
    assert reloj.valor == 0
    assert reloj.incrementar() == 1
    assert reloj.incrementar() == 2
    assert reloj.obtener() == 2


def test_reloj_actualizar():
    reloj = RelojLamport(5)
    # L_local = max(5, 10) + 1 = 11
    nuevo = reloj.actualizar(10)
    assert nuevo == 11
    assert reloj.valor == 11

    # Si llega un timestamp menor: L_local = max(11, 3) + 1 = 12
    assert reloj.actualizar(3) == 12


def test_reloj_concurrente():
    reloj = RelojLamport(0)
    hilos = []

    def _incrementar():
        for _ in range(100):
            reloj.incrementar()

    for _ in range(10):
        t = threading.Thread(target=_incrementar)
        hilos.append(t)
        t.start()

    for t in hilos:
        t.join()

    assert reloj.valor == 1000
