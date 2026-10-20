"""Módulo de Validación Estricta de Solicitudes y Prevención de Ataques en JSON (Hit 3).

Principios de Seguridad y Validación Fail-Fast:
1. Rechazo Temprano sin Coerción Implícita:
   - Toda entrada se valida rigurosamente contra el contrato antes de llegar a la lógica de negocio.
   - Jamás se intenta "arreglar" o "coercionar" tipos (ej. un string `"123"` donde se espera un entero
     o booleano se rechaza de inmediato con HTTP 422 indicando el campo infractor).

2. Mitigación de Vulnerabilidades de Deserialización JSON (RFC 8259):
   - Ataques de Agotamiento de Pila (Stack Overflow DoS): Se analiza el anidamiento sintáctico de `[` y `{`
     antes de la llamada a `json.loads()`, acotando la profundidad máxima a `MAX_ANIDAMIENTO = 32`.
   - Inyección de Claves Duplicadas (JSON Collision / Pollution Attacks): Se implementa
     `object_pairs_hook=_objeto_sin_duplicados`, abortando con HTTP 400 si se detectan claves repetidas.
   - Constantes no estándar (RFC 8259): Se utiliza `parse_constant=_rechazar_constante` para
     rechazar `NaN`, `Infinity` y `-Infinity` que violan la especificación estándar de JSON.
"""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app import imagenes
from app.errores import ErrorApi, TipoError


class SolicitudTarea(BaseModel):
    """Modelo Pydantic V2 de validación de carga útil para solicitudes de tareas remotas.
    
    Reglas de Configuración:
    - strict=True: Prohíbe coerción implícita de tipos (ej. booleano a entero, string a int).
    - extra="forbid": Rechaza peticiones que contengan campos desconocidos o no documentados.
    """
    model_config = ConfigDict(extra="forbid", strict=True)

    # Identificador del cálculo (sólo minúsculas, números y guiones bajos)
    calculo: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$",
                         description="Nombre del cálculo que resuelve la imagen", examples=["suma"])
    # Diccionario de parámetros numéricos u operacionales requeridos por la tarea
    parametros: dict[str, Any] = Field(description="Parámetros del cálculo; los interpreta la tarea",
                                       examples=[{"a": 3, "b": 4}])
    # Datos opcionales adicionales
    datos: dict[str, Any] = Field(default_factory=dict, description="Datos adicionales")
    # Referencia de la imagen Docker en la lista blanca
    imagen: str = Field(min_length=1, max_length=imagenes.LARGO_MAXIMO,
                        description="Imagen Docker con tag fijo o digest; tiene que estar en la lista blanca",
                        examples=["cerberusdistribuido/tarea:1.0.0"])

    @field_validator("imagen")
    @classmethod
    def _imagen_bien_formada(cls, valor):
        """Valida que la referencia de la imagen cumpla con la sintaxis canónica de Docker."""
        imagenes.parsear(valor)
        return valor


def _tipo_json(valor):
    """Identifica el tipo de dato JSON de un valor en lenguaje coloquial amigable."""
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
    """Traduce códigos de error técnicos de Pydantic a explicaciones claras y precisas."""
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
    """Hook para json.loads que detecta y rechaza claves duplicadas en objetos JSON."""
    vistas, repetidas = set(), set()
    for clave, _ in pares:
        (repetidas if clave in vistas else vistas).add(clave)
    if repetidas:
        repetidas = sorted(repetidas)
        raise ErrorApi(TipoError.JSON_INVALIDO, f"claves duplicadas en el JSON: {', '.join(repetidas)}")
    return dict(pares)


# Profundidad máxima de anidamiento permitida para prevenir agotamiento de pila recursiva
MAX_ANIDAMIENTO = 32


def _anidamiento(texto):
    """Calcula el nivel máximo de anidamiento de `[` y `{` omitiendo caracteres dentro de cadenas.
    
    Protección contra Denegación de Servicio (DoS por Stack Overflow en parsers recursivos C/Python).
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
    """Rechaza constantes no numéricas (NaN, Infinity, -Infinity) no conformes a RFC 8259."""
    raise ErrorApi(TipoError.JSON_INVALIDO, f"{nombre} no es un valor JSON válido")


def es_json(content_type):
    """Verifica si el encabezado Content-Type corresponde a application/json."""
    tipo = (content_type or "").split(";", 1)[0].strip().lower()
    return tipo == "application/json"


def validar_solicitud(cuerpo: bytes, content_type: str) -> SolicitudTarea:
    """Valida exhaustivamente los bytes del cuerpo HTTP contra el contrato de la API.
    
    Secuencia de Validación:
    1. Content-Type: Verifica presencia de 'application/json' (HTTP 415 si difiere).
    2. Cuerpo no vacío: Rechaza cuerpos en blanco o espacios (HTTP 400).
    3. Codificación UTF-8: Verifica decodificación válida sin bytes truncados (HTTP 400).
    4. Límite de Anidamiento: Verifica profundidad <= 32 niveles (HTTP 400).
    5. Parsing Estricto: Rechaza duplicados y constantes no estándar (HTTP 400).
    6. Esquema Pydantic V2: Validación de tipos y campos prohibidos (HTTP 422).
    """
    # 1. Validación de encabezado Content-Type
    if not es_json(content_type):
        recibido = content_type or "ninguno"
        raise ErrorApi(TipoError.TIPO_DE_CONTENIDO,
                       f"el cuerpo tiene que ser application/json (llegó: {recibido})",
                       cabeceras={"Accept": "application/json"})
    
    # 2. Control de cuerpo vacío
    if not cuerpo.strip():
        raise ErrorApi(TipoError.CUERPO_VACIO, "el cuerpo está vacío: se esperaba un objeto JSON")
    
    # 3. Decodificación UTF-8
    try:
        texto = cuerpo.decode("utf-8")
    except UnicodeDecodeError:
        raise ErrorApi(TipoError.JSON_INVALIDO, "el cuerpo no está codificado en UTF-8") from None
    
    # 4. Control de anidamiento máximo
    if _anidamiento(texto) > MAX_ANIDAMIENTO:
        raise ErrorApi(TipoError.JSON_INVALIDO,
                       f"JSON demasiado anidado: como máximo {MAX_ANIDAMIENTO} niveles")
    
    # 5. Parsing con detección de duplicados y rechazo de NaN/Infinity
    try:
        objeto = json.loads(texto, object_pairs_hook=_objeto_sin_duplicados,
                            parse_constant=_rechazar_constante)
    except json.JSONDecodeError as error:
        raise ErrorApi(TipoError.JSON_INVALIDO,
                       f"JSON mal formado: {error.msg} (línea {error.lineno}, columna {error.colno})"
                       ) from None

    # 6. Verificación de tipo raíz objeto diccionario
    if not isinstance(objeto, dict):
        raise ErrorApi(TipoError.PAYLOAD_INVALIDO,
                       f"el cuerpo tiene que ser un objeto JSON, llegó {_tipo_json(objeto)}")
    
    # 7. Validación contra modelo Pydantic
    try:
        return SolicitudTarea.model_validate(objeto)
    except ValidationError as error:
        detalles = [{"campo": ".".join(str(p) for p in e["loc"]) or "(cuerpo)", "problema": _problema(e)}
                    for e in error.errors(include_url=False)]
        raise ErrorApi(TipoError.PAYLOAD_INVALIDO, "el payload no es válido", detalles=detalles) from None

