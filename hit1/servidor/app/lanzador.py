"""Orquestador de Ciclo de Vida de Contenedores de Tarea (Hit 1).

Implementa la función central `ejecutarTareaRemota()` exigida por el enunciado:
1. Conexión directa al daemon de Docker mediante el SDK oficial (`/var/run/docker.sock`).
   (No utiliza el CLI `docker run`, lo que hace a la imagen del servidor liviana y segura).
2. Descarga autenticada de imágenes (Pull) desde Docker Hub cuando no están en el host.
3. Creación y ejecución de contenedores efímeros aislados en la red interna `tp2-tareas`.
4. Monitoreo de arranque activo (Active Polling) contra `GET /health` de la tarea.
5. Invocación de cálculo vía HTTP RPC `POST /ejecutarTarea`.
6. Garantía de destrucción incondicional del contenedor en bloque `finally:` (`remove(force=True)`),
   evitando fugas de recursos o acumulación de contenedores huérfanos.
"""

import json
import time
import urllib.error
import urllib.request
from typing import Any, NamedTuple

import docker
from docker.errors import APIError, DockerException, NotFound
from requests.exceptions import RequestException

from app.errores import ErrorApi, TipoError

# Tupla de excepciones que indican inaccesibilidad al daemon de Docker:
# - DockerException: error del SDK (ej. socket no montado).
# - RequestException: error de transporte HTTP sobre el socket UNIX.
# - OSError: permisos denegados (EACCES) o socket inexistente (ENOENT).
_SIN_DOCKER = (DockerException, RequestException, OSError)


class Ejecucion(NamedTuple):
    """Estructura inmutable que encapsula el resultado y los metadatos de auditoría de una ejecución."""

    resultado: Any      # Valor computado devuelto por la tarea (cualquier estructura JSON válida)
    contenedor: str     # ID corto (12 caracteres hexadecimales) del contenedor efímero
    imagen_id: str      # Hash SHA-256 local exacto de la imagen con la que se creó el contenedor
    digest: Any         # SHA-256 canónico del registry (RepoDigests); None si fue construida localmente
    ejecucion_s: float  # Tiempo transcurrido (en segundos) desde la creación hasta la obtención del resultado


def _digest_ejecutado(imagen, local):
    """Extrae el digest inmutable (`sha256:...`) del registry para la imagen local ejecutada.

    Si una imagen posee múltiples digests en sus metadatos (RepoDigests), selecciona
    aquel que coincida con el repositorio solicitado. Si no vino de un registry, retorna None.
    """
    candidatos = [d.partition("@") for d in local.attrs.get("RepoDigests") or []]
    propios = [
        digest
        for repo, _, digest in candidatos
        if repo.removeprefix("docker.io/") == imagen.nombre.removeprefix("docker.io/")
    ]
    elegidos = propios or [digest for _, _, digest in candidatos]
    return min(elegidos) if elegidos else None


class SinConexion(Exception):
    """Indica falla de red al intentar conectarse al servidor HTTP interno del contenedor de la tarea."""


class Vencido(Exception):
    """Indica que el contenedor de la tarea superó el tiempo máximo de respuesta (Timeout)."""


def http_json(metodo, url, cuerpo=None, timeout=5.0):
    """Cliente HTTP liviano basado en la biblioteca estándar `urllib`.

    Evita dependencias pesadas y realiza peticiones JSON sincronizadas:
    - Serializa el cuerpo a UTF-8.
    - Configura `Content-Type: application/json`.
    - Captura y normaliza excepciones de socket y timeout en `SinConexion` y `Vencido`.

    Retorna:
        Tupla `(codigo_estado_http, contenido_parseado_o_none)`.
    """
    datos = None if cuerpo is None else json.dumps(cuerpo).encode("utf-8")
    pedido = urllib.request.Request(
        url,
        data=datos,
        method=metodo,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(pedido, timeout=timeout) as respuesta:
            estado, crudo = respuesta.status, respuesta.read()
    except urllib.error.HTTPError as error:
        # Errores 4xx y 5xx devueltos por el servidor HTTP de la tarea
        estado, crudo = error.code, error.read()
    except TimeoutError as error:
        raise Vencido(str(error)) from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise Vencido(str(error.reason)) from None
        raise SinConexion(str(error.reason)) from None
    except OSError as error:
        raise SinConexion(str(error)) from None

    try:
        return estado, json.loads(crudo) if crudo else None
    except ValueError:
        return estado, None


def _sin_docker(error):
    """Genera un error 503 SERVICIO_NO_DISPONIBLE protegiendo detalles internos de Docker."""
    return ErrorApi(
        TipoError.SERVICIO_NO_DISPONIBLE,
        "el servicio no puede ejecutar tareas en este momento",
        interno=f"Docker: {error}",
    )


class Lanzador:
    """Administra la interacción con el daemon de Docker y el ciclo de vida de los contenedores de tareas."""

    def __init__(
        self,
        config,
        logger,
        fabrica_docker=docker.from_env,
        http=http_json,
        reloj=time.monotonic,
        dormir=time.sleep,
    ):
        """Inicializa el lanzador con inyección de dependencias para máxima testabilidad."""
        self._config = config
        self._log = logger
        self._fabrica = fabrica_docker
        self._http = http
        self._reloj = reloj
        self._dormir = dormir
        self._cliente = None  # Inicialización perezosa (lazy) de la conexión Docker

    def _docker(self):
        """Obtiene la instancia del cliente Docker SDK conectándose de forma perezosa (Lazy Loading).

        ¿Por qué lazy?
        Si el daemon de Docker no está listo al momento de iniciar el servidor, la API HTTP
        levanta de todos modos y reporta el estado degradado a través de su endpoint `/health`
        en lugar de crashearse en el arranque.
        """
        if self._cliente is None:
            try:
                self._cliente = self._fabrica()
            except _SIN_DOCKER as error:
                raise _sin_docker(error) from None
        return self._cliente

    def disponible(self) -> bool:
        """Verifica la conectividad activa con el daemon de Docker mediante `ping()`.

        Utilizado por el endpoint `/health` para determinar si el nodo está saludable.
        """
        try:
            return bool(self._docker().ping())
        except (ErrorApi, *_SIN_DOCKER):
            return False

    def preparar_imagen(self, imagen):
        """Garantiza la presencia local de la imagen Docker antes de su ejecución.

        Si la imagen no está en el host, la descarga desde el Registry con las credenciales configuradas.
        Retorna:
            Conjunto (`set`) con los digests SHA-256 de la imagen para validación contra la lista blanca.
        """
        local = self._asegurar_imagen(self._docker(), imagen)
        return {d.partition("@")[2] for d in local.attrs.get("RepoDigests") or []}

    def ejecutarTareaRemota(self, imagen, calculo, parametros, datos):  # noqa: N802 (nombre del enunciado)
        """Ejecuta una tarea computacional en un contenedor efímero aislado.

        Fases del Flujo de Ejecución:
        1. Asegurar la imagen en el host (local o pull).
        2. `containers.create()`: Reserva el contenedor en la red de tareas sin iniciarlo.
        3. `contenedor.start()`: Inicia el proceso en el contenedor.
           (Separar create de start garantiza que si start falla, tengamos la referencia para borrarlo).
        4. `_esperar_listo()`: Sondeo activo al `/health` de la tarea con detección de terminación temprana.
        5. `_enviar()`: Despacho HTTP del cálculo a `POST /ejecutarTarea`.
        6. `finally: remove(force=True)`: Destrucción inmediata del contenedor para evitar fugas.
        """
        cliente = self._docker()
        local = self._asegurar_imagen(cliente, imagen)
        contenedor = None
        try:
            inicio = time.perf_counter()

            # Fase 1: Creación del contenedor conectado a la red privada bridge
            contenedor = cliente.containers.create(
                imagen.referencia,
                network=self._config.red_tareas,
            )

            # Fase 2: Arranque del contenedor
            contenedor.start()

            # Fase 3: Espera activa hasta que el servidor HTTP interno responda
            base = self._esperar_listo(contenedor)

            # Fase 4: Envío del trabajo y recepción del resultado computado
            resultado = self._enviar(base, calculo, parametros, datos)

            # Retorno estructurado con métricas de duración de ejecución
            return Ejecucion(
                resultado,
                str(contenedor.id)[:12],
                local.id,
                _digest_ejecutado(imagen, local),
                round(time.perf_counter() - inicio, 4),
            )
        except APIError as error:
            raise ErrorApi(
                TipoError.TAREA_FALLIDA,
                "no se pudo levantar el contenedor de la tarea",
                interno=str(error),
            ) from None
        except _SIN_DOCKER as error:
            raise _sin_docker(error) from None
        finally:
            # Limpieza crítica obligatoria: siempre se elimina el contenedor, haya tenido éxito o fallado
            if contenedor is not None:
                try:
                    contenedor.remove(force=True)
                except _SIN_DOCKER as error:
                    self._log.warning("no se pudo borrar el contenedor %s: %s", contenedor.id, error)

    def _asegurar_imagen(self, cliente, imagen):
        """Verifica la existencia local de la imagen o efectúa el `docker pull` autenticado."""
        # 1. Búsqueda en el cache local de imágenes del daemon
        try:
            return cliente.images.get(imagen.referencia)
        except NotFound:
            pass  # La imagen no está en el host; se procede a descargarla
        except _SIN_DOCKER as error:
            raise _sin_docker(error) from None

        # 2. Configuración de credenciales de Docker Hub (si coinciden con el registry de la imagen)
        credenciales = None
        if (
            self._config.registry_usuario
            and self._config.registry_token
            and imagen.registry == self._config.registry
        ):
            credenciales = {
                "username": self._config.registry_usuario,
                "password": self._config.registry_token,
            }

        self._log.info(
            "descargando %s (autenticado: %s)",
            imagen.referencia,
            "sí" if credenciales else "no",
        )

        # 3. Descarga de la imagen desde el registry remoto
        try:
            return cliente.images.pull(imagen.nombre, tag=imagen.version, auth_config=credenciales)
        except NotFound as error:
            raise ErrorApi(
                TipoError.IMAGEN_INEXISTENTE,
                f"la imagen {imagen.referencia} no existe o no es accesible",
                interno=str(error),
            ) from None
        except APIError as error:
            raise ErrorApi(
                TipoError.REGISTRY_NO_DISPONIBLE,
                "no se pudo descargar la imagen",
                interno=str(error),
            ) from None
        except _SIN_DOCKER as error:
            raise _sin_docker(error) from None

    def _ip(self, contenedor) -> str:
        """Obtiene la dirección IP privada asignada al contenedor dentro de la red Docker de tareas.

        ¿Por qué usar la IP en vez del hostname/nombre del contenedor?
        - Si el servidor de tareas se corre directamente en el host (durante desarrollo local en Linux),
          la resolución DNS interna de Docker no está activa para procesos fuera de la red.
        - Conectarse directamente a la IP del contenedor en la interfaz bridge funciona en ambos escenarios.
        """
        return contenedor.attrs["NetworkSettings"]["Networks"][self._config.red_tareas]["IPAddress"]

    def _esperar_listo(self, contenedor) -> str:
        """Sondea activamente `GET /health` hasta que el microservicio de la tarea esté listo.

        Optimizaciones clave:
        1. Detección de Terminación Prematura (Fast-Fail):
           Si el contenedor crashea inmediatamente (status 'exited' o 'dead'), significa que
           la imagen no es un servicio HTTP válido (ej. un script batch). Se aborta de inmediato
           sin esperar innecesariamente los 30 segundos de timeout.
        2. Sondeo en intervalos de 200 ms:
           En cuanto el servidor levanta (típicamente 100-300 ms en Python), se retorna la URL base
           minimizando la latencia global del pedido.
        """
        limite = self._reloj() + self._config.timeout_arranque
        while self._reloj() < limite:
            contenedor.reload()
            # Si el contenedor terminó prematuramente, abortar con error
            if contenedor.status in ("exited", "dead"):
                raise ErrorApi(
                    TipoError.TAREA_FALLIDA,
                    "el contenedor terminó antes de quedar listo: la imagen no es un "
                    f"servicio tarea (HTTP en el puerto {self._config.tarea_puerto})",
                    interno=f"código de salida {contenedor.attrs['State']['ExitCode']}",
                )

            base = f"http://{self._ip(contenedor)}:{self._config.tarea_puerto}"
            try:
                estado, _ = self._http("GET", base + "/health", timeout=1.0)
                if estado == 200:
                    return base
            except (SinConexion, Vencido):
                pass  # El puerto aún no está abierto; reintentar

            self._dormir(0.2)

        raise ErrorApi(
            TipoError.TAREA_SIN_RESPUESTA,
            f"la tarea no estuvo lista en {self._config.timeout_arranque:g} s",
        )

    def _enviar(self, base: str, calculo: str, parametros: dict, datos: dict) -> Any:
        """Envía el trabajo a `POST /ejecutarTarea` en el contenedor y desempaqueta la respuesta."""
        cuerpo = {"calculo": calculo, "parametros": parametros, "datos": datos}
        try:
            estado, respuesta = self._http(
                "POST",
                base + "/ejecutarTarea",
                cuerpo,
                timeout=self._config.timeout_ejecucion,
            )
        except Vencido:
            raise ErrorApi(
                TipoError.TAREA_SIN_RESPUESTA,
                f"la tarea no respondió en {self._config.timeout_ejecucion:g} s",
            ) from None
        except SinConexion as error:
            raise ErrorApi(
                TipoError.TAREA_FALLIDA,
                "se perdió la conexión con la tarea",
                interno=str(error),
            ) from None

        # La tarea responde con el mismo sobre contractual que el servidor (contrato §5)
        contenido = respuesta.get("contenido") if isinstance(respuesta, dict) else None

        # Caso de Éxito: HTTP 200 con resultado presente
        if estado == 200 and isinstance(contenido, dict) and "resultado" in contenido:
            return contenido["resultado"]

        # Caso de Rechazo por la Tarea: HTTP 4xx (ej. división por cero, parámetros inválidos)
        if 400 <= estado < 500:
            error = contenido.get("error") if isinstance(contenido, dict) else None
            mensaje = error.get("mensaje") if isinstance(error, dict) else None
            raise ErrorApi(
                TipoError.TAREA_RECHAZADA,
                mensaje or "la tarea rechazó los parámetros",
                interno=f"HTTP {estado}",
            )

        # Caso de Falla Inesperada: HTTP 5xx o JSON mal estructurado
        raise ErrorApi(
            TipoError.TAREA_FALLIDA,
            "la tarea falló o devolvió una respuesta inválida",
            interno=f"HTTP {estado}, cuerpo={str(respuesta)[:300]}",
        )
