"""`ejecutarTareaRemota()`: levanta el servicio tarea como contenedor, le pasa el
trabajo, espera el resultado y borra el contenedor.

Se usa el SDK de Docker y no el CLI: le habla directo al socket, así la imagen
del servidor no necesita traer el binario de `docker`.
"""

import json
import time
import urllib.error
import urllib.request

import docker
from docker.errors import APIError, DockerException, NotFound
from requests.exceptions import RequestException

from app.errores import ErrorApi, TipoError

# Errores de "no llego al daemon": socket ausente, sin permiso, daemon caído.
_SIN_DOCKER = (DockerException, RequestException, OSError)


class SinConexion(Exception):
    """No se pudo conectar con el contenedor tarea."""


class Vencido(Exception):
    """El contenedor tarea no contestó a tiempo."""


def http_json(metodo, url, cuerpo=None, timeout=5.0):
    """Pedido HTTP con cuerpo JSON → (código, objeto o None si no era JSON)."""
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
    return ErrorApi(TipoError.SERVICIO_NO_DISPONIBLE,
                    "el servicio no puede ejecutar tareas en este momento",
                    interno=f"Docker: {error}")


class Lanzador:
    def __init__(self, config, logger, fabrica_docker=docker.from_env, http=http_json,
                 reloj=time.monotonic, dormir=time.sleep):
        self._config = config
        self._log = logger
        self._fabrica = fabrica_docker
        self._http = http
        self._reloj = reloj
        self._dormir = dormir
        self._cliente = None

    def _docker(self):
        """Se conecta recién cuando hace falta: si Docker no está al arrancar, el
        servidor levanta igual y su /health lo informa."""
        if self._cliente is None:
            try:
                self._cliente = self._fabrica()
            except _SIN_DOCKER as error:
                raise _sin_docker(error) from None
        return self._cliente

    def disponible(self):
        try:
            return bool(self._docker().ping())
        except (ErrorApi, *_SIN_DOCKER):
            return False

    def ejecutarTareaRemota(self, imagen, calculo, parametros, datos):  # noqa: N802 (nombre del enunciado)
        cliente = self._docker()
        self._asegurar_imagen(cliente, imagen)
        contenedor = None
        try:
            # create + start por separado: si falla el start, `containers.run()`
            # deja el contenedor creado y no habría referencia para borrarlo.
            contenedor = cliente.containers.create(imagen.referencia, network=self._config.red_tareas)
            contenedor.start()
            base = f"http://{self._ip(contenedor)}:{self._config.tarea_puerto}"
            self._esperar_listo(base)
            return self._enviar(base, calculo, parametros, datos)
        except APIError as error:
            raise ErrorApi(TipoError.TAREA_FALLIDA, "no se pudo levantar el contenedor de la tarea",
                           interno=str(error)) from None
        except _SIN_DOCKER as error:
            raise _sin_docker(error) from None
        finally:
            # Siempre, salga bien o mal: si no, quedan contenedores tarea colgados.
            if contenedor is not None:
                try:
                    contenedor.remove(force=True)
                except _SIN_DOCKER as error:
                    self._log.warning("no se pudo borrar el contenedor %s: %s", contenedor.id, error)

    def _asegurar_imagen(self, cliente, imagen):
        """Descarga la imagen si no está en el host, con las credenciales del
        servidor (nunca del cliente), y sólo si la imagen es de su registry."""
        try:
            cliente.images.get(imagen.referencia)
            return
        except NotFound:
            pass
        except _SIN_DOCKER as error:
            raise _sin_docker(error) from None

        credenciales = None
        if self._config.registry_usuario and imagen.registry == self._config.registry:
            credenciales = {"username": self._config.registry_usuario,
                            "password": self._config.registry_token}
        self._log.info("descargando %s (autenticado: %s)", imagen.referencia,
                       "sí" if credenciales else "no")
        try:
            cliente.images.pull(imagen.nombre, tag=imagen.version, auth_config=credenciales)
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
        contenedor.reload()
        return contenedor.attrs["NetworkSettings"]["Networks"][self._config.red_tareas]["IPAddress"]

    def _esperar_listo(self, base):
        """En vez de un sleep fijo: consulta el /health de la tarea hasta que conteste."""
        limite = self._reloj() + self._config.timeout_arranque
        while self._reloj() < limite:
            try:
                estado, _ = self._http("GET", base + "/health", timeout=1.0)
                if estado == 200:
                    return
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
