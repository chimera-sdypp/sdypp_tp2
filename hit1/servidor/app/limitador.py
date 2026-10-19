"""Control de Tasa de Peticiones por Usuario - Rate Limiter (Requisito R4).

Implementa el algoritmo clásico de "Balde de Fichas" (Token Bucket) en memoria RAM,
asociado a la identidad criptográfica extraída del JWT (`sub`).

Principios de Diseño y Matemáticas del Algoritmo:
1. Capacidad del Balde (Burst Capacity):
   - Cada usuario comienza con un balde lleno con `por_minuto` fichas.
   - Permite ráfagas instantáneas de hasta `por_minuto` peticiones consecutivas si el balde está lleno.
2. Tasa de Reposición Continua (Refill Rate):
   - Se repone a razón de `reposicion = por_minuto / 60.0` fichas por segundo.
   - La reposición es perezosa (lazy evaluation): se calcula únicamente en el momento exacto
     en que el usuario realiza una petición, comparando el instante actual contra el anterior:
     `fichas_actuales = min(capacidad, fichas_anteriores + delta_tiempo * tasa_reposicion)`.
3. Reloj Monótono (`time.monotonic`):
   - Se utiliza `time.monotonic` en lugar de `time.time()` (wall-clock) para evitar fallas
     provocadas por ajustes de hora del sistema operativo, sincronizaciones NTP o saltos de horario.
4. Thread-Safety:
   - Todas las mutaciones del diccionario `_baldes` están protegidas por un `threading.Lock`
     para garantizar atomicidad y evitar condiciones de carrera en entornos multi-hilo.
5. Fail-Safe:
   - Si `por_minuto <= 0`, el limitador se considera desactivado y deja pasar todo tráfico.
"""

import threading
import time
from typing import Callable, Dict, Tuple


class LimitadorPorUsuario:
    """Administrador de baldes de fichas por usuario."""

    def __init__(self, por_minuto: int, reloj: Callable[[], float] = time.monotonic):
        """Inicializa el limitador.

        Parámetros:
            por_minuto: Cuota de peticiones permitidas por minuto por cada usuario.
            reloj: Función proveedora del tiempo actual (inyectable en tests unitarios).
        """
        self.por_minuto = por_minuto
        self._reloj = reloj
        self._candado = threading.Lock()
        # Diccionario en memoria: usuario (str) -> tupla (fichas_disponibles: float, timestamp_ultimo_acceso: float)
        self._baldes: Dict[str, Tuple[float, float]] = {}

    def reservar(self, usuario: str) -> float:
        """Intenta consumir 1 ficha del balde correspondiente al usuario especificado.

        Retorna:
            - `0.0` si la petición fue autorizada (el usuario disponía de al menos 1 ficha y fue consumida).
            - Un `float > 0.0` con la cantidad exacta de segundos que el cliente debe esperar
              para que el balde reponga al menos 1 ficha completa (para la cabecera `Retry-After`).
        """
        # Si el límite está apagado (<= 0), no se aplican restricciones
        if self.por_minuto <= 0:
            return 0.0

        ahora = self._reloj()
        # Tasa de fichas que se regeneran por cada segundo transcurrido
        reposicion = self.por_minuto / 60.0

        with self._candado:
            # Si es la primera vez que vemos al usuario, su balde arranca lleno al máximo
            fichas, antes = self._baldes.get(usuario, (float(self.por_minuto), ahora))

            # Se calcula cuántas fichas se acumularon desde la última visita (acotado a la capacidad máxima)
            fichas = min(float(self.por_minuto), fichas + (ahora - antes) * reposicion)

            # Caso 1: Hay al menos 1 ficha completa disponible para gastar
            if fichas >= 1.0:
                # Se descuenta la ficha consumida y se actualiza el instante de acceso
                self._baldes[usuario] = (fichas - 1.0, ahora)
                return 0.0

            # Caso 2: Balde agotado (< 1.0 ficha). Se rechaza la petición sin descontar fichas.
            self._baldes[usuario] = (fichas, ahora)
            # Tiempo en segundos necesarios para recuperar la fracción faltante hasta llegar a 1.0 ficha:
            return (1.0 - fichas) / reposicion
