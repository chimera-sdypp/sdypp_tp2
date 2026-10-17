"""El emisor de tokens de evidencias/ (sólo stdlib) tiene que producir lo que el servidor valida."""

import importlib.util
from pathlib import Path

import jwt
import pytest

from app.auth import autenticar, Identidad
from app.errores import ErrorApi

RUTA = Path(__file__).resolve().parents[3] / "evidencias" / "emitir_token.py"
SECRETO = "secreto-de-prueba-de-al-menos-32-caracteres"


@pytest.fixture(scope="module")
def emisor():
    especificacion = importlib.util.spec_from_file_location("emitir_token", RUTA)
    modulo = importlib.util.module_from_spec(especificacion)
    especificacion.loader.exec_module(modulo)
    return modulo


def test_el_servidor_acepta_lo_que_emite_la_herramienta(emisor):
    token = emisor.emitir(SECRETO, "ana", "usuario", 60)
    assert autenticar(f"Bearer {token}", SECRETO) == Identidad("ana", "usuario")
    assert jwt.decode(token, SECRETO, algorithms=["HS256"])["role"] == "usuario"


def test_un_token_vencido_o_con_otro_secreto_se_rechaza(emisor):
    for token in (emisor.emitir(SECRETO, "ana", "usuario", -5), emisor.emitir("otro" * 10, "ana", "usuario", 60)):
        with pytest.raises(ErrorApi):
            autenticar(f"Bearer {token}", SECRETO)
