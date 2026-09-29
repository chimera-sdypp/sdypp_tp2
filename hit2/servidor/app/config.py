"""Configuración del servidor, leída de variables de entorno."""

import os
from dataclasses import dataclass
from pathlib import Path


def _leer_secreto(ruta):
    """Lee un secreto de un archivo montado (secret de compose) y no de una variable
    de entorno: así no aparece en `docker inspect`. Sin archivo, queda vacío."""
    if not ruta or not Path(ruta).is_file():
        return ""
    return Path(ruta).read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class Config:
    imagenes_permitidas: tuple = ()
    red_tareas: str = "tp2-tareas"
    tarea_puerto: int = 8080
    timeout_arranque: float = 30.0
    timeout_ejecucion: float = 60.0
    max_cuerpo: int = 64 * 1024
    dir_logs: str = "logs"
    workers_max: int = 4
    # El token es de Docker Hub: sólo se manda en los pulls a este registry.
    registry: str = "docker.io"
    registry_usuario: str = ""
    registry_token: str = ""

    @classmethod
    def desde_entorno(cls, entorno=None):
        e = os.environ if entorno is None else entorno
        return cls(
            imagenes_permitidas=tuple(
                i.strip() for i in e.get("TP2_IMAGENES_PERMITIDAS", "").split(",") if i.strip()),
            red_tareas=e.get("TP2_RED_TAREAS") or cls.red_tareas,
            tarea_puerto=int(e.get("TP2_TAREA_PUERTO") or cls.tarea_puerto),
            timeout_arranque=float(e.get("TP2_TIMEOUT_ARRANQUE") or cls.timeout_arranque),
            timeout_ejecucion=float(e.get("TP2_TIMEOUT_EJECUCION") or cls.timeout_ejecucion),
            dir_logs=e.get("TP2_DIR_LOGS") or cls.dir_logs,
            workers_max=int(e.get("TP2_WORKERS_MAX") or cls.workers_max),
            registry_usuario=e.get("TP2_REGISTRY_USUARIO", ""),
            registry_token=_leer_secreto(e.get("TP2_REGISTRY_TOKEN_ARCHIVO", "")),
        )

