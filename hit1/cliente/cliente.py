"""Cliente de Línea de Comandos (CLI) para Tareas Remotas (Hit 1).

Permite enviar cálculos al servidor HTTP de forma interactiva o automatizada en scripts.

Ejemplos de invocación:
    # Cálculo exitoso con token JWT
    python cliente.py suma '{"a": 3, "b": 4}' --imagen cerberusdistribuido/tarea:1.0.0 --token "$TOKEN"

    # Caso de error semántico (división por cero)
    python cliente.py division '{"a": 1, "b": 0}' --imagen cerberusdistribuido/tarea:1.0.0 --servidor http://localhost:8080

Convención de Códigos de Salida (Exit Codes):
- Retorna `0` si el servidor respondió con HTTP 200 (éxito en la tarea).
- Retorna `1` si el servidor respondió con un error (4xx o 5xx) o si falló la conexión.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict


def enviar_tarea(
    servidor: str,
    calculo: str,
    parametros: dict,
    datos: dict,
    imagen: str,
    timeout: float = 120.0,
    token: str = "",
) -> Dict[str, Any]:
    """Envía la solicitud a `POST /getRemoteTask` y desempaqueta el sobre JSON de respuesta.

    Parámetros:
        servidor: URL base del servidor (ej. 'http://localhost:8080').
        calculo: Operación aritmética ('suma', 'resta', 'multiplicacion', 'division').
        parametros: Diccionario con operandos {'a': ..., 'b': ...}.
        datos: Diccionario opcional para extensiones.
        imagen: Referencia a la imagen Docker de la tarea.
        timeout: Tiempo máximo de espera en segundos antes de abortar.
        token: Token JWT firmado opcional para la cabecera `Authorization: Bearer <token>`.

    Retorna:
        Diccionario con el sobre `{"codigo": ..., "contenido": ...}` devuelto por el servidor.
    """
    # Construcción del payload JSON exigido por el esquema contractual
    cuerpo = {
        "calculo": calculo,
        "parametros": parametros,
        "datos": datos,
        "imagen": imagen,
    }

    # Configuración de cabeceras HTTP: Content-Type y Authorization Bearer opcional
    cabeceras = {"Content-Type": "application/json"}
    if token:
        cabeceras["Authorization"] = f"Bearer {token}"

    pedido = urllib.request.Request(
        f"{servidor.rstrip('/')}/getRemoteTask",
        data=json.dumps(cuerpo).encode("utf-8"),
        headers=cabeceras,
        method="POST",
    )

    try:
        # Peticiones exitosas (código 200)
        with urllib.request.urlopen(pedido, timeout=timeout) as respuesta:
            return json.loads(respuesta.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # En caso de error HTTP (4xx o 5xx), el servidor de todos modos devuelve
        # el cuerpo JSON estructurado con el sobre de error; se parsea y retorna normalmente
        return json.loads(error.read().decode("utf-8"))


def main() -> int:
    """Parsea los argumentos de línea de comandos, despacha la tarea e imprime la respuesta formateada."""
    parser = argparse.ArgumentParser(
        description="Cliente CLI para ejecución de tareas remotas en Hit 1",
    )
    # Argumentos posicionales obligatorios
    parser.add_argument("calculo", help="Operación a resolver: suma, resta, multiplicacion o division")
    parser.add_argument(
        "parametros",
        type=json.loads,
        help="Objeto JSON con los operandos, p. ej. '{\"a\": 3, \"b\": 4}'",
    )
    # Argumentos opcionales con flags
    parser.add_argument(
        "--datos",
        type=json.loads,
        default={},
        help="Objeto JSON con datos adicionales de contexto (opcional)",
    )
    parser.add_argument(
        "--imagen",
        required=True,
        help="Referencia Docker de la imagen de tarea (repositorio:tag o repositorio@digest)",
    )
    parser.add_argument(
        "--servidor",
        default=os.environ.get("TP2_SERVIDOR", "http://localhost:8080"),
        help="URL base del servidor de tareas (por defecto: $TP2_SERVIDOR o http://localhost:8080)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("TP2_TOKEN", ""),
        help="Token JWT para autenticación (Authorization: Bearer <token>)",
    )
    args = parser.parse_args()

    # Envío de la tarea remota
    try:
        respuesta = enviar_tarea(
            args.servidor,
            args.calculo,
            args.parametros,
            args.datos,
            args.imagen,
            token=args.token,
        )
    except urllib.error.URLError as error:
        sys.exit(f"no se pudo conectar con {args.servidor}: {error.reason}")

    # Salida estándar formateada con sangría para fácil lectura en consola
    print(json.dumps(respuesta, ensure_ascii=False, indent=2))

    # Retorna código de salida UNIX según el código HTTP recibido
    return 0 if respuesta.get("codigo") == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
