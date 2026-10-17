"""Limitador de Tasa por Usuario — Algoritmo Token Bucket (Requisito R4).

Principios Fundamentales de Control de Flujo:
1. PROTECCIÓN CONTRA ABUSO Y BOTS: Evita ataques de denegación de servicio (DoS)
   o saturación no autorizada del servidor limitando cuántos pedidos puede emitir
   cada usuario autenticado en una ventana de tiempo móvil (`TP2_LIMITE_PEDIDOS_MIN`).
2. ARQUITECTURA STATELESS (SIN SESIÓN): No almacena sesiones de usuario ni cookies;
   solo mantiene en memoria un balance volátil de fichas indexado por la identidad (`sub`)
   del token JWT. Si el servidor se reinicia, los baldes vuelven a su capacidad inicial.
3. ALGORITMO TOKEN BUCKET (Balde de Fichas):
   - Cada usuario posee un balde con capacidad máxima de `por_minuto` fichas.
   - Las fichas se reponen continuamente a una tasa fija de `por_minuto / 60` fichas/segundo.
   - Cada solicitud a `/getRemoteTask` consume exactamente 1 ficha.
   - Si no hay fichas suficientes (< 1.0), la solicitud es rechazada inmediatamente con
     HTTP `429 DEMASIADOS_PEDIDOS` y se calcula con precisión el tiempo de espera para la
     cabecera HTTP estándar `Retry-After`.
4. HILO-SEGURIDAD (THREAD SAFETY): Múltiples peticiones simultáneas del mismo o distinto
   usuario compiten por consultar y descontar fichas. Un cerrojo (`threading.Lock`)
   asegura atomicidad completa en el cálculo de reposición y decremento.
"""

import threading
import time


class LimitadorPorUsuario:
    """Implementa un limitador de tasa Token Bucket por identidad de usuario."""

    def __init__(self, por_minuto: int, reloj=time.monotonic):
        """Inicializa el limitador con una cuota máxima por minuto.
        
        Args:
            por_minuto: Cantidad máxima de pedidos permitidos por minuto (0 desactiva el límite).
            reloj: Función de tiempo monotónico (inmune a saltos de reloj NTP del sistema operativo).
        """
        self.por_minuto = por_minuto
        self._reloj = reloj
        self._candado = threading.Lock()
        # Diccionario en memoria: usuario -> (fichas_actuales: float, timestamp_ultimo_calculo: float)
        self._baldes = {}

    def reservar(self, usuario: str) -> float:
        """Intenta consumir 1 ficha del balde asignado al usuario.
        
        Retorno:
            - 0.0: Si la reserva fue exitosa (el usuario tenía al menos 1 ficha). El pedido puede continuar.
            - > 0.0: Segundos exactos que debe esperar el cliente hasta que se regenere al menos 1 ficha.
              Este valor se utiliza directamente en la cabecera `Retry-After` de la respuesta HTTP 429.
        """
        if self.por_minuto <= 0:
            return 0.0  # Límite desactivado por configuración

        ahora = self._reloj()
        # Tasa de regeneración: cuántas fracciones de ficha se acreditan por cada segundo transcurrido
        reposicion = self.por_minuto / 60.0

        with self._candado:
            # Si el usuario es nuevo, arranca con el balde lleno a su capacidad máxima
            fichas, antes = self._baldes.get(usuario, (float(self.por_minuto), ahora))

            # Reposición continua: sumamos las fichas generadas en el intervalo (ahora - antes),
            # sin superar nunca la capacidad máxima del balde (clamp a `self.por_minuto`).
            fichas = min(float(self.por_minuto), fichas + (ahora - antes) * reposicion)

            if fichas >= 1.0:
                # Éxito: descontamos 1 ficha completa y actualizamos el instante de cómputo
                self._baldes[usuario] = (fichas - 1.0, ahora)
                return 0.0

            # Rechazo: el balde está agotado (< 1 ficha).
            # Guardamos el saldo parcial acumulado y calculamos cuánto tiempo falta para alcanzar 1.0
            self._baldes[usuario] = (fichas, ahora)
            segundos_espera = (1.0 - fichas) / reposicion
            return segundos_espera

