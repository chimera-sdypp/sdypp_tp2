"""Módulo Lanzador (Hit 3): Orquestador de Contenedores con Etiquetado de Nodos y Limpieza de Huérfanos.

En el contexto de un cluster distribuido (Hit 3):
1. Aislamiento por Contenedor:
   - Cada tarea se ejecuta dentro de un micro-contenedor Docker efímero conectado a la red privada `red_tareas`.
2. Etiquetado de Propiedad (Ownership Labeling):
   - Al crear el contenedor se le inyecta la etiqueta `tp2.hit3.nodo=<nodo_id>`.
   - Si un nodo sufre una caída abrupta (SIGKILL, fallo de máquina o apagado forzoso), el bloque `finally`
     no llega a ejecutarse y el contenedor Docker queda colgado (huérfano).
3. Recuperación Fail-Clean al Reiniciar (`limpiar_huerfanos`):
   - Cuando el nodo arranca, antes de aceptar tareas, consulta a Docker por todos los contenedores
     que posean su etiqueta y los elimina forzosamente (`remove(force=True)`).
   - De esta manera cada nodo limpia estrictamente sus propios restos sin interferir con las tareas
     de otros nodos del cluster que comparten el daemon de Docker.
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

# Clave de metadatos Docker (label) para identificar qué nodo del cluster creó el contenedor.
# Permite a cada nodo reclamar y limpiar únicamente sus contenedores tras un reinicio inesperado.
ETIQUETA_NODO = "tp2.hit3.nodo"

# Excepciones que representan imposibilidad de conectar con el socket de Docker (/var/run/docker.sock)
_SIN_DOCKER = (DockerException, RequestException, OSError)


class Ejecucion(NamedTuple):
    """Estructura con los metadatos de la tarea ejecutada en el contenedor."""
    resultado: Any
    contenedor: str     # id corto (12 caracteres) del contenedor ejecutor
    imagen_id: str      # sha256 de la imagen local
    digest: Any         # sha256 del repositorio remoto (RepoDigests)
    ejecucion_s: float  # latencia neta de cómputo en el contenedor


def _digest_ejecutado(imagen, local):
    """Obtiene el digest criptográfico inmutable de la imagen en el registry oficial."""
    candidatos = [d.partition("@") for d in local.attrs.get("RepoDigests") or []]
    propios = [digest for repo, _, digest in candidatos
               if repo.removeprefix("docker.io/") == imagen.nombre.removeprefix("docker.io/")]
    elegidos = propios or [digest for _, _, digest in candidatos]
    return min(elegidos) if elegidos else None


class SinConexion(Exception):
    """No se pudo establecer comunicación TCP con el microservicio tarea."""


class Vencido(Exception):
    """El contenedor tarea no completó el procesamiento dentro del tiempo límite (timeout)."""


def http_json(metodo, url, cuerpo=None, timeout=5.0):
    """Cliente HTTP síncrono para interactuar con el contenedor tarea."""
    datos = None if cuerpo is None else json.dumps(cuerpo).encode("utf-8")
    pedido = urllib.request.Request(url, data=datos, method=metodo,
                                    headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(pedido, timeout=timeout) as respuesta:
            estado, crudo = respuesta.status, respuesta.read()
    except urllib.error.HTTPError as error:
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
    """Constructor estandarizado de error 503 cuando Docker no responde."""
    return ErrorApi(TipoError.SERVICIO_NO_DISPONIBLE,
                    "el servicio no puede ejecutar tareas en este momento",
                    interno=f"Docker: {error}")


class Lanzador:
    """Administra el ciclo de vida de los contenedores Docker en el nodo del cluster."""

    def __init__(self, config, logger, fabrica_docker=docker.from_env, http=http_json,
                 reloj=time.monotonic, dormir=time.sleep):
        """Inicializa el lanzador con inyección de dependencias para tests."""
        self._config = config
        self._log = logger
        self._fabrica = fabrica_docker
        self._http = http
        self._reloj = reloj
        self._dormir = dormir
        self._cliente = None

    def _docker(self):
        """Inicialización perezosa (lazy) de la conexión con el daemon de Docker."""
        if self._cliente is None:
            try:
                self._cliente = self._fabrica()
            except _SIN_DOCKER as error:
                raise _sin_docker(error) from None
        return self._cliente

    def disponible(self):
        """Verifica la respuesta al ping del daemon de Docker."""
        try:
            return bool(self._docker().ping())
        except (ErrorApi, *_SIN_DOCKER):
            return False

    def preparar_imagen(self, imagen):
        """Verifica que la imagen exista localmente o la descarga del registry autorizado."""
        local = self._asegurar_imagen(self._docker(), imagen)
        return {d.partition("@")[2] for d in local.attrs.get("RepoDigests") or []}

    def ejecutarTareaRemota(self, imagen, calculo, parametros, datos):  # noqa: N802 (nombre del enunciado)
        """Instancia un contenedor efímero con la etiqueta del nodo, ejecuta la tarea y lo destruye."""
        cliente = self._docker()
        local = self._asegurar_imagen(cliente, imagen)
        contenedor = None
        try:
            inicio = time.perf_counter()
            # Separamos create y start para tener la referencia del contenedor en caso de fallo
            contenedor = cliente.containers.create(
                imagen.referencia,
                network=self._config.red_tareas,
                labels={ETIQUETA_NODO: str(self._config.nodo_id)}
            )
            contenedor.start()
            base = self._esperar_listo(contenedor)
            resultado = self._enviar(base, calculo, parametros, datos)
            return Ejecucion(resultado, str(contenedor.id)[:12], local.id, _digest_ejecutado(imagen, local),
                             round(time.perf_counter() - inicio, 4))
        except APIError as error:
            raise ErrorApi(TipoError.TAREA_FALLIDA, "no se pudo levantar el contenedor de la tarea",
                           interno=str(error)) from None
        except _SIN_DOCKER as error:
            raise _sin_docker(error) from None
        finally:
            # Garantía de eliminación en bloque finally
            if contenedor is not None:
                try:
                    contenedor.remove(force=True)
                except _SIN_DOCKER as error:
                    self._log.warning("no se pudo borrar el contenedor %s: %s", contenedor.id, error)

    def limpiar_huerfanos(self):
        """Identifica y destruye los contenedores huérfanos generados por este nodo en ejecuciones previas."""
        filtro = {"label": f"{ETIQUETA_NODO}={self._config.nodo_id}"}
        try:
            huerfanos = self._docker().containers.list(all=True, filters=filtro)
            for contenedor in huerfanos:
                contenedor.remove(force=True)
        except (ErrorApi, *_SIN_DOCKER) as error:
            self._log.warning("no se pudieron limpiar los contenedores huérfanos: %s", error)
            return 0
        if huerfanos:
            self._log.info("se borraron %d contenedores tarea huérfanos", len(huerfanos))
        return len(huerfanos)


    def _asegurar_imagen(self, cliente, imagen):
        """Descarga la imagen si no está en el host, con las credenciales del
        servidor (nunca del cliente), y sólo si la imagen es de su registry."""
        try:
            return cliente.images.get(imagen.referencia)
        except NotFound:
            pass
        except _SIN_DOCKER as error:
            raise _sin_docker(error) from None

        credenciales = None
        if (self._config.registry_usuario and self._config.registry_token
                and imagen.registry == self._config.registry):
            credenciales = {"username": self._config.registry_usuario,
                            "password": self._config.registry_token}
        self._log.info("descargando %s (autenticado: %s)", imagen.referencia,
                       "sí" if credenciales else "no")
        try:
            return cliente.images.pull(imagen.nombre, tag=imagen.version, auth_config=credenciales)
        except NotFound as error:
            raise ErrorApi(TipoError.IMAGEN_INEXISTENTE,
                           f"la imagen {imagen.referencia} no existe o no es accesible",
                           interno=str(error)) from None
        except APIError as error:
            raise ErrorApi(TipoError.REGISTRY_NO_DISPONIBLE, "no se pudo descargar la imagen",
                           interno=str(error)) from None
        except _SIN_DOCKER as error:
            raise _sin_docker(error) from None

    def _ip(self, contenedor):
        """IP en la red de tareas. La IP y no el nombre: el nombre sólo resuelve
        desde adentro de la red de Docker, y así el servidor anda también suelto
        en el host (desarrollo en Linux)."""
        return contenedor.attrs["NetworkSettings"]["Networks"][self._config.red_tareas]["IPAddress"]

    def _esperar_listo(self, contenedor):
        """En vez de un sleep fijo: consulta el /health de la tarea hasta que conteste
        → URL base de la tarea. Si el contenedor termina antes, la imagen no es un
        servicio tarea: falla en el momento, sin esperar el timeout."""
        limite = self._reloj() + self._config.timeout_arranque
        while self._reloj() < limite:
            contenedor.reload()
            if contenedor.status in ("exited", "dead"):
                raise ErrorApi(TipoError.TAREA_FALLIDA,
                               "el contenedor terminó antes de quedar listo: la imagen no es un "
                               f"servicio tarea (HTTP en el puerto {self._config.tarea_puerto})",
                               interno=f"código de salida {contenedor.attrs['State']['ExitCode']}")
            base = f"http://{self._ip(contenedor)}:{self._config.tarea_puerto}"
            try:
                estado, _ = self._http("GET", base + "/health", timeout=1.0)
                if estado == 200:
                    return base
            except (SinConexion, Vencido):
                pass
            self._dormir(0.2)
        raise ErrorApi(TipoError.TAREA_SIN_RESPUESTA,
                       f"la tarea no estuvo lista en {self._config.timeout_arranque:g} s")

    def _enviar(self, base, calculo, parametros, datos):
        cuerpo = {"calculo": calculo, "parametros": parametros, "datos": datos}
        try:
            estado, respuesta = self._http("POST", base + "/ejecutarTarea", cuerpo,
                                           timeout=self._config.timeout_ejecucion)
        except Vencido:
            raise ErrorApi(TipoError.TAREA_SIN_RESPUESTA,
                           f"la tarea no respondió en {self._config.timeout_ejecucion:g} s") from None
        except SinConexion as error:
            raise ErrorApi(TipoError.TAREA_FALLIDA, "se perdió la conexión con la tarea",
                           interno=str(error)) from None

        # La tarea responde con el mismo sobre que el servidor (contrato §5).
        contenido = respuesta.get("contenido") if isinstance(respuesta, dict) else None
        if estado == 200 and isinstance(contenido, dict) and "resultado" in contenido:
            return contenido["resultado"]
        if 400 <= estado < 500:
            error = contenido.get("error") if isinstance(contenido, dict) else None
            mensaje = error.get("mensaje") if isinstance(error, dict) else None
            raise ErrorApi(TipoError.TAREA_RECHAZADA, mensaje or "la tarea rechazó los parámetros",
                           interno=f"HTTP {estado}")
        raise ErrorApi(TipoError.TAREA_FALLIDA, "la tarea falló o devolvió una respuesta inválida",
                       interno=f"HTTP {estado}, cuerpo={str(respuesta)[:300]}")
