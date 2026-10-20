"""Limitador de Tasa por Usuario basado en Balde de Fichas (Token Bucket — Requisito R4).

Fundamento Algorítmico (Token Bucket):
1. Capacidad Máxima y Tasa de Relleno:
   - Cada usuario posee una capacidad máxima de `por_minuto` fichas (tokens).
   - Las fichas se reponen continuamente a una tasa constante de `por_minuto / 60.0` fichas por segundo.
   - Cada solicitud consumida descuenta exactamente 1 ficha.

2. Evaluación con Reloj Monotónico:
   - Se utiliza `time.monotonic()` inmune a saltos temporales del reloj de pared (NTP o cambios de huso horario).
   - En lugar de hilos de background que despierten cada segundo a recargar baldes (lo cual saturaría
     la CPU), la reposición se calcula bajo demanda (lazy evaluation) calculando `(ahora - antes) * reposicion`.

3. Respuestas 429 con Retry-After:
   - Si un usuario no tiene fichas suficientes (`fichas < 1.0`), la solicitud se rechaza con HTTP 429
     y se calcula el tiempo exacto en segundos para acumular una ficha: `(1.0 - fichas) / reposicion`.
"""

import threading
import time


class LimitadorPorUsuario:
    """Implementación hilo-segura del algoritmo Token Bucket por identidad de usuario."""

    def __init__(self, por_minuto: int, reloj=time.monotonic):
        """Inicializa el limitador de tasa.
        
        Parámetros:
        - por_minuto: Cantidad de solicitudes permitidas por minuto (0 desactiva el límite).
        - reloj: Función proveedora del tiempo monotónico.
        """
        self.por_minuto = por_minuto
        self._reloj = reloj
        self._candado = threading.Lock()
        # Mapeo en memoria: usuario -> (fichas_actuales, timestamp_ultima_actualizacion)
        self._baldes = {}

    def reservar(self, usuario: str) -> float:
        """Intenta reservar y consumir una ficha del balde del usuario.
        
        Retorna:
        - 0.0: Si la petición fue admitida con éxito (se consumió 1 ficha).
        - float > 0.0: Tiempo en segundos que el usuario debe esperar hasta que haya 1 ficha disponible
          (utilizado para poblar la cabecera HTTP `Retry-After`).
        """
        if self.por_minuto <= 0:
            return 0.0
        ahora = self._reloj()
        reposicion = self.por_minuto / 60.0
        with self._candado:
            # Obtenemos el balde actual del usuario (por defecto balde lleno si es la primera vez)
            fichas, antes = self._baldes.get(usuario, (float(self.por_minuto), ahora))
            # Reposición perezosa continua acotada al tope máximo
            fichas = min(float(self.por_minuto), fichas + (ahora - antes) * reposicion)
            if fichas >= 1.0:
                # Caso Exitoso: Se descuenta una ficha y se actualiza el timestamp
                self._baldes[usuario] = (fichas - 1.0, ahora)
                return 0.0
            # Caso Agotado: Se preserva la fracción acumulada y se retorna la espera restante
            self._baldes[usuario] = (fichas, ahora)
            return (1.0 - fichas) / reposicion

