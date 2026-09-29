import pytest

from app.imagenes import ImagenInvalida, lista_blanca, parsear

DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize("texto, nombre, tag, digest", [
    ("cerberus/tarea:1.0.0", "docker.io/cerberus/tarea", "1.0.0", None),
    ("docker.io/cerberus/tarea:1.0", "docker.io/cerberus/tarea", "1.0", None),
    ("python:3.14-slim", "docker.io/library/python", "3.14-slim", None),
    ("ghcr.io/mnomico/tarea:v2", "ghcr.io/mnomico/tarea", "v2", None),
    ("localhost:5000/tarea:1", "localhost:5000/tarea", "1", None),
    (f"cerberus/tarea@{DIGEST}", "docker.io/cerberus/tarea", None, DIGEST),
])
def test_referencias_validas_se_normalizan(texto, nombre, tag, digest):
    imagen = parsear(texto)
    assert (imagen.nombre, imagen.tag, imagen.digest) == (nombre, tag, digest)


def test_version_prefiere_el_digest():
    assert parsear(f"cerberus/tarea:1.0@{DIGEST}").version == DIGEST
    assert parsear("cerberus/tarea:1.0").version == "1.0"


@pytest.mark.parametrize("texto, motivo", [
    ("cerberus/tarea", "latest implícito"),
    ("cerberus/tarea:latest", "latest"),
    ("localhost:5000/tarea", "latest implícito"),
    ("Cerberus/Tarea:1.0", "componente inválido"),
    ("cerberus/tarea:", "tag inválido"),
    ("cerberus/tarea@sha256:corto", "digest"),
    ("cerberus//tarea:1", "componente inválido"),
    (":1.0", "falta el nombre"),
    ("cerberus/tarea:1.0; rm -rf /", "inválido"),
    ("a" * 300 + ":1", "255"),
])
def test_referencias_invalidas(texto, motivo):
    with pytest.raises(ImagenInvalida, match=motivo):
        parsear(texto)


def test_lista_blanca_normaliza_los_nombres():
    permitidas = lista_blanca(["cerberus/tarea", "python"])
    assert parsear("docker.io/cerberus/tarea:1.0").nombre in permitidas
    assert parsear("python:3.14").nombre in permitidas
    assert parsear("cerberus/otra:1.0").nombre not in permitidas
