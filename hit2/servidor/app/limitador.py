"""Límite de pedidos por usuario (R4): un balde de fichas por usuario, en memoria.

No es una sesión: es un contador de uso por identidad. Cada usuario tiene `por_minuto`
fichas, se le repone una cada `60 / por_minuto` segundos y cada pedido gasta una. Si no
queda ninguna, el pedido es `429` y se le dice cuánto esperar. `por_minuto = 0` lo apaga.
"""

import threading
import time


class LimitadorPorUsuario:
    def __init__(self, por_minuto, reloj=time.monotonic):
        self.por_minuto = por_minuto
        self._reloj = reloj
        self._candado = threading.Lock()
        self._baldes = {}   # usuario -> (fichas, instante de la última cuenta)

    def reservar(self, usuario):
        """0.0 si el pedido pasa (y gasta una ficha); si no, los segundos hasta tener una."""
        if self.por_minuto <= 0:
            return 0.0
        ahora = self._reloj()
        reposicion = self.por_minuto / 60.0
        with self._candado:
            fichas, antes = self._baldes.get(usuario, (float(self.por_minuto), ahora))
            fichas = min(float(self.por_minuto), fichas + (ahora - antes) * reposicion)
            if fichas >= 1.0:
                self._baldes[usuario] = (fichas - 1.0, ahora)
                return 0.0
            self._baldes[usuario] = (fichas, ahora)
            return (1.0 - fichas) / reposicion
