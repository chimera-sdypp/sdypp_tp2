"""Referencias de imágenes Docker y lista blanca (Requisito R2).

El cliente elige qué imagen corre el servidor dinámicamente por parámetro.
El servidor la ejecuta con acceso al daemon de Docker; por ende, sin una validación
estricta y una lista blanca, cualquiera podría ejecutar código malicioso en el host.

Niveles de validación implementados:
1. Validación sintáctica por regex (formato OCI: usuario/repo:tag semántico o digest SHA-256).
2. Rechazo explícito de tags mutables como 'latest' para garantizar reproducibilidad.
3. Verificación contra lista blanca (TP2_IMAGENES_PERMITIDAS) con soporte de digests inmutables.
"""

import re
from dataclasses import dataclass

REGISTRY_POR_DEFECTO = "docker.io"
LARGO_MAXIMO = 255

# ------------------------------------------------------------------------------
# 1. Gramática y Expresiones Regulares (OCI / Distribution Reference)
# ------------------------------------------------------------------------------
_COMPONENTE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$")
_TAG = re.compile(r"^[\w][\w.-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


class ImagenInvalida(ValueError):
    """La referencia no respeta el formato, las reglas de seguridad o el catálogo del servidor."""


# ------------------------------------------------------------------------------
# 2. Modelo de Dominio: Estructura Inmutable de una Imagen
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Imagen:
    registry: str
    repositorio: str
    tag: str | None = None
    digest: str | None = None

    @property
    def nombre(self) -> str:
        """Nombre normalizado con registry (ej. 'docker.io/cerberusdistribuido/tarea')."""
        return f"{self.registry}/{self.repositorio}"

    @property
    def version(self) -> str | None:
        """Lo que se le pasa a `docker pull` como tag: el digest inmutable tiene prioridad."""
        return self.digest or self.tag

    @property
    def referencia(self) -> str:
        """Referencia canónica completa (ej. 'docker.io/repo:1.0.0@sha256:...')."""
        texto = self.nombre
        if self.tag:
            texto += f":{self.tag}"
        if self.digest:
            texto += f"@{self.digest}"
        return texto


# ------------------------------------------------------------------------------
# 3. Funciones de Parseo y Normalización Léxica
# ------------------------------------------------------------------------------
def _normalizar_nombre(nombre: str) -> tuple[str, str]:
    """Normaliza nombres de imagen al estándar canónico de Docker.
    
    Ejemplos:
      `cerberusdistribuido/tarea` -> ("docker.io", "cerberusdistribuido/tarea")
      `python`                    -> ("docker.io", "library/python")
      `localhost:5000/tarea`      -> ("localhost:5000", "tarea")
    """
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


def parsear(texto: str) -> Imagen:
    """Convierte un string como `cerberusdistribuido/tarea:1.0.0` en una `Imagen`.
    
    Reglas contractuales estrictas (Requisito R2):
    1. Longitud máxima acotada (evita ataques por saturación de payload).
    2. Digest criptográfico inmutable verificado por regex SHA-256.
    3. Separación segura de puerto de registry vs. tag semántico.
    4. Normalización canónica (agrega 'docker.io' y 'library' si corresponden).
    5. Rechazo estricto de 'latest': garantiza reproducibilidad determinística.
    """
    # Paso 1: Protección de longitud máxima (previene ataques de denegación de servicio)
    if len(texto) > LARGO_MAXIMO:
        raise ImagenInvalida(f"supera los {LARGO_MAXIMO} caracteres")

    # Paso 2: Separar y validar el digest criptográfico (@sha256:...) si fue provisto
    nombre, arroba, digest = texto.partition("@")
    if arroba and not _DIGEST.match(digest):
        raise ImagenInvalida("el digest tiene que ser sha256:<64 hex>")

    # Paso 3: Extraer el tag de versión (:tag)
    # Se usa rpartition para no confundir el puerto del registry (ej: localhost:5000/tarea) con el tag
    tag = None
    base, dos_puntos, posible_tag = nombre.rpartition(":")
    if dos_puntos and "/" not in posible_tag:
        nombre, tag = base, posible_tag
        if not _TAG.match(tag):
            raise ImagenInvalida(f"tag inválido: {tag!r}")

    # Paso 4: Validar presencia de nombre y normalizar componentes OCI
    if not nombre:
        raise ImagenInvalida("falta el nombre de la imagen")
    registry, repositorio = _normalizar_nombre(nombre)

    # Paso 5: Reglas de inmutabilidad y reproducibilidad (Requisito R2)
    # - No se permite omitir versión (no se asume 'latest' implícito)
    if not tag and not digest:
        raise ImagenInvalida("falta el tag o el digest (no se acepta latest implícito)")
    # - Rechazo explícito de 'latest': en sistemas distribuidos el código debe ser determinístico
    if tag == "latest":
        raise ImagenInvalida("no se acepta el tag latest: usar una versión fija o un digest")

    # Paso 6: Construcción de la instancia inmutable de dominio
    return Imagen(registry, repositorio, tag, digest or None)


# ------------------------------------------------------------------------------
# 4. Catálogo y Validación contra Lista Blanca
# ------------------------------------------------------------------------------
def lista_blanca(entradas: tuple | list) -> dict[str, set[str]]:
    """Construye el catálogo permitido: `repo` o `repo@sha256:...` -> {nombre: digests fijados}.

    - Un repositorio sin digest acepta cualquier versión fija de ese repositorio.
    - Con uno o más digests fijados, sólo esos contenidos exactos son admitidos:
      si alguien vuelve a subir el mismo tag con otro código malicioso, el servidor
      lo rechaza impidiendo ataques de sustitución de tags.
    """
    permitidas = {}
    for entrada in entradas:
        nombre, arroba, digest = entrada.partition("@")
        if arroba and not _DIGEST.match(digest):
            raise ImagenInvalida(f"{entrada}: el digest tiene que ser sha256:<64 hex>")
        fijados = permitidas.setdefault("/".join(_normalizar_nombre(nombre)), set())
        if digest:
            fijados.add(digest)
    return permitidas
