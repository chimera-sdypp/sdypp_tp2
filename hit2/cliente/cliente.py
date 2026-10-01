"""Cliente HTTP para Hit #2 con soporte para Relojes Lógicos de Lamport [LAM78].

Mantiene su propio reloj local de Lamport, incluye la cabecera 'X-Lamport-Clock' y el campo
'lamport_ts' en cada petición, y sincroniza su reloj local al recibir respuestas del servidor:
L_client = max(L_client, L_server) + 1
"""

import json
import urllib.request
import urllib.error
from typing import Any, Optional


class ClienteHit2:
    """Cliente para interactuar con la API del Servidor de Tareas (Hit #2)."""

    def __init__(self, base_url: str = "http://localhost:8080", reloj_inicial: int = 0):
        self.base_url = base_url.rstrip("/")
        self.reloj_lamport = reloj_inicial

    def _incrementar_reloj() -> int:
        pass

    def enviar_tarea(
        self,
        calculo: str,
        parametros: dict[str, Any],
        imagen: str = "cerberusdistribuido/tarea:1.0.0",
        datos: Optional[dict[str, Any]] = None,
        timeout: float = 60.0
    ) -> dict[str, Any]:
        """Envía una tarea remota incrementando el reloj de Lamport local y actualizándolo con la respuesta."""
        # 1. Incrementar el reloj local antes de enviar la petición (evento interno/envío)
        self.reloj_lamport += 1
        ts_envio = self.reloj_lamport

        payload = {
            "calculo": calculo,
            "parametros": parametros,
            "datos": datos or {},
            "imagen": imagen,
        }
        data_bytes = json.dumps(payload).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "X-Lamport-Clock": str(ts_envio)
        }

        url = f"{self.base_url}/getRemoteTask"
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                estado = resp.status
                crudo = resp.read()
                header_ts = resp.headers.get("X-Lamport-Clock")
        except urllib.error.HTTPError as error:
            estado = error.code
            crudo = error.read()
            header_ts = error.headers.get("X-Lamport-Clock")

        # Parsear sobre JSON
        respuesta = json.loads(crudo.decode("utf-8")) if crudo else {}

        # 2. Sincronizar reloj de Lamport local al recibir la respuesta
        ts_remoto = 0
        if header_ts and header_ts.isdigit():
            ts_remoto = int(header_ts)
        elif isinstance(respuesta.get("contenido"), dict) and "lamport_ts" in respuesta["contenido"]:
            ts_remoto = int(respuesta["contenido"]["lamport_ts"])

        if ts_remoto > 0:
            self.reloj_lamport = max(self.reloj_lamport, ts_remoto) + 1

        return {
            "codigo": estado,
            "contenido": respuesta.get("contenido", {}),
            "lamport_ts_cliente": self.reloj_lamport,
            "lamport_ts_servidor": ts_remoto
        }

    def health(self) -> dict[str, Any]:
        """Consulta el estado del servidor y su reloj de Lamport."""
        self.reloj_lamport += 1
        req = urllib.request.Request(
            f"{self.base_url}/health",
            headers={"X-Lamport-Clock": str(self.reloj_lamport)},
            method="GET"
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            crudo = resp.read()
            header_ts = resp.headers.get("X-Lamport-Clock")
            respuesta = json.loads(crudo.decode("utf-8"))

        ts_remoto = int(header_ts) if header_ts and header_ts.isdigit() else 0
        if ts_remoto > 0:
            self.reloj_lamport = max(self.reloj_lamport, ts_remoto) + 1

        return respuesta


def main():
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(description="Cliente del Hit 2 con Relojes de Lamport")
    parser.add_argument("calculo", help="suma, resta, multiplicacion o division")
    parser.add_argument("parametros", type=json.loads, help="objeto JSON, p. ej. '{\"a\": 3, \"b\": 4}'")
    parser.add_argument("--datos", type=json.loads, default={}, help="objeto JSON con datos adicionales")
    parser.add_argument("--imagen", default="cerberusdistribuido/tarea:1.0.0", help="imagen Docker de la tarea")
    parser.add_argument("--servidor", default=os.environ.get("TP2_SERVIDOR", "http://localhost:8080"))
    args = parser.parse_args()

    cliente = ClienteHit2(args.servidor)
    try:
        resp = cliente.enviar_tarea(args.calculo, args.parametros, imagen=args.imagen, datos=args.datos)
    except urllib.error.URLError as error:
        sys.exit(f"no se pudo conectar con {args.servidor}: {error.reason}")

    print(json.dumps(resp, ensure_ascii=False, indent=2))
    return 0 if resp.get("codigo") == 200 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())

