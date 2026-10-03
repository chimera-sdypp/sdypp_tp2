"""Logs en memoria y en disco.

Cada línea sale a la consola (la ve `docker logs`), a un buffer circular en RAM
y a un archivo rotativo en disco, que queda aunque el contenedor muera.
"""

import logging
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path


class HandlerEnMemoria(logging.Handler):
    """Conserva las últimas `capacidad` líneas ya formateadas."""

    def __init__(self, capacidad=500):
        super().__init__()
        self.registros = deque(maxlen=capacidad)

    def emit(self, record):
        self.registros.append(self.format(record))


def configurar(directorio):
    """Devuelve (logger, handler_en_memoria) con salida a consola, RAM y disco."""
    logger = logging.getLogger("tp2.servidor")
    logger.setLevel(logging.INFO)
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    logger.propagate = False

    Path(directorio).mkdir(parents=True, exist_ok=True)
    memoria = HandlerEnMemoria()
    disco = RotatingFileHandler(Path(directorio) / "servidor.log", maxBytes=1024 * 1024,
                                backupCount=3, encoding="utf-8")
    formato = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    for handler in (logging.StreamHandler(), memoria, disco):
        handler.setFormatter(formato)
        logger.addHandler(handler)
    return logger, memoria
