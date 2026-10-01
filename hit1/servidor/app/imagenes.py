"""Referencias de imágenes Docker y lista blanca.

El cliente elige qué imagen corre el servidor, y el servidor la corre con acceso
al daemon de Docker: sin una lista blanca, cualquiera ejecutaría lo que quisiera
en el host. Sólo pasan los repositorios configurados; sin configurar, ninguno.

Los nombres se normalizan como lo hace Docker (`cerberusdistribuido/tarea` →
`docker.io/cerberusdistribuido/tarea`), para que la lista y Docker hablen de lo mismo.
"""

import re
from dataclasses import dataclass

REGISTRY_POR_DEFECTO = "docker.io"
LARGO_MAXIMO = 255

# Gramática de github.com/distribution/reference, simplificada.
_COMPONENTE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$")
_TAG = re.compile(r"^[\w][\w.-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


class ImagenInvalida(ValueError):
    """La referencia no respeta el formato o las reglas del servidor."""


@dataclass(frozen=True)
class Imagen:
    registry: str
    repositorio: str
    tag: str | None = None
    digest: str | None = None

    @property
    def nombre(self):
        return f"{self.registry}/{self.repositorio}"

    @property
    def version(self):
        """Lo que se le pasa a `docker pull` como tag: el digest gana si está."""
        return self.digest or self.tag

    @property
    def referencia(self):
        texto = self.nombre
        if self.tag:
            texto += f":{self.tag}"
        if self.digest:
            texto += f"@{self.digest}"
        return texto


def _normalizar_nombre(nombre):
    """`cerberusdistribuido/tarea` → ("docker.io", "cerberusdistribuido/tarea");
    `python` → ("docker.io", "library/python")."""
    partes = nombre.split("/")
    es_registry = "." in partes[0] or ":" in partes[0] or partes[0] == "localhost"
    if len(partes) > 1 and es_registry:
        registry, ruta = partes[0], partes[1:]
    else:
        registry, ruta = REGISTRY_POR_DEFECTO, partes
    if registry == REGISTRY_POR_DEFECTO and len(ruta) == 1:
        ruta = ["library", *ruta]
    for componente in ruta:
        if not _COMPONENTE.match(componente):
            raise ImagenInvalida(f"componente inválido {componente!r}: sólo minúsculas, dígitos y . _ -")
    return registry, "/".join(ruta)


def parsear(texto):
    """Convierte `cerberusdistribuido/tarea:1.0` en una `Imagen`, o lanza `ImagenInvalida`.

    Exige una versión fija (tag distinto de `latest`, o digest): con `latest` la
    misma petición puede correr código distinto de un día al otro.
    """
    if len(texto) > LARGO_MAXIMO:
        raise ImagenInvalida(f"supera los {LARGO_MAXIMO} caracteres")
    nombre, arroba, digest = texto.partition("@")
    if arroba and not _DIGEST.match(digest):
        raise ImagenInvalida("el digest tiene que ser sha256:<64 hex>")

    tag = None
    # El ':' del tag es el que viene después de la última '/': el anterior es el
    # puerto del registry (localhost:5000/tarea).
    base, dos_puntos, posible_tag = nombre.rpartition(":")
    if dos_puntos and "/" not in posible_tag:
        nombre, tag = base, posible_tag
        if not _TAG.match(tag):
            raise ImagenInvalida(f"tag inválido: {tag!r}")
    if not nombre:
        raise ImagenInvalida("falta el nombre de la imagen")
    registry, repositorio = _normalizar_nombre(nombre)

    if not tag and not digest:
        raise ImagenInvalida("falta el tag o el digest (no se acepta latest implícito)")
    if tag == "latest":
        raise ImagenInvalida("no se acepta el tag latest: usar una versión fija o un digest")
    return Imagen(registry, repositorio, tag, digest or None)


def lista_blanca(repositorios):
    """Nombres normalizados de los repositorios permitidos."""
    return {"/".join(_normalizar_nombre(r)) for r in repositorios}
