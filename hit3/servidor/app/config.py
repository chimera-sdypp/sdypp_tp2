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


def _leer_pares(texto, nodo_id):
    """`2=http://nodo2:8080,3=http://nodo3:8080` → ((2, "http://nodo2:8080"), (3, ...)).

    Son los otros nodos del cluster: el propio no va en la lista."""
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
    imagenes_permitidas: tuple = ()
    red_tareas: str = "tp2-tareas"
    tarea_puerto: int = 8080
    timeout_arranque: float = 30.0
    timeout_ejecucion: float = 60.0
    max_cuerpo: int = 64 * 1024
    dir_logs: str = "logs"
    # El token es de Docker Hub: sólo se manda en los pulls a este registry.
    registry: str = "docker.io"
    registry_usuario: str = ""
    registry_token: str = ""
    # Cluster (Hit 3). Los tiempos están en segundos.
    nodo_id: int = 1
    pares: tuple = ()                    # ((id, url), ...) de los otros nodos
    intervalo_heartbeat: float = 1.0     # cada cuánto cada nodo le avisa al coordinador que está vivo
    timeout_mensaje: float = 1.0         # para un heartbeat, una ELECCION o un COORDINADOR
    vencimiento_nodo: float = 3.0        # sin heartbeat en este tiempo, el coordinador lo da por caído
    timeout_coordinador: float = 3.0     # después de un OK, cuánto se espera el COORDINADOR
    timeout_asignacion: float = 10.0     # cuánto espera una tarea a que haya coordinador

    @classmethod
    def desde_entorno(cls, entorno=None):
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
            nodo_id=nodo_id,
            pares=_leer_pares(e.get("TP2_PARES", ""), nodo_id),
            **{campo: float(e[variable]) for campo, variable in _TIEMPOS.items() if e.get(variable)},
        )


_TIEMPOS = {
    "intervalo_heartbeat": "TP2_INTERVALO_HEARTBEAT",
    "timeout_mensaje": "TP2_TIMEOUT_MENSAJE",
    "vencimiento_nodo": "TP2_VENCIMIENTO_NODO",
    "timeout_coordinador": "TP2_TIMEOUT_COORDINADOR",
    "timeout_asignacion": "TP2_TIMEOUT_ASIGNACION",
}
