"""Reloj Lógico de Lamport (Leslie Lamport, 1978 — CACM).

Fundamento Teórico del Ordenamiento Causal:
En un sistema distribuido asíncrono no existe un reloj físico global perfectamente
sincronizado. Para establecer un orden consistente entre eventos, Leslie Lamport introdujo
la relación "sucedió-antes" (happened-before, denotada $a \\to b$) basada en causalidad:

1. REGLA 1 (Evento Local o Emisión de Mensaje):
   Antes de emitir una respuesta HTTP o registrar un evento local, el proceso avanza su reloj:
   $$L = L + 1$$
2. REGLA 2 (Recepción de Mensaje):
   Al recibir una petición con timestamp lógico del cliente ($L_{remoto}$ en `X-Lamport-Clock`),
   el servidor sincroniza su reloj para reflejar que la recepción es causalmente posterior
   al envío:
   $$L_{local} = \\max(L_{local}, L_{remoto}) + 1$$
3. DESEMPATE EN LA COLA DE WORKERS:
   En `PoolWorkers`, las tareas se insertan en una Min-Heap ordenada por este timestamp de Lamport.
   Si dos tareas tienen el mismo timestamp lógico, desempatan de forma determinística por orden
   de llegada física (FIFO).
4. HILO-SEGURIDAD:
   Todas las operaciones de lectura, incremento y actualización están protegidas por
   un cerrojo (`threading.Lock`), permitiendo lecturas y escrituras atómicas en entornos concurrentes.
"""

import threading


class RelojLamport:
    """Implementación hilo-segura de un Reloj Lógico Escalar de Lamport."""

    def __init__(self, inicial: int = 0):
        """Inicializa el reloj escalar con un valor base (por defecto 0)."""
        self._lock = threading.Lock()
        self._valor = inicial

    @property
    def valor(self) -> int:
        """Valor actual del reloj bajo exclusión mutua."""
        with self._lock:
            return self._valor

    def obtener(self) -> int:
        """Alias para consultar el valor actual del reloj."""
        with self._lock:
            return self._valor

    def incrementar(self) -> int:
        """Regla 1: Incrementa el reloj por un evento local o emisión de respuesta (L = L + 1)."""
        with self._lock:
            self._valor += 1
            return self._valor

    def actualizar(self, timestamp_remoto: int) -> int:
        """Regla 2: Sincroniza el reloj local al recibir un mensaje del cliente.
        
        Aplica: L_local = max(L_local, timestamp_remoto) + 1
        Garantiza que el evento de recepción sea causalmente posterior al de emisión.
        """
        with self._lock:
            self._valor = max(self._valor, int(timestamp_remoto)) + 1
            return self._valor

