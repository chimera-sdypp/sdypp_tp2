"""Configuración Centralizada del Servidor (Hit 2 & Requisitos R4 / R5).

Lee y valida variables de entorno del sistema y Docker Secrets:
- REQUISITO R4 (Seguridad y Control):
  * `jwt_secreto`: Secreto criptográfico HMAC-SHA256 (mínimo 32 caracteres).
  * `limite_pedidos_min`: Cuota del limitador de tasa por usuario (Token Bucket).
- REQUISITO R5 (Concurrencia y Aislamiento de Hardware):
  * `workers_max`: Cantidad máxima de hilos workers concurrentes en el pool.
  * `tarea_cpus`: Límite estricto de CPU asignado a cada contenedor tarea (ej. 1.0 core).
  * `tarea_mem_limit`: Límite estricto de memoria RAM asignado a cada contenedor (ej. '256m').
"""

import os
from dataclasses import dataclass
from pathlib import Path


def _leer_secreto(ruta: str) -> str:
    """Lee un secreto desde un archivo montado en el filesystem (`/run/secrets/...`).
    
    Patrón seguro de Docker Secrets: el secreto nunca viaja como variable de entorno
    en texto plano, evitando que quede expuesto en `docker inspect` o volcados de entorno.
    """
    if not ruta or not Path(ruta).is_file():
        return ""
    return Path(ruta).read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class Config:
    """Estructura inmutable que centraliza todos los parámetros operativos del servidor."""

    # Requisito R2: Whitelist de imágenes ejecutables
    imagenes_permitidas: tuple = ()
    red_tareas: str = "tp2-tareas"
    tarea_puerto: int = 8080
    timeout_arranque: float = 30.0
    timeout_ejecucion: float = 60.0
    max_cuerpo: int = 64 * 1024
    dir_logs: str = "logs"

    # Requisito R5: Concurrencia de Workers en el Pool
    workers_max: int = 4

    # Requisito R1: Autenticación en Container Registry Privado
    registry: str = "docker.io"
    registry_usuario: str = ""
    registry_token: str = ""

    # Requisito R4: Autenticación y Autorización por Request (JWT Stateless)
    jwt_secreto: str = ""
    limite_pedidos_min: int = 300
    api_token: str = ""

    # Requisito R5: Contención de Recursos de Hardware por Contenedor (cgroups)
    # Evita que 8 workers saturen la CPU o agoten la RAM del host físico.
    tarea_mem_limit: str = "256m"
    tarea_cpus: float = 1.0

    @classmethod
    def desde_entorno(cls, entorno=None):
        """Construye la configuración a partir de variables de entorno y secretos montados."""
        e = os.environ if entorno is None else entorno
        mem_limit = e.get("TP2_TAREA_MEM_LIMIT")
        if mem_limit is None:
            mem_limit = cls.tarea_mem_limit
        cpus_env = e.get("TP2_TAREA_CPUS")
        cpus = float(cpus_env) if cpus_env and cpus_env.strip() else cls.tarea_cpus
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
            jwt_secreto=_leer_secreto(e.get("TP2_JWT_SECRET_ARCHIVO", "")) or e.get("TP2_JWT_SECRET", "").strip(),
            limite_pedidos_min=int(e.get("TP2_LIMITE_PEDIDOS_MIN") or cls.limite_pedidos_min),
            api_token=_leer_secreto(e.get("TP2_API_TOKEN_ARCHIVO", "")) or e.get("TP2_API_TOKEN", "").strip(),
            tarea_mem_limit=mem_limit,
            tarea_cpus=cpus,
        )


