"""Módulo de Errores Tipados, Mapeo HTTP y Sobre Estandarizado de Respuestas (Hit 3).

Garantiza el cumplimiento estricto del Contrato de la API (§4 y §5):
1. Estructura Unificada de Sobre (Envelope):
   Toda respuesta generada por el servidor, ya sea exitosa (HTTP 2xx) o de error (HTTP 4xx/5xx),
   mantiene exactamente la misma estructura de dos campos:
   {
       "codigo": <codigo_http_entero>,
       "contenido": <datos_o_error>
   }

2. Taxonomía de Errores Distribuidos (Hit 3):
   - Errores de Cliente y Validación: 400 (Bad Request), 401 (Unauthorized), 403 (Forbidden), 404, 405, 413, 415, 422, 429.
   - Errores de Infraestructura y Docker: 500 (Internal), 502 (Bad Gateway), 503 (Unavailable), 504 (Gateway Timeout).
   - Errores de Coordinación de Cluster:
     * `CLUSTER_NO_DISPONIBLE` (HTTP 503): No hay coordinador activo ni nodos vivos disponibles para asignar la tarea.
     * `NO_ES_COORDINADOR` (HTTP 409 Conflict): Se envió una petición exclusiva de líder (asignar, heartbeat)
       a un nodo worker que no ostenta el liderazgo en la época actual.
     * `COORDINADOR_RECHAZADO` (HTTP 409 Conflict): Se rechazó pacíficamente el mensaje COORDINADOR de un nodo
       cuyo identificador numérico es inferior al propio, desencadenando una nueva elección de Bully.

3. Separación de Seguridad (Prevención de Fuga de Información sensible — CWE-209):
   - Campos Públicos (`tipo`, `mensaje`, `detalles`): Visibles por el cliente HTTP en el cuerpo de la respuesta.
   - Campo Privado (`interno`): Exclusivamente volcado a los registros de auditoría (logs) del servidor,
     evitando exponer rutas internas de archivos, detalles de Docker o direcciones IP de la red privada.
"""

from enum import Enum
from fastapi.responses import JSONResponse


class TipoError(str, Enum):
    """Enumeración exhaustiva de los tipos de error reconocidos por el protocolo del cluster."""
    # Validación HTTP básica
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
    
    # Fallos de ejecución y Docker
    ERROR_INTERNO = "ERROR_INTERNO"
    TAREA_FALLIDA = "TAREA_FALLIDA"
    REGISTRY_NO_DISPONIBLE = "REGISTRY_NO_DISPONIBLE"
    SERVICIO_NO_DISPONIBLE = "SERVICIO_NO_DISPONIBLE"
    TAREA_SIN_RESPUESTA = "TAREA_SIN_RESPUESTA"
    
    # Seguridad, RBAC y Rate Limiting (R4)
    NO_AUTORIZADO = "NO_AUTORIZADO"
    ROL_INSUFICIENTE = "ROL_INSUFICIENTE"
    DEMASIADOS_PEDIDOS = "DEMASIADOS_PEDIDOS"
    
    # Protocolo de Cluster y Algoritmo Bully (Hit 3)
    CLUSTER_NO_DISPONIBLE = "CLUSTER_NO_DISPONIBLE"
    NO_ES_COORDINADOR = "NO_ES_COORDINADOR"
    COORDINADOR_RECHAZADO = "COORDINADOR_RECHAZADO"

    @property
    def estado(self) -> int:
        """Retorna el código de estado HTTP correspondiente según el contrato."""
        return _ESTADOS[self]


# Mapeo inmutable y determinístico entre el TipoError y el código de estado HTTP RFC 9110
_ESTADOS = {
    TipoError.CUERPO_VACIO: 400,
    TipoError.JSON_INVALIDO: 400,
    TipoError.NO_AUTORIZADO: 401,
    TipoError.ROL_INSUFICIENTE: 403,
    TipoError.IMAGEN_NO_PERMITIDA: 403,
    TipoError.RUTA_INEXISTENTE: 404,
    TipoError.METODO_NO_PERMITIDO: 405,
    TipoError.CUERPO_DEMASIADO_GRANDE: 413,
    TipoError.TIPO_DE_CONTENIDO: 415,
    TipoError.PAYLOAD_INVALIDO: 422,
    TipoError.IMAGEN_INEXISTENTE: 422,
    TipoError.TAREA_RECHAZADA: 422,
    TipoError.DEMASIADOS_PEDIDOS: 429,
    TipoError.ERROR_INTERNO: 500,
    TipoError.TAREA_FALLIDA: 502,
    TipoError.REGISTRY_NO_DISPONIBLE: 502,
    TipoError.SERVICIO_NO_DISPONIBLE: 503,
    TipoError.TAREA_SIN_RESPUESTA: 504,
    # Códigos específicos de Cluster
    TipoError.CLUSTER_NO_DISPONIBLE: 503,
    TipoError.NO_ES_COORDINADOR: 409,
    TipoError.COORDINADOR_RECHAZADO: 409,
}


class ErrorApi(Exception):
    """Excepción de negocio que encapsula un error tipado para ser serializado hacia el cliente."""

    def __init__(self, tipo: TipoError, mensaje: str, detalles=None, cabeceras=None, interno=None):
        """Inicializa la excepción estructurada.
        
        Parámetros:
        - tipo: Instancia de TipoError.
        - mensaje: Explicación amigable y segura para el cliente.
        - detalles: Lista opcional de campos infractores o metadatos de contexto.
        - cabeceras: Diccionario opcional de encabezados HTTP (ej. Retry-After, Allow).
        - interno: Texto de diagnóstico privado volcado únicamente a los logs.
        """
        super().__init__(mensaje)
        self.tipo = tipo
        self.mensaje = mensaje
        self.detalles = detalles or []
        self.cabeceras = cabeceras
        self.interno = interno


def responder(codigo: int, contenido, cabeceras=None) -> JSONResponse:
    """Construye una respuesta JSON conforming al contrato de sobre del TP2."""
    return JSONResponse(status_code=codigo, content={"codigo": codigo, "contenido": contenido},
                        headers=cabeceras)


def responder_error(error: ErrorApi) -> JSONResponse:
    """Serializa un ErrorApi dentro del sobre estructurado del protocolo."""
    contenido = {"error": {"tipo": error.tipo.value, "mensaje": error.mensaje,
                           "detalles": error.detalles}}
    return responder(error.tipo.estado, contenido, error.cabeceras)

