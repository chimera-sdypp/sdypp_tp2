"""Cliente CLI del Hit 3: Envío de tareas distribuidas al cluster vía Nginx.

Este cliente permite enviar solicitudes de cómputo remoto a través del reverse proxy Nginx
(por defecto en http://localhost:8080), el cual distribuye round-robin las conexiones HTTP
hacia los nodos de la red distribuida (nodo1, nodo2, nodo3).

Características Principales:
1. Inyección de Autenticación Bearer JWT:
   Soporta el envío del token firmado exigido por el endpoint `/getRemoteTask` (Requisito R4).
   El token puede pasarse por la variable de entorno `TP2_TOKEN` o mediante el flag `--token`.
2. Protocolo de Envoltura (Sobre Uniforme):
   Recibe y decodifica la respuesta canónica JSON `{"codigo": int, "contenido": ...}` tanto
   en casos de éxito (HTTP 200) como en errores de validación (422) o de cluster (503).
3. Códigos de Retorno UNIX:
   Retorna código de salida 0 si la tarea finalizó con éxito (`codigo == 200`), y código 1
   ante cualquier error de cómputo, validación o indisponibilidad del cluster.

Ejemplos de Ejecución:
    python cliente.py suma '{"a": 3, "b": 4}' --imagen cerberusdistribuido/tarea:1.0.0
    python cliente.py division '{"a": 1, "b": 0}' --imagen cerberusdistribuido/tarea:1.0.0 --servidor http://127.0.0.1:8080
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def enviar_tarea(
    servidor: str,
    calculo: str,
    parametros: dict,
    datos: dict,
    imagen: str,
    timeout: float = 120.0,
    token: str | None = None
) -> dict:
    """Envía un pedido POST a `/getRemoteTask` y decodifica el sobre JSON de respuesta.
    
    Parámetros:
      - `servidor`: URL base del cluster o balanceador Nginx (ej. 'http://localhost:8080').
      - `calculo`: Operación aritmética solicitada ('suma', 'resta', 'multiplicacion', 'division').
      - `parametros`: Diccionario con operandos numéricos `{"a": ..., "b": ...}`.
      - `datos`: Objeto con metadatos contextuales o cadenas adicionales.
      - `imagen`: Referencia OCI de la imagen Docker en lista blanca (ej. 'cerberusdistribuido/tarea:1.0.0').
      - `timeout`: Tiempo límite en segundos para la conexión y lectura de la respuesta.
      - `token`: Token JWT opcional; si no se suministra, se lee de la variable de entorno `TP2_TOKEN`.
    
    Retorna:
      Diccionario Python correspondiente al sobre JSON devuelto por el servidor (`codigo` y `contenido`).
    """
    # Construcción del payload JSON exigido contractualmente por el endpoint /getRemoteTask
    cuerpo = {
        "calculo": calculo,
        "parametros": parametros,
        "datos": datos,
        "imagen": imagen,
    }
    
    # Resolución del token Bearer para autenticación obligatoria
    token_efectivo = os.environ.get("TP2_TOKEN", "") if token is None else token

    # Ensamblado de cabeceras HTTP: Content-Type JSON y Authorization Bearer condicional
    cabeceras = {"Content-Type": "application/json"}
    if token_efectivo:
        cabeceras["Authorization"] = f"Bearer {token_efectivo}"

    # Construcción de la solicitud HTTP POST usando la biblioteca estándar urllib
    url_destino = f"{servidor.rstrip('/')}/getRemoteTask"
    pedido = urllib.request.Request(
        url_destino,
        data=json.dumps(cuerpo).encode("utf-8"),
        headers=cabeceras,
        method="POST",
    )

    try:
        # Enviar petición y esperar la respuesta sincrónica del cluster
        with urllib.request.urlopen(pedido, timeout=timeout) as respuesta:
            cuerpo_bytes = respuesta.read()
            return json.loads(cuerpo_bytes.decode("utf-8"))
    except urllib.error.HTTPError as error:
        # En caso de error HTTP (4xx o 5xx), el servidor de todos modos emite el sobre uniforme JSON.
        # Leemos el cuerpo del error para parsear el sobre de error contractualmente estructurado.
        cuerpo_error_bytes = error.read()
        return json.loads(cuerpo_error_bytes.decode("utf-8"))


def main() -> int:
    """Función de entrada del cliente por línea de comandos."""
    # Configuración del parser de argumentos de terminal
    parser = argparse.ArgumentParser(
        description="Cliente CLI del Hit 3 para ejecución de tareas distribuidas en cluster"
    )
    # Operación matemática requerida
    parser.add_argument(
        "calculo",
        help="Operación requerida: suma, resta, multiplicacion o division"
    )
    # Operandos en formato JSON string
    parser.add_argument(
        "parametros",
        type=json.loads,
        help="Objeto JSON con operandos numéricos, ej: '{\"a\": 3, \"b\": 4}'"
    )
    # Metadatos adicionales opcionales
    parser.add_argument(
        "--datos",
        type=json.loads,
        default={},
        help="Objeto JSON con metadatos contextuales adicionales"
    )
    # Imagen Docker de la tarea
    parser.add_argument(
        "--imagen",
        required=True,
        help="Referencia de la imagen Docker en lista blanca (repositorio:tag)"
    )
    # Dirección del reverse proxy Nginx o nodo ingress
    parser.add_argument(
        "--servidor",
        default=os.environ.get("TP2_SERVIDOR", "http://localhost:8080"),
        help="URL base del servidor o reverse proxy Nginx (por defecto: http://localhost:8080)"
    )
    # Token JWT explícito
    parser.add_argument(
        "--token",
        default=None,
        help="Token de autenticación JWT (si no se especifica, toma de la variable TP2_TOKEN)"
    )

    # Procesamiento de argumentos
    args = parser.parse_args()

    # Despacho de la tarea remota
    try:
        respuesta = enviar_tarea(
            servidor=args.servidor,
            calculo=args.calculo,
            parametros=args.parametros,
            datos=args.datos,
            imagen=args.imagen,
            token=args.token
        )
    except urllib.error.URLError as error:
        # Falla de red a nivel socket (Nginx caído, puerto inaccesible o DNS no resuelto)
        sys.exit(f"No se pudo conectar con el servidor {args.servidor}: {error.reason}")

    # Salida por stdout formateada en JSON legible
    print(json.dumps(respuesta, ensure_ascii=False, indent=2))

    # Retorno: 0 si el código de estado devuelto en el sobre es 200, 1 en caso contrario
    return 0 if respuesta.get("codigo") == 200 else 1


if __name__ == "__main__":
    sys.exit(main())

