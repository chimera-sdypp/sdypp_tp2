"""Validación Sintáctica y Semántica de Peticiones (Hit 2).

Implementa la barrera de contención y validación exhaustiva de los payloads entrantes
a `POST /getRemoteTask` antes de que toquen el dominio, el pool de workers o el daemon de Docker.

Filosofía de Validación Estricta (Fail-Fast & Zero-Coercion):
1. Rechazar, nunca "arreglar" o inferir:
   Si el cliente envía `"123"` donde se espera un objeto o entero, el sistema jamás
   aplica `int("123")` silenciosamente; responde con `422 PAYLOAD_INVALIDO` señalando
   exactamente el campo y la discrepancia de tipos.
2. Parser JSON Manual y Defensivo:
   No se utiliza el body parser implícito de FastAPI, sino un pipeline customizado:
   - Detección de `Content-Type: application/json` (415 TIPO_DE_CONTENIDO si no coincide).
   - Verificación de payload no vacío (400 CUERPO_VACIO).
   - Decodificación UTF-8 estricta (400 JSON_INVALIDO).
   - Mitigación DoS por Anidamiento (Stack Overflow): El parser de CPython es recursivo.
     Para evitar agotar el stack (`ulimit -s`) mediante `[[[[...]]]]`, se analiza la
     profundidad léxica antes de parsear (`MAX_ANIDAMIENTO = 32`).
   - Rechazo de Claves Duplicadas (`_objeto_sin_duplicados`): RFC 8259 recomienda no permitir
     duplicados. Si se permiten, puede haber inconsistencias de deserialización (JSON Smuggling).
   - Rechazo de Constantes no estándar (`parse_constant`): Bloquea `NaN`, `Infinity` y `-Infinity`.
3. Validación de Esquema con Pydantic V2:
   - `extra="forbid"`: Rechaza campos desconocidos o mal escritos (ej. `parametro`).
   - `strict=True`: Desactiva coerción de tipos automática.
"""

import json
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app import imagenes
from app.errores import ErrorApi, TipoError


class SolicitudTarea(BaseModel):
    """Modelo Pydantic que define el esquema contractual estricto de una tarea remota.

    Configuración:
        - `extra="forbid"`: Genera error si el JSON contiene propiedades extra no documentadas.
        - `strict=True`: Exige que los tipos coincidan exactamente con la anotación (sin coerción).
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    # calculo: identificador de la operación matemática (ej. 'suma', 'multiplicacion')
    # Debe empezar con minúscula y sólo contener minúsculas, dígitos y guiones bajos (hasta 64 caracteres)
    calculo: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Nombre del cálculo que resuelve la imagen",
        examples=["suma"],
    )

    # parametros: diccionario con los operandos interpretados por la tarea (ej. {"a": 3, "b": 4})
    parametros: dict[str, Any] = Field(
        description="Parámetros del cálculo; los interpreta la tarea",
        examples=[{"a": 3, "b": 4}],
    )

    # datos: diccionario opcional para metadata o extensiones (por defecto vacío)
    datos: dict[str, Any] = Field(
        default_factory=dict,
        description="Datos adicionales",
    )

    # imagen: referencia a la imagen de Docker a ejecutar (debe cumplir formato OCI y estar en whitelist)
    imagen: str = Field(
        min_length=1,
        max_length=imagenes.LARGO_MAXIMO,
        description="Imagen Docker con tag fijo o digest; tiene que estar en la lista blanca",
        examples=["cerberusdistribuido/tarea:1.0.0"],
    )

    @field_validator("imagen")
    @classmethod
    def _imagen_bien_formada(cls, valor: str) -> str:
        """Valida sintácticamente la referencia de la imagen Docker utilizando el parser OCI."""
        # imagenes.parsear() lanza ImagenInvalida (subclase de ValueError),
        # lo cual Pydantic captura y reporta dentro del árbol de errores de validación.
        imagenes.parsear(valor)
        return valor


def _tipo_json(valor: Any) -> str:
    """Devuelve el nombre amigable en español del tipo de dato JSON recibido para los mensajes de error."""
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


# Descripciones legibles de patrones regex para clarificar los mensajes al cliente
_PATRONES = {
    "calculo": "sólo minúsculas, dígitos y '_', empezando por una letra",
}


def _problema(error: Dict[str, Any]) -> str:
    """Traduce un código de error interno de Pydantic a una descripción clara y contextualizada."""
    tipo = error["type"]
    entrada = error.get("input")
    contexto = error.get("ctx") or {}
    campo = str(error["loc"][0]) if error.get("loc") else ""

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
        return str(contexto.get("error", error.get("msg", "")))
    return str(error.get("msg", "valor inválido"))


def _objeto_sin_duplicados(pares: List[tuple]) -> Dict[str, Any]:
    """Hook para `json.loads` que detecta y rechaza objetos con claves duplicadas.

    ¿Por qué?
    Por defecto, `json.loads` en Python sobrescribe silenciosamente las claves repetidas
    con el último valor (`{"a": 1, "a": 2}` -> `{"a": 2}`). En APIs seguras, esto puede
    ocasionar inconsistencias de autorización o validación (JSON Deserialization Attacks).
    """
    vistas, repetidas = set(), set()
    for clave, _ in pares:
        if clave in vistas:
            repetidas.add(clave)
        else:
            vistas.add(clave)
    if repetidas:
        claves_ordenadas = sorted(repetidas)
        raise ErrorApi(
            TipoError.JSON_INVALIDO,
            f"claves duplicadas en el JSON: {', '.join(claves_ordenadas)}",
        )
    return dict(pares)


# Límite máximo de niveles de anidamiento permitido para prevenir DoS por agotamiento de pila
MAX_ANIDAMIENTO = 32


def _anidamiento(texto: str) -> int:
    """Calcula el nivel máximo de llaves `{` y corchetes `[` anidados sin considerar los strings.

    ¿Por qué se evalúa antes de parsear?
    El parser estándar de JSON en Python utiliza llamadas recursivas en C/Python.
    Un atacante que envíe 50.000 corchetes abiertos puede provocar un `RecursionError`
    o un Segmentation Fault por Stack Overflow antes de que el código de validación ejecute.
    Al verificar el anidamiento con un escaneo lineal O(N), se aborta la petición de forma segura.
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


def _rechazar_constante(nombre: str) -> None:
    """Hook para rechazar constantes numéricas no contempladas en el estándar JSON estricto (RFC 8259)."""
    raise ErrorApi(TipoError.JSON_INVALIDO, f"{nombre} no es un valor JSON válido")


def es_json(content_type: str | None) -> bool:
    """Comprueba si el Content-Type de la cabecera es formalmente 'application/json'."""
    tipo = (content_type or "").split(";", 1)[0].strip().lower()
    return tipo == "application/json"


def validar_solicitud(cuerpo: bytes, content_type: str | None) -> SolicitudTarea:
    """Pipeline integral de validación de entrada para `POST /getRemoteTask`.

    Parámetros:
        cuerpo: Bytes crudos recibidos en el payload HTTP.
        content_type: Valor de la cabecera `Content-Type`.

    Retorna:
        Instancia validada de `SolicitudTarea`.

    Lanza:
        ErrorApi con el código HTTP específico (400, 415 o 422) y el detalle correspondiente.
    """
    # 1. Validación de cabecera Content-Type
    if not es_json(content_type):
        recibido = content_type or "ninguno"
        raise ErrorApi(
            TipoError.TIPO_DE_CONTENIDO,
            f"el cuerpo tiene que ser application/json (llegó: {recibido})",
            cabeceras={"Accept": "application/json"},
        )

    # 2. Validación de cuerpo no vacío
    if not cuerpo.strip():
        raise ErrorApi(TipoError.CUERPO_VACIO, "el cuerpo está vacío: se esperaba un objeto JSON")

    # 3. Decodificación de caracteres UTF-8
    try:
        texto = cuerpo.decode("utf-8")
    except UnicodeDecodeError:
        raise ErrorApi(TipoError.JSON_INVALIDO, "el cuerpo no está codificado en UTF-8") from None

    # 4. Control de profundidad de anidamiento (protección contra Recursion DoS)
    if _anidamiento(texto) > MAX_ANIDAMIENTO:
        raise ErrorApi(
            TipoError.JSON_INVALIDO,
            f"JSON demasiado anidado: como máximo {MAX_ANIDAMIENTO} niveles",
        )

    # 5. Parsing sintáctico con detección de duplicados y rechazo de NaN/Infinity
    try:
        objeto = json.loads(
            texto,
            object_pairs_hook=_objeto_sin_duplicados,
            parse_constant=_rechazar_constante,
        )
    except json.JSONDecodeError as error:
        raise ErrorApi(
            TipoError.JSON_INVALIDO,
            f"JSON mal formado: {error.msg} (línea {error.lineno}, columna {error.colno})",
        ) from None

    # 6. Verificación de que el JSON raíz sea un objeto diccionario (no una lista ni un primitivo)
    if not isinstance(objeto, dict):
        raise ErrorApi(
            TipoError.PAYLOAD_INVALIDO,
            f"el cuerpo tiene que ser un objeto JSON, llegó {_tipo_json(objeto)}",
        )

    # 7. Validación semántica del esquema contractual mediante Pydantic
    try:
        return SolicitudTarea.model_validate(objeto)
    except ValidationError as error:
        detalles = [
            {"campo": ".".join(str(p) for p in e["loc"]) or "(cuerpo)", "problema": _problema(e)}
            for e in error.errors(include_url=False)
        ]
        raise ErrorApi(TipoError.PAYLOAD_INVALIDO, "el payload no es válido", detalles=detalles) from None
