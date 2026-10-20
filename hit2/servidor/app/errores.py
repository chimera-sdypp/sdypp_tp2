"""Manejo Centralizado de Errores y Sobre Común de Respuesta (Hit 2 & Video).

Implementa el estándar de respuestas y errores tipados definido en el Contrato de la API (§4)
y extendido en el Contrato de Video (`contrato-video.md` §6).

Todas las respuestas del servidor (tanto éxitos como fallas) se encapsulan en una estructura
uniforme denominada "sobre" (envelope):
    {
        "codigo": <int>,
        "contenido": <dict>
    }

Para respuestas de error, el campo `contenido` adopta obligatoriamente el formato:
    {
        "error": {
            "tipo": "<NOMBRE_DEL_TIPO>",
            "mensaje": "<descripción apta para el cliente>",
            "detalles": [...]
        }
    }

Principio de Seguridad Contra Fuga de Información (CWE-209):
- `mensaje` y `detalles` son públicos y contractualmente estables (se envían al cliente).
- `interno` es privado: contiene stack traces, comandos de ffprobe/Docker o excepciones crudas
  y se escribe ÚNICAMENTE en los logs del servidor para auditoría de administradores.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi.responses import JSONResponse


class TipoError(str, Enum):
    """Enumeración canónica de todos los tipos de error reconocidos por la API en Hit 2 y Video.

    Hereda de `str` para permitir serialización JSON directa sin conversiones adicionales.
    """

    # --- Errores 400 (Bad Request) ---
    CUERPO_VACIO = "CUERPO_VACIO"              # El payload HTTP recibido no contiene bytes útiles
    JSON_INVALIDO = "JSON_INVALIDO"            # Fallo sintáctico: mal formado, NaN, profundidad excesiva o claves duplicadas

    # --- Errores 401 / 403 (Autenticación y Autorización - R4) ---
    NO_AUTORIZADO = "NO_AUTORIZADO"            # Falta el token JWT, expiró o la firma HMAC es inválida (401)
    ROL_INSUFICIENTE = "ROL_INSUFICIENTE"      # Token válido pero el usuario no tiene el rol requerido para el endpoint (403)
    IMAGEN_NO_PERMITIDA = "IMAGEN_NO_PERMITIDA"# Imagen fuera de la lista blanca o con digest no autorizado (403)

    # --- Errores 404 / 405 (Enrutamiento y Recursos) ---
    RUTA_INEXISTENTE = "RUTA_INEXISTENTE"      # Endpoint no registrado en la aplicación (404)
    TRABAJO_INEXISTENTE = "TRABAJO_INEXISTENTE"# ID de trabajo de video no encontrado en disco o no pertenece al usuario (404)
    CALIDAD_NO_SOLICITADA = "CALIDAD_NO_SOLICITADA" # La resolución pedida no fue solicitada al crear el trabajo (404)
    METODO_NO_PERMITIDO = "METODO_NO_PERMITIDO"# Verbo HTTP (GET, POST, etc.) no admitido para la ruta solicitada (405)

    # --- Errores 409 (Conflictos de Estado) ---
    RESULTADO_NO_LISTO = "RESULTADO_NO_LISTO"  # El video aún se encuentra en cola o procesando (409)

    # --- Errores 413 / 415 (Filtros de Entrada HTTP) ---
    CUERPO_DEMASIADO_GRANDE = "CUERPO_DEMASIADO_GRANDE" # El tamaño del payload de tarea excede 64 KB (413)
    VIDEO_DEMASIADO_GRANDE = "VIDEO_DEMASIADO_GRANDE"   # El archivo de video subido excede 1 GiB (413)
    TIPO_DE_CONTENIDO = "TIPO_DE_CONTENIDO"    # Content-Type no admitido (ej. no es JSON o multipart) (415)

    # --- Errores 422 (Semántica, Validación de Dominio y Video) ---
    PAYLOAD_INVALIDO = "PAYLOAD_INVALIDO"      # JSON sintácticamente válido pero viola el esquema Pydantic (422)
    IMAGEN_INEXISTENTE = "IMAGEN_INEXISTENTE"  # La imagen solicitada no existe localmente ni en Docker Hub (422)
    TAREA_RECHAZADA = "TAREA_RECHAZADA"        # El contenedor de tarea rechazó los parámetros de cálculo (ej. div por cero) (422)
    ARCHIVO_NO_ES_VIDEO = "ARCHIVO_NO_ES_VIDEO"# El archivo subido no es un contenedor multimedia reproducible (0 bytes o sin stream de video) (422)
    VIDEO_DEMASIADO_LARGO = "VIDEO_DEMASIADO_LARGO" # La duración del video excede el límite contractual (15 min) (422)
    RESOLUCION_NO_SOPORTADA = "RESOLUCION_NO_SOPORTADA" # La altura o ancho del video es menor a 240p (422)
    CALIDAD_SUPERIOR_AL_ORIGINAL = "CALIDAD_SUPERIOR_AL_ORIGINAL" # Se solicitó escalar a una resolución mayor que la original (no upscale) (422)
    URL_INVALIDA = "URL_INVALIDA"              # URL mal formada, protocolo no http/https, o apunta a hostnames prohibidos (localhost, loopback, rangos privados SSRF) (422)
    URL_INACCESIBLE = "URL_INACCESIBLE"        # Falla de DNS, conexión rechazada o error HTTP al descargar la URL (422)
    DESCARGA_FALLIDA = "DESCARGA_FALLIDA"      # Error HTTP (404, 500) o timeout durante la descarga remota del video (422)

    # --- Errores 429 (Rate Limiting y Cuotas Concurrentes - R4 / Video) ---
    DEMASIADOS_PEDIDOS = "DEMASIADOS_PEDIDOS"  # Tasa de peticiones por minuto superada según algoritmo Token Bucket (429)
    DEMASIADOS_TRABAJOS = "DEMASIADOS_TRABAJOS"# El usuario ya posee el máximo permitido de trabajos de video activos (3 concurrentes) (429)

    # --- Errores 500 / 502 / 503 / 504 (Infraestructura y Fallas de Sistema) ---
    ERROR_INTERNO = "ERROR_INTERNO"            # Excepción inesperada o bug no controlado en el servidor (500)
    TAREA_FALLIDA = "TAREA_FALLIDA"            # El contenedor de tarea crasheó o devolvió HTTP 5xx (502)
    REGISTRY_NO_DISPONIBLE = "REGISTRY_NO_DISPONIBLE" # Error de red o timeout conectando a Docker Hub (502)
    SERVICIO_NO_DISPONIBLE = "SERVICIO_NO_DISPONIBLE" # Daemon de Docker inalcanzable (503)
    SERVICIO_SATURADO = "SERVICIO_SATURADO"    # La cola de tareas o videos alcanzó su capacidad máxima de amortiguación (503)
    TAREA_SIN_RESPUESTA = "TAREA_SIN_RESPUESTA" # Timeout: el contenedor no estuvo listo o no respondió a tiempo (504)

    @property
    def estado(self) -> int:
        """Obtiene el código de estado HTTP correspondiente según la tabla oficial."""
        return _ESTADOS[self]


# Mapeo inmutable entre el tipo de error semántico y el código de estado HTTP RFC
_ESTADOS: Dict[TipoError, int] = {
    TipoError.CUERPO_VACIO: 400,
    TipoError.JSON_INVALIDO: 400,
    TipoError.NO_AUTORIZADO: 401,
    TipoError.ROL_INSUFICIENTE: 403,
    TipoError.IMAGEN_NO_PERMITIDA: 403,
    TipoError.RUTA_INEXISTENTE: 404,
    TipoError.TRABAJO_INEXISTENTE: 404,
    TipoError.CALIDAD_NO_SOLICITADA: 404,
    TipoError.METODO_NO_PERMITIDO: 405,
    TipoError.RESULTADO_NO_LISTO: 409,
    TipoError.CUERPO_DEMASIADO_GRANDE: 413,
    TipoError.VIDEO_DEMASIADO_GRANDE: 413,
    TipoError.TIPO_DE_CONTENIDO: 415,
    TipoError.PAYLOAD_INVALIDO: 422,
    TipoError.IMAGEN_INEXISTENTE: 422,
    TipoError.TAREA_RECHAZADA: 422,
    TipoError.ARCHIVO_NO_ES_VIDEO: 422,
    TipoError.VIDEO_DEMASIADO_LARGO: 422,
    TipoError.RESOLUCION_NO_SOPORTADA: 422,
    TipoError.CALIDAD_SUPERIOR_AL_ORIGINAL: 422,
    TipoError.URL_INVALIDA: 422,
    TipoError.URL_INACCESIBLE: 422,
    TipoError.DESCARGA_FALLIDA: 422,
    TipoError.DEMASIADOS_PEDIDOS: 429,
    TipoError.DEMASIADOS_TRABAJOS: 429,
    TipoError.ERROR_INTERNO: 500,
    TipoError.TAREA_FALLIDA: 502,
    TipoError.REGISTRY_NO_DISPONIBLE: 502,
    TipoError.SERVICIO_NO_DISPONIBLE: 503,
    TipoError.SERVICIO_SATURADO: 503,
    TipoError.TAREA_SIN_RESPUESTA: 504,
}


class ErrorApi(Exception):
    """Excepción controlada de negocio que interrumpe el flujo y produce una respuesta HTTP de error.

    Atributos:
        tipo: Instancia de `TipoError` que categoriza la falla.
        mensaje: Descripción legible para el usuario de la causa del error.
        detalles: Lista opcional con especificadores de campos (p. ej. en fallas de validación).
        cabeceras: Diccionario opcional de cabeceras HTTP a incluir (p. ej. `Retry-After` en 429/503).
        interno: Información diagnóstica privada para el log del sistema (jamás se envía al cliente).
    """

    def __init__(
        self,
        tipo: TipoError,
        mensaje: str,
        detalles: Optional[List[Any]] = None,
        cabeceras: Optional[Dict[str, str]] = None,
        interno: Optional[str] = None,
    ):
        super().__init__(mensaje)
        self.tipo = tipo
        self.mensaje = mensaje
        self.detalles = detalles or []
        self.cabeceras = cabeceras
        self.interno = interno


def responder(codigo: int, contenido: Any, cabeceras: Optional[Dict[str, str]] = None) -> JSONResponse:
    """Construye una respuesta HTTP JSON empaquetada estrictamente en el sobre contractual.

    Formato de salida:
        HTTP/1.1 <codigo>
        Content-Type: application/json
        {
            "codigo": <codigo>,
            "contenido": <contenido>
        }
    """
    return JSONResponse(
        status_code=codigo,
        content={"codigo": codigo, "contenido": contenido},
        headers=cabeceras,
    )


def responder_error(error: ErrorApi) -> JSONResponse:
    """Convierte una excepción `ErrorApi` en una respuesta HTTP formateada con el sobre de error.

    Garantiza que la estructura de la respuesta cumpla con la especificación OpenAPI y el contrato §4.
    """
    contenido = {
        "error": {
            "tipo": error.tipo.value,
            "mensaje": error.mensaje,
            "detalles": error.detalles,
        }
    }
    return responder(error.tipo.estado, contenido, error.cabeceras)
