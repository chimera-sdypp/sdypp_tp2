"""Reloj Lógico de Lamport [LAM78].

Garantiza el ordenamiento lógico parcial/total de eventos en sistemas distribuidos.
Thread-safe mediante exclusión mutua interna.
"""

import threading


class RelojLamport:
    """Implementación de Reloj Lógico de Lamport."""

    def __init__(self, inicial: int = 0):
        self._lock = threading.Lock()
        self._valor = inicial

    @property
    def valor(self) -> int:
        with self._lock:
            return self._valor

    def obtener(self) -> int:
        with self._lock:
            return self._valor

    def incrementar(self) -> int:
        """Incrementa el reloj local por un evento interno o de emisión (L = L + 1)."""
        with self._lock:
            self._valor += 1
            return self._valor

    def actualizar(self, timestamp_remoto: int) -> int:
        """Actualiza el reloj local al recibir un mensaje con timestamp remoto:

        L_local = max(L_local, timestamp_remoto) + 1
        """
        with self._lock:
            self._valor = max(self._valor, int(timestamp_remoto)) + 1
            return self._valor
