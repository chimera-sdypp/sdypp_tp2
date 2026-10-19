"""Módulo de Registro de Auditoría y Bitácora Multi-Destino (Hit 3).

Implementa un sistema de logging desacoplado con tres destinos simultáneos:
1. Consola Estándar (`StreamHandler`): Visualización en tiempo real vía `docker compose logs`.
2. Búfer Circular en RAM (`HandlerEnMemoria`): Conserva las últimas 500 líneas en memoria volátil
   para diagnóstico rápido sin acceso directo a disco.
3. Archivo Rotativo Persistente (`RotatingFileHandler`): Almacenado en disco en `logs/servidor.log`
   con rotación automática (1 MiB máximo por archivo, hasta 3 respaldos históricos).
"""

import logging
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path


class HandlerEnMemoria(logging.Handler):
    """Manejador de logging en memoria RAM basado en una cola circular de tamaño acotado."""

    def __init__(self, capacidad=500):
        """Inicializa el búfer circular en memoria con capacidad máxima fija."""
        super().__init__()
        self.registros = deque(maxlen=capacidad)

    def emit(self, record):
        """Formatea el registro y lo almacena al final de la cola circular."""
        self.registros.append(self.format(record))


def configurar(directorio):
    """Configura e inicializa el subsistema de logging para el nodo del cluster.
    
    Retorna:
    - Tupla: (logger_configurado, handler_en_memoria)
    """
    logger = logging.getLogger("tp2.servidor")
    logger.setLevel(logging.INFO)
    # Limpieza preventiva de manejadores previos para evitar duplicación de logs
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    logger.propagate = False

    # Aseguramos la existencia del directorio de logs
    Path(directorio).mkdir(parents=True, exist_ok=True)
    memoria = HandlerEnMemoria()
    # Manejador con rotación en disco (1 MiB por archivo, 3 backups)
    disco = RotatingFileHandler(Path(directorio) / "servidor.log", maxBytes=1024 * 1024,
                                backupCount=3, encoding="utf-8")
    formato = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    
    # Registramos los tres destinos con el mismo formato estándar
    for handler in (logging.StreamHandler(), memoria, disco):
        handler.setFormatter(formato)
        logger.addHandler(handler)
    return logger, memoria

