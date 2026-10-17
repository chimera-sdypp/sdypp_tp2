"""Dobles de prueba: un Docker, un HTTP y un reloj falsos, para probar sin daemon."""

import logging

from docker.errors import APIError, ImageNotFound

from app.lanzador import Ejecucion


class RelojFalso:
    def __init__(self):
        self.ahora = 1000.0

    def __call__(self):
        return self.ahora

    def dormir(self, segundos):
        self.ahora += segundos


class ContenedorFalso:
    def __init__(self, imagen, network, falla_start=False, termina=False, **kwargs):
        self.id = "abc123"
        self.imagen = imagen
        self.network = network
        self.kwargs = kwargs
        self.falla_start = falla_start
        # termina: la imagen no es un servicio tarea y el proceso sale enseguida.
        self.status = "exited" if termina else "running"
        self.attrs = {"NetworkSettings": {"Networks": {network: {"IPAddress": "172.30.0.5"}}},
                      "State": {"ExitCode": 3 if termina else 0}}
        self.borrado = False

    def start(self):
        if self.falla_start:
            raise APIError("no se pudo iniciar")

    def reload(self):
        pass

    def remove(self, force=False):
        self.borrado = True


def ejecucion(resultado):
    """Lo que devuelve `Lanzador.ejecutarTareaRemota()`."""
    return Ejecucion(resultado, "c0ffee123456", "sha256:" + "a" * 64, "sha256:" + "b" * 64, 0.01)


class ImagenFalsa:
    def __init__(self, digests):
        self.id = "sha256:" + "a" * 64
        self.attrs = {"RepoDigests": [f"cerberusdistribuido/tarea@{d}" for d in digests]}


class DockerFalso:
    """Hace de `docker.DockerClient`: images.get/pull, containers.create y ping."""

    def __init__(self, presentes=(), error_pull=None, falla_start=False, termina=False, digests=()):
        self.presentes = set(presentes)
        self.error_pull = error_pull
        self.falla_start = falla_start
        self.termina = termina
        self.digests = digests
        self.pulls = []
        self.creados = []
        self.images = self
        self.containers = self

    def ping(self):
        return True

    def get(self, referencia):
        if referencia not in self.presentes:
            raise ImageNotFound(f"No such image: {referencia}")
        return ImagenFalsa(self.digests)

    def pull(self, nombre, tag=None, auth_config=None):
        self.pulls.append({"nombre": nombre, "tag": tag, "auth": auth_config})
        if self.error_pull:
            raise self.error_pull
        return ImagenFalsa(self.digests)

    def create(self, imagen, network=None, **kwargs):
        contenedor = ContenedorFalso(imagen, network, self.falla_start, self.termina, **kwargs)
        self.creados.append(contenedor)
        return contenedor


def sobre(codigo, contenido):
    return codigo, {"codigo": codigo, "contenido": contenido}


class HttpFalso:
    """Responde según el método; una respuesta puede ser una excepción o una lista (una por llamada)."""

    def __init__(self, salud=sobre(200, {"tarea": "ok"}), tarea=sobre(200, {"resultado": 7})):
        self.respuestas = {"GET": salud, "POST": tarea}
        self.pedidos = []

    def __call__(self, metodo, url, cuerpo=None, timeout=5.0):
        self.pedidos.append((metodo, url, cuerpo, timeout))
        respuesta = self.respuestas[metodo]
        if isinstance(respuesta, list):
            respuesta = respuesta.pop(0) if len(respuesta) > 1 else respuesta[0]
        if isinstance(respuesta, BaseException):
            raise respuesta
        return respuesta


def logger_silencioso():
    logger = logging.getLogger("tp2.tests")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


