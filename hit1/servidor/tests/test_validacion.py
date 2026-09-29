import json

import pytest

from app.errores import ErrorApi, TipoError
from app.validacion import validar_solicitud

JSON = "application/json"
VALIDO = {"calculo": "suma", "parametros": {"a": 3, "b": 4}, "imagen": "cerberus/tarea:1.0"}


def _cuerpo(objeto):
    return json.dumps(objeto).encode()


def _error(cuerpo, content_type=JSON):
    with pytest.raises(ErrorApi) as info:
        validar_solicitud(cuerpo, content_type)
    return info.value


def _problemas(cuerpo):
    error = _error(cuerpo)
    assert error.tipo is TipoError.PAYLOAD_INVALIDO
    return {d["campo"]: d["problema"] for d in error.detalles}


def test_payload_valido():
    solicitud = validar_solicitud(_cuerpo(VALIDO), JSON)
    assert solicitud.calculo == "suma"
    assert solicitud.datos == {}


def test_content_type_con_charset_es_valido():
    validar_solicitud(_cuerpo(VALIDO), "application/json; charset=utf-8")


@pytest.mark.parametrize("content_type", [None, "", "text/plain", "application/x-www-form-urlencoded"])
def test_content_type_distinto_es_415(content_type):
    error = _error(_cuerpo(VALIDO), content_type)
    assert error.tipo is TipoError.TIPO_DE_CONTENIDO
    assert error.tipo.estado == 415


@pytest.mark.parametrize("cuerpo", [b"", b"   \n"])
def test_cuerpo_vacio_es_400(cuerpo):
    assert _error(cuerpo).tipo is TipoError.CUERPO_VACIO


@pytest.mark.parametrize("cuerpo, motivo", [
    (b'{"calculo": "suma",', "mal formado"),
    (b"\xff\xfe", "UTF-8"),
    (b'{"calculo": NaN}', "NaN"),
    (b'{"a": Infinity}', "Infinity"),
    (b'{"imagen": "a:1", "imagen": "b:1"}', "duplicadas en el JSON: imagen"),
    (b"[" * 100_000 + b"]" * 100_000, "anidado"),
])
def test_json_invalido_es_400(cuerpo, motivo):
    error = _error(cuerpo)
    assert error.tipo is TipoError.JSON_INVALIDO
    assert motivo in error.mensaje


@pytest.mark.parametrize("objeto", [[], [VALIDO], "texto", 42, None])
def test_cuerpo_que_no_es_objeto_es_422(objeto):
    error = _error(_cuerpo(objeto))
    assert error.tipo is TipoError.PAYLOAD_INVALIDO
    assert "objeto JSON" in error.mensaje


def test_campos_faltantes():
    assert _problemas(_cuerpo({})) == {
        "calculo": "campo obligatorio faltante",
        "parametros": "campo obligatorio faltante",
        "imagen": "campo obligatorio faltante",
    }


@pytest.mark.parametrize("campo, valor, problema", [
    ("calculo", None, "no puede ser null"),
    ("calculo", "", "no puede estar vacío"),
    ("calculo", 123, "tiene que ser un string, llegó un número"),
    ("calculo", "Suma", "formato inválido"),
    ("calculo", "suma; rm", "formato inválido"),
    ("calculo", "a" * 65, "64 caracteres"),
    ("parametros", None, "no puede ser null"),
    ("parametros", "a=1", "tiene que ser un objeto, llegó un string"),
    ("parametros", [1, 2], "tiene que ser un objeto, llegó un array"),
    ("datos", None, "no puede ser null"),
    ("imagen", "cerberus/tarea", "latest implícito"),
    ("imagen", "cerberus/tarea:latest", "latest"),
    ("imagen", 12.5, "tiene que ser un string, llegó un número"),
])
def test_campo_invalido_dice_exactamente_que_paso(campo, valor, problema):
    problemas = _problemas(_cuerpo({**VALIDO, campo: valor}))
    assert problema in problemas[campo]


def test_campo_de_mas_se_rechaza():
    problemas = _problemas(_cuerpo({**VALIDO, "parametro": {}}))
    assert problemas == {"parametro": "campo no permitido"}


def test_credenciales_en_el_payload_se_rechazan():
    """El enunciado prohíbe que viajen: el servidor ni siquiera las acepta."""
    problemas = _problemas(_cuerpo({**VALIDO, "usuario": "x", "password": "y"}))
    assert problemas == {"usuario": "campo no permitido", "password": "campo no permitido"}


def test_booleano_no_pasa_por_string():
    assert "llegó un booleano" in _problemas(_cuerpo({**VALIDO, "calculo": True}))["calculo"]
