"""Sistema de Trazabilidad y Logging Multi-Destino (Hit 1).

Implementa la arquitectura de auditoría y diagnóstico del servidor emitiendo
cada evento hacia tres destinos simultáneos:
1. Salida Estándar (Console/stdout): Capturada por el Docker Daemon (`docker logs`).
2. Buffer Circular en Memoria RAM (`HandlerEnMemoria`): Mantiene en memoria las últimas
   N líneas para inspección rápida sin I/O de disco.
3. Archivo Rotativo en Disco (`RotatingFileHandler`): Persiste hasta 3 archivos de 1 MB cada uno
   (3 MB totales rotados), asegurando que la evidencia forense sobreviva si el contenedor se reinicia
   y evitando saturar el almacenamiento físico del host.
"""

import logging
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Tuple


class HandlerEnMemoria(logging.Handler):
    """Handler de logging que almacena registros en una cola circular FIFO en memoria RAM.

    Utiliza `collections.deque(maxlen=capacidad)` que opera con complejidad temporal O(1)
    tanto para inserción como para descarte automático de los mensajes más antiguos.
    """

    def __init__(self, capacidad: int = 500):
        """Inicializa el buffer en RAM con una capacidad acotada para evitar consumo desmedido de memoria."""
        super().__init__()
        # deque(maxlen=...) descarta automáticamente el elemento más viejo cuando se alcanza el límite
        self.registros = deque(maxlen=capacidad)

    def emit(self, record: logging.LogRecord) -> None:
        """Formatea y almacena el registro entrante en el buffer circular."""
        self.registros.append(self.format(record))


def configurar(directorio: str) -> Tuple[logging.Logger, HandlerEnMemoria]:
    """Inicializa y configura el Logger principal 'tp2.servidor'.

    Parámetros:
        directorio: Ruta de la carpeta donde se almacenará el archivo `servidor.log`.

    Retorna:
        Una tupla `(logger, memoria)` donde:
        - `logger` es la instancia configurada con nivel INFO.
        - `memoria` es el `HandlerEnMemoria` para consultas directas del buffer.
    """
    logger = logging.getLogger("tp2.servidor")
    logger.setLevel(logging.INFO)

    # Limpieza preventiva de handlers anteriores (evita logs duplicados si se reinicializa en tests)
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    # propagate = False evita que los logs suban al logger raíz de Python y se dupliquen en consola
    logger.propagate = False

    # Asegura que el directorio destino exista en el sistema de archivos
    Path(directorio).mkdir(parents=True, exist_ok=True)

    # Instanciación de los 3 destinos:
    memoria = HandlerEnMemoria(capacidad=500)
    # RotatingFileHandler: 1 MB máximo por archivo, mantiene hasta 3 backups históricos (servidor.log.1, etc.)
    disco = RotatingFileHandler(
        Path(directorio) / "servidor.log",
        maxBytes=1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    consola = logging.StreamHandler()

    # Formato unificado y estructurado: Timestamp ISO | Nivel de severidad | Mensaje
    formato = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    for handler in (consola, memoria, disco):
        handler.setFormatter(formato)
        logger.addHandler(handler)

    return logger, memoria
