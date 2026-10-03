"""Cliente del Hit 3: manda una tarea al cluster (por nginx) por POST JSON y muestra la respuesta.

    python cliente.py suma '{"a": 3, "b": 4}' --imagen usuario/tarea:1.0.0
    python cliente.py division '{"a": 1, "b": 0}' --imagen usuario/tarea:1.0.0 --servidor http://host:8080

Sale con 0 si la tarea terminó (codigo 200) y con 1 si no.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def enviar_tarea(servidor, calculo, parametros, datos, imagen, timeout=120.0):
    """POST /getRemoteTask. Devuelve el sobre {codigo, contenido} del servidor."""
    cuerpo = {"calculo": calculo, "parametros": parametros, "datos": datos, "imagen": imagen}
    pedido = urllib.request.Request(
        f"{servidor.rstrip('/')}/getRemoteTask",
        data=json.dumps(cuerpo).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(pedido, timeout=timeout) as respuesta:
            return json.loads(respuesta.read())
    except urllib.error.HTTPError as error:  # 4xx/5xx: el servidor igual manda el sobre
        return json.loads(error.read())


def main():
    parser = argparse.ArgumentParser(description="Cliente del Hit 3")
    parser.add_argument("calculo", help="suma, resta, multiplicacion o division")
    parser.add_argument("parametros", type=json.loads, help="objeto JSON, p. ej. '{\"a\": 3, \"b\": 4}'")
    parser.add_argument("--datos", type=json.loads, default={}, help="objeto JSON con datos adicionales")
    parser.add_argument("--imagen", required=True, help="imagen Docker de la tarea (repositorio:tag)")
    parser.add_argument("--servidor", default=os.environ.get("TP2_SERVIDOR", "http://localhost:8080"))
    args = parser.parse_args()

    try:
        respuesta = enviar_tarea(args.servidor, args.calculo, args.parametros, args.datos, args.imagen)
    except urllib.error.URLError as error:
        sys.exit(f"no se pudo conectar con {args.servidor}: {error.reason}")
    print(json.dumps(respuesta, ensure_ascii=False, indent=2))
    return 0 if respuesta.get("codigo") == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
