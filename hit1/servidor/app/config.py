"""Módulo de Configuración Centralizada del Servidor (Hit 1).

Lee las variables de entorno del sistema y archivos de secretos (Docker Secrets),
proveyendo un objeto inmutable de configuración con valores por defecto seguros.

Principios de diseño:
1. Inmutabilidad (@dataclass(frozen=True)): Evita modificaciones accidentales del estado en runtime.
2. Gestión Segura de Credenciales: Lee secretos desde archivos montados en disco para no exponer
   claves en `docker inspect`, logs de volcado o `/proc/<pid>/environ`.
3. Fail-Closed: Variables críticas (como JWT_SECRET) se leen aquí para posterior validación estricta.
"""

import os
from dataclasses import dataclass
from pathlib import Path


def _leer_secreto(ruta: str) -> str:
    """Lee un secreto sensible desde un archivo montado en el filesystem.

    ¿Por qué usar archivos en vez de variables de entorno directas?
    - En Docker y Kubernetes, las variables de entorno quedan visibles en texto plano
      mediante `docker inspect <container_id>` o `/proc/<pid>/environ`.
    - Los secretos montados como archivos (Docker Secrets / tmpfs) residen sólo en RAM
      y tienen permisos UNIX restringidos (0400/0600), impidiendo filtraciones accidentales.

    Parámetros:
        ruta: Ruta absoluta o relativa al archivo que contiene el secreto.

    Retorna:
        El contenido del secreto sin espacios en blanco ni saltos de línea al inicio/fin,
        o un string vacío si la ruta es nula o el archivo no existe.
    """
    # Si la ruta no fue especificada o el archivo no existe en el filesystem, devolvemos vacío
    if not ruta or not Path(ruta).is_file():
        return ""
    # Se lee en UTF-8 y se aplica strip() para remover saltos de línea (\n, \r) generados por editores
    return Path(ruta).read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class Config:
    """Estructura de datos inmutable que almacena todos los parámetros de ejecución del servidor.

    Al ser `frozen=True`, cualquier intento de reasignar un atributo en tiempo de ejecución
    lanzará una excepción (FrozenInstanceError), garantizando predictibilidad y thread-safety.
    """

    # 1. Lista blanca de imágenes autorizadas (Requisito R2).
    # Tupla inmutable con nombres o patrones 'repo:tag' o 'repo@sha256:...'.
    # Si está vacía, el servidor rechaza toda ejecución por seguridad (Fail-Closed).
    imagenes_permitidas: tuple = ()

    # 2. Red de Docker interna para comunicar el servidor con los contenedores de tareas.
    # Esta red debe ser de tipo bridge y privada para aislar la comunicación inter-nodo.
    red_tareas: str = "tp2-tareas"

    # 3. Puerto TCP interno donde el microservicio de la tarea expone su servidor HTTP.
    # Por convención en el proyecto, todas las imágenes de tarea escuchan en el puerto 8080.
    tarea_puerto: int = 8080

    # 4. Timeout máximo (en segundos) para que el contenedor levante y responda a GET /health.
    # Si expira este tiempo sin respuesta exitosa, se destruye el contenedor y se aborta con error.
    timeout_arranque: float = 30.0

    # 5. Timeout máximo (en segundos) para esperar el cálculo por POST /ejecutarTarea.
    # Previene que un cálculo infinito o bucle bloquee indefinidamente recursos del sistema.
    timeout_ejecucion: float = 60.0

    # 6. Tamaño máximo del cuerpo HTTP en bytes (64 KB por defecto).
    # Previene ataques DoS por agotamiento de memoria ante subida de payloads gigantes.
    max_cuerpo: int = 64 * 1024

    # 7. Directorio de persistencia para los logs rotativos del servidor en disco.
    dir_logs: str = "logs"

    # 8. Parámetros de autenticación contra el Docker Registry (Docker Hub).
    # Evita el rate limit de Docker Hub para peticiones anónimas (100 pulls cada 6 horas).
    registry: str = "docker.io"
    registry_usuario: str = ""
    registry_token: str = ""

    # 9. Secreto criptográfico HMAC-SHA256 para firmar y verificar tokens JWT (Requisito R4).
    # Sin este secreto (de al menos 32 caracteres / 256 bits), el servidor no arranca.
    jwt_secreto: str = ""

    # 10. Límite de tasa (Rate Limiting) por usuario mediante Token Bucket (Requisito R4).
    # Cantidad máxima de peticiones por minuto permitidas por cada identidad JWT. 0 = desactivado.
    limite_pedidos_min: int = 300

    @classmethod
    def desde_entorno(cls, entorno=None):
        """Constructor de fábrica (Factory Method) que inicializa la configuración desde variables de entorno.

        Parámetros:
            entorno: Diccionario opcional que simula `os.environ` (útil para inyección en tests unitarios).

        Retorna:
            Instancia congelada de `Config` con los valores leídos y fallbacks por defecto.
        """
        # Si no se pasó un diccionario explícito, se utiliza el entorno global del sistema operativo
        e = os.environ if entorno is None else entorno

        # Lectura y parsing de cada variable de entorno:
        return cls(
            # TP2_IMAGENES_PERMITIDAS: string separado por comas -> tupla de strings limpios
            imagenes_permitidas=tuple(
                i.strip() for i in e.get("TP2_IMAGENES_PERMITIDAS", "").split(",") if i.strip()
            ),
            # TP2_RED_TAREAS: nombre de la red Docker compartida
            red_tareas=e.get("TP2_RED_TAREAS") or cls.red_tareas,
            # TP2_TAREA_PUERTO: puerto numérico interno
            tarea_puerto=int(e.get("TP2_TAREA_PUERTO") or cls.tarea_puerto),
            # TP2_TIMEOUT_ARRANQUE: float con los segundos de espera para el /health inicial
            timeout_arranque=float(e.get("TP2_TIMEOUT_ARRANQUE") or cls.timeout_arranque),
            # TP2_TIMEOUT_EJECUCION: float con los segundos máximos para procesar la tarea
            timeout_ejecucion=float(e.get("TP2_TIMEOUT_EJECUCION") or cls.timeout_ejecucion),
            # TP2_DIR_LOGS: carpeta donde se crean los archivos servidor.log
            dir_logs=e.get("TP2_DIR_LOGS") or cls.dir_logs,
            # TP2_REGISTRY_USUARIO: usuario de Docker Hub
            registry_usuario=e.get("TP2_REGISTRY_USUARIO", ""),
            # TP2_REGISTRY_TOKEN_ARCHIVO: ruta al archivo con el token de acceso a Docker Hub
            registry_token=_leer_secreto(e.get("TP2_REGISTRY_TOKEN_ARCHIVO", "")),
            # TP2_JWT_SECRET_ARCHIVO / TP2_JWT_SECRET: secreto para firmas JWT (prioriza archivo seguro)
            jwt_secreto=_leer_secreto(e.get("TP2_JWT_SECRET_ARCHIVO", "")) or e.get("TP2_JWT_SECRET", "").strip(),
            # TP2_LIMITE_PEDIDOS_MIN: cuota de peticiones por minuto por usuario
            limite_pedidos_min=int(e.get("TP2_LIMITE_PEDIDOS_MIN") or cls.limite_pedidos_min),
        )
