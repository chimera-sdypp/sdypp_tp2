"""Módulo de Configuración Centralizada del Nodo del Cluster (Hit 3).

Administra la lectura y validación estricta de variables de entorno y secretos montados:
- Secretos en Archivos: Prevención de fuga de credenciales sensibles (tokens de Docker Hub, secretos JWT)
  evitando que queden expuestos en comandos `docker inspect` o volcados de entorno.
- Membresía del Cluster: Deserialización de `TP2_PARES` (`<id>=<url>,...`) garantizando que ningún nodo
  se incluya a sí mismo ni existan identificadores duplicados.
- Parámetros Temporales de Bully y Tolerancia a Fallos:
  * `intervalo_heartbeat`: Frecuencia de emisión de latidos de workers al líder (por defecto 1.0 s).
  * `timeout_mensaje`: Límite de espera para respuestas HTTP entre pares (1.0 s).
  * `vencimiento_nodo`: Ventana de tiempo tras la cual un nodo sin latidos se considera caído (3.0 s).
  * `timeout_coordinador`: Tiempo que un nodo espera el anuncio `COORDINADOR` antes de reintentar (3.0 s).
  * `timeout_asignacion`: Ventana máxima que una tarea espera a que el cluster elija un líder (10.0 s).
"""

import os
from dataclasses import dataclass
from pathlib import Path


def _leer_secreto(ruta):
    """Lee un secreto criptográfico desde un archivo montado en el contenedor (Docker secrets).
    
    Protege la confidencialidad de tokens y claves al no pasarlas por variables de entorno de proceso.
    """
    if not ruta or not Path(ruta).is_file():
        return ""
    return Path(ruta).read_text(encoding="utf-8").strip()


def _leer_pares(texto, nodo_id):
    """Parsea la lista de pares del cluster: `2=http://nodo2:8080,3=http://nodo3:8080`.
    
    Validaciones de Integridad:
    1. Formato estricto `<id_entero>=<url>`.
    2. Exclusión del propio nodo (un nodo no debe figurar en su propia lista de pares).
    3. Detección de identificadores numéricos duplicados.
    Retorna una tupla inmutable y ordenada de tuplas `(id_nodo, url)`.
    """
    pares = {}
    for item in (i.strip() for i in texto.split(",")):
        if not item:
            continue
        nodo, igual, url = item.partition("=")
        if not igual or not nodo.strip().isdigit() or not url.strip():
            raise ValueError(f"TP2_PARES: {item!r} no tiene la forma <id>=<url>")
        nodo = int(nodo)
        if nodo == nodo_id or nodo in pares:
            raise ValueError(f"TP2_PARES: el nodo {nodo} está repetido o es el propio")
        pares[nodo] = url.strip().rstrip("/")
    return tuple(sorted(pares.items()))


@dataclass(frozen=True)
class Config:
    """Configuración inmutable de un nodo en el cluster distribuido."""
    imagenes_permitidas: tuple = ()
    red_tareas: str = "tp2-tareas"
    tarea_puerto: int = 8080
    timeout_arranque: float = 30.0
    timeout_ejecucion: float = 60.0
    max_cuerpo: int = 64 * 1024
    dir_logs: str = "logs"
    # Autenticación contra Docker Registry
    registry: str = "docker.io"
    registry_usuario: str = ""
    registry_token: str = ""
    # Secreto simétrico HMAC-SHA256 para validación de JWT (Requisito R4)
    jwt_secreto: str = ""
    # Cuota por usuario de solicitudes por minuto (Token Bucket)
    limite_pedidos_min: int = 300
    
    # Parámetros específicos de Cluster y Elección Bully (Hit 3)
    nodo_id: int = 1                     # ID numérico único del nodo
    pares: tuple = ()                    # Tupla de (id, url) de los otros nodos del cluster
    intervalo_heartbeat: float = 1.0     # Frecuencia en segundos de los latidos de workers
    timeout_mensaje: float = 1.0         # Timeout de peticiones RPC entre nodos pares
    vencimiento_nodo: float = 3.0        # Tiempo sin latidos para declarar a un worker caído
    timeout_coordinador: float = 3.0     # Tiempo de espera tras OK para recibir COORDINADOR
    timeout_asignacion: float = 10.0     # Tiempo máximo que espera una tarea para ser asignada

    @classmethod
    def desde_entorno(cls, entorno=None):
        """Construye la configuración leyendo variables de entorno del sistema operativo."""
        e = os.environ if entorno is None else entorno
        nodo_id = int(e.get("TP2_NODO_ID") or cls.nodo_id)
        return cls(
            imagenes_permitidas=tuple(
                i.strip() for i in e.get("TP2_IMAGENES_PERMITIDAS", "").split(",") if i.strip()),
            red_tareas=e.get("TP2_RED_TAREAS") or cls.red_tareas,
            tarea_puerto=int(e.get("TP2_TAREA_PUERTO") or cls.tarea_puerto),
            timeout_arranque=float(e.get("TP2_TIMEOUT_ARRANQUE") or cls.timeout_arranque),
            timeout_ejecucion=float(e.get("TP2_TIMEOUT_EJECUCION") or cls.timeout_ejecucion),
            dir_logs=e.get("TP2_DIR_LOGS") or cls.dir_logs,
            registry_usuario=e.get("TP2_REGISTRY_USUARIO", ""),
            registry_token=_leer_secreto(e.get("TP2_REGISTRY_TOKEN_ARCHIVO", "")),
            jwt_secreto=_leer_secreto(e.get("TP2_JWT_SECRET_ARCHIVO", "")) or e.get("TP2_JWT_SECRET", "").strip(),
            limite_pedidos_min=int(e.get("TP2_LIMITE_PEDIDOS_MIN") or cls.limite_pedidos_min),
            nodo_id=nodo_id,
            pares=_leer_pares(e.get("TP2_PARES", ""), nodo_id),
            **{campo: float(e[variable]) for campo, variable in _TIEMPOS.items() if e.get(variable)},
        )


# Mapeo de nombres de atributos internos a variables de entorno del sistema
_TIEMPOS = {
    "intervalo_heartbeat": "TP2_INTERVALO_HEARTBEAT",
    "timeout_mensaje": "TP2_TIMEOUT_MENSAJE",
    "vencimiento_nodo": "TP2_VENCIMIENTO_NODO",
    "timeout_coordinador": "TP2_TIMEOUT_COORDINADOR",
    "timeout_asignacion": "TP2_TIMEOUT_ASIGNACION",
}

