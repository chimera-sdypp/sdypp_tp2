"""Errores tipados de la API y el sobre común de las respuestas.

Cada error es un `TipoError` con su código HTTP (contrato §4). El sobre es el
mismo salga bien o mal: `{"codigo", "contenido"}`; si es un error, `contenido`
es `{"error": {tipo, mensaje, detalles}}`.
"""

from enum import Enum

from fastapi.responses import JSONResponse


class TipoError(str, Enum):
    CUERPO_VACIO = "CUERPO_VACIO"
    JSON_INVALIDO = "JSON_INVALIDO"
    IMAGEN_NO_PERMITIDA = "IMAGEN_NO_PERMITIDA"
    RUTA_INEXISTENTE = "RUTA_INEXISTENTE"
    METODO_NO_PERMITIDO = "METODO_NO_PERMITIDO"
    CUERPO_DEMASIADO_GRANDE = "CUERPO_DEMASIADO_GRANDE"
    TIPO_DE_CONTENIDO = "TIPO_DE_CONTENIDO"
    PAYLOAD_INVALIDO = "PAYLOAD_INVALIDO"
    IMAGEN_INEXISTENTE = "IMAGEN_INEXISTENTE"
    TAREA_RECHAZADA = "TAREA_RECHAZADA"
    ERROR_INTERNO = "ERROR_INTERNO"
    TAREA_FALLIDA = "TAREA_FALLIDA"
    REGISTRY_NO_DISPONIBLE = "REGISTRY_NO_DISPONIBLE"
    SERVICIO_NO_DISPONIBLE = "SERVICIO_NO_DISPONIBLE"
    TAREA_SIN_RESPUESTA = "TAREA_SIN_RESPUESTA"
    # Cluster (Hit 3)
    CLUSTER_NO_DISPONIBLE = "CLUSTER_NO_DISPONIBLE"
    NO_ES_COORDINADOR = "NO_ES_COORDINADOR"
    COORDINADOR_RECHAZADO = "COORDINADOR_RECHAZADO"

    @property
    def estado(self):
        return _ESTADOS[self]


_ESTADOS = {
    TipoError.CUERPO_VACIO: 400,
    TipoError.JSON_INVALIDO: 400,
    TipoError.IMAGEN_NO_PERMITIDA: 403,
    TipoError.RUTA_INEXISTENTE: 404,
    TipoError.METODO_NO_PERMITIDO: 405,
    TipoError.CUERPO_DEMASIADO_GRANDE: 413,
    TipoError.TIPO_DE_CONTENIDO: 415,
    TipoError.PAYLOAD_INVALIDO: 422,
    TipoError.IMAGEN_INEXISTENTE: 422,
    TipoError.TAREA_RECHAZADA: 422,
    TipoError.ERROR_INTERNO: 500,
    TipoError.TAREA_FALLIDA: 502,
    TipoError.REGISTRY_NO_DISPONIBLE: 502,
    TipoError.SERVICIO_NO_DISPONIBLE: 503,
    TipoError.TAREA_SIN_RESPUESTA: 504,
    TipoError.CLUSTER_NO_DISPONIBLE: 503,
    TipoError.NO_ES_COORDINADOR: 409,
    TipoError.COORDINADOR_RECHAZADO: 409,
}


class ErrorApi(Exception):
    """Un error que termina en una respuesta al cliente.

    `mensaje` y `detalles` son públicos. `interno` sólo va al log: ahí va lo que
    el cliente no tiene por qué ver (el error de Docker tal cual, la IP, etc.).
    """

    def __init__(self, tipo, mensaje, detalles=None, cabeceras=None, interno=None):
        super().__init__(mensaje)
        self.tipo = tipo
        self.mensaje = mensaje
        self.detalles = detalles or []
        self.cabeceras = cabeceras
        self.interno = interno


def responder(codigo, contenido, cabeceras=None):
    return JSONResponse(status_code=codigo, content={"codigo": codigo, "contenido": contenido},
                        headers=cabeceras)


def responder_error(error):
    contenido = {"error": {"tipo": error.tipo.value, "mensaje": error.mensaje,
                           "detalles": error.detalles}}
    return responder(error.tipo.estado, contenido, error.cabeceras)
