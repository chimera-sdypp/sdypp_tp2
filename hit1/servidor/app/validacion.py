"""Validación del cuerpo de `POST /getRemoteTask`, antes de que entre al sistema.

El criterio es rechazar, nunca "arreglar": un `"123"` donde va un objeto no se
convierte, se devuelve un 422 que dice exactamente qué campo falló y por qué.

El JSON se lee a mano (no con el parser automático de FastAPI) para poder
distinguir cada caso con su código: cuerpo vacío, JSON roto, claves duplicadas y
`NaN` son 400; un payload bien formado pero inválido es 422.
"""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app import imagenes
from app.errores import ErrorApi, TipoError


class SolicitudTarea(BaseModel):
    # strict: sin conversiones implícitas. extra=forbid: un campo mal escrito
    # ("parametro") no pasa en silencio.
    model_config = ConfigDict(extra="forbid", strict=True)

    calculo: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$",
                         description="Nombre del cálculo que resuelve la imagen", examples=["suma"])
    parametros: dict[str, Any] = Field(description="Parámetros del cálculo; los interpreta la tarea",
                                       examples=[{"a": 3, "b": 4}])
    datos: dict[str, Any] = Field(default_factory=dict, description="Datos adicionales")
    imagen: str = Field(min_length=1, max_length=imagenes.LARGO_MAXIMO,
                        description="Imagen Docker con tag fijo o digest; tiene que estar en la lista blanca",
                        examples=["cerberusdistribuido/tarea:1.0.0"])

    @field_validator("imagen")
    @classmethod
    def _imagen_bien_formada(cls, valor):
        imagenes.parsear(valor)  # ImagenInvalida es un ValueError: pydantic lo junta con el resto
        return valor


def _tipo_json(valor):
    if valor is None:
        return "null"
    if isinstance(valor, bool):
        return "un booleano"
    if isinstance(valor, (int, float)):
        return "un número"
    if isinstance(valor, str):
        return "un string"
    if isinstance(valor, list):
        return "un array"
    return "un objeto"


_PATRONES = {
    "calculo": "sólo minúsculas, dígitos y '_', empezando por una letra",
}


def _problema(error):
    """Traduce un error de pydantic a una frase que diga exactamente qué pasó."""
    tipo, entrada, contexto = error["type"], error.get("input"), error.get("ctx") or {}
    campo = str(error["loc"][0]) if error["loc"] else ""
    if tipo == "missing":
        return "campo obligatorio faltante"
    if tipo == "extra_forbidden":
        return "campo no permitido"
    if tipo.endswith("_type"):
        if entrada is None:
            return "no puede ser null"
        esperado = "un objeto" if tipo == "dict_type" else "un string"
        return f"tiene que ser {esperado}, llegó {_tipo_json(entrada)}"
    if tipo == "string_too_short":
        return "no puede estar vacío"
    if tipo == "string_too_long":
        return f"no puede superar los {contexto.get('max_length')} caracteres"
    if tipo == "string_pattern_mismatch":
        return f"formato inválido: {_PATRONES.get(campo, contexto.get('pattern'))}"
    if tipo == "value_error":
        return str(contexto.get("error", error["msg"]))
    return error["msg"]


def _objeto_sin_duplicados(pares):
    vistas, repetidas = set(), set()
    for clave, _ in pares:
        (repetidas if clave in vistas else vistas).add(clave)
    if repetidas:
        repetidas = sorted(repetidas)
        raise ErrorApi(TipoError.JSON_INVALIDO, f"claves duplicadas en el JSON: {', '.join(repetidas)}")
    return dict(pares)


MAX_ANIDAMIENTO = 32


def _anidamiento(texto):
    """Nivel máximo de `[` / `{` abiertos, sin contar los que están dentro de strings.

    Se mide antes de parsear porque el parser de JSON es recursivo: hasta dónde
    llega depende del stack de la máquina (con `ulimit -s unlimited` un JSON de
    100.000 niveles se parsea; con el stack por defecto, falla). Con un límite
    explícito, la respuesta es la misma en cualquier entorno.
    """
    maximo = actual = 0
    en_string = escapado = False
    for caracter in texto:
        if en_string:
            if escapado:
                escapado = False
            elif caracter == "\\":
                escapado = True
            elif caracter == '"':
                en_string = False
        elif caracter == '"':
            en_string = True
        elif caracter in "[{":
            actual += 1
            maximo = max(maximo, actual)
        elif caracter in "]}":
            actual -= 1
    return maximo


def _rechazar_constante(nombre):
    raise ErrorApi(TipoError.JSON_INVALIDO, f"{nombre} no es un valor JSON válido")


def es_json(content_type):
    tipo = (content_type or "").split(";", 1)[0].strip().lower()
    return tipo == "application/json"


def validar_solicitud(cuerpo, content_type):
    """bytes del cuerpo → `SolicitudTarea`, o `ErrorApi` con el motivo exacto."""
    if not es_json(content_type):
        recibido = content_type or "ninguno"
        raise ErrorApi(TipoError.TIPO_DE_CONTENIDO,
                       f"el cuerpo tiene que ser application/json (llegó: {recibido})",
                       cabeceras={"Accept": "application/json"})
    if not cuerpo.strip():
        raise ErrorApi(TipoError.CUERPO_VACIO, "el cuerpo está vacío: se esperaba un objeto JSON")
    try:
        texto = cuerpo.decode("utf-8")
    except UnicodeDecodeError:
        raise ErrorApi(TipoError.JSON_INVALIDO, "el cuerpo no está codificado en UTF-8") from None
    if _anidamiento(texto) > MAX_ANIDAMIENTO:
        raise ErrorApi(TipoError.JSON_INVALIDO,
                       f"JSON demasiado anidado: como máximo {MAX_ANIDAMIENTO} niveles")
    try:
        objeto = json.loads(texto, object_pairs_hook=_objeto_sin_duplicados,
                            parse_constant=_rechazar_constante)
    except json.JSONDecodeError as error:
        raise ErrorApi(TipoError.JSON_INVALIDO,
                       f"JSON mal formado: {error.msg} (línea {error.lineno}, columna {error.colno})"
                       ) from None

    if not isinstance(objeto, dict):
        raise ErrorApi(TipoError.PAYLOAD_INVALIDO,
                       f"el cuerpo tiene que ser un objeto JSON, llegó {_tipo_json(objeto)}")
    try:
        return SolicitudTarea.model_validate(objeto)
    except ValidationError as error:
        detalles = [{"campo": ".".join(str(p) for p in e["loc"]) or "(cuerpo)", "problema": _problema(e)}
                    for e in error.errors(include_url=False)]
        raise ErrorApi(TipoError.PAYLOAD_INVALIDO, "el payload no es válido", detalles=detalles) from None
