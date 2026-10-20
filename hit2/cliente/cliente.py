"""Cliente HTTP para Hit #2 con soporte para Relojes Lógicos de Lamport [LAM78].

Implementa la especificación de ordenamiento de eventos en sistemas distribuidos formulada por
Leslie Lamport en 1978 ("Time, Clocks, and the Ordering of Events in a Distributed System", CACM):

1. Regla 1 (Evento Local / Envío de Mensaje):
   - Antes de realizar cualquier evento local o emitir un mensaje al servidor, el cliente
     incrementa su reloj lógico local:
     L_cliente = L_cliente + 1
   - Adjunta este valor en la cabecera HTTP `X-Lamport-Clock`.

2. Regla 2 (Recepción de Mensaje):
   - Al recibir una respuesta del servidor con una marca temporal L_servidor (obtenida de la
     cabecera `X-Lamport-Clock` o del campo `lamport_ts` en el cuerpo JSON), el cliente
     sincroniza su reloj local:
     L_cliente = max(L_cliente, L_servidor) + 1
   - De esta manera se preserva la relación 'happened-before' (a -> b) entre el cliente y el servidor.
"""

import json
import urllib.request
import urllib.error
from typing import Any, Optional


class ClienteHit2:
    """Cliente para interactuar con la API del Servidor de Tareas (Hit #2).
    
    Mantiene el estado mutable del reloj lógico de Lamport y maneja la autenticación Bearer JWT.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        reloj_inicial: int = 0,
        token: Optional[str] = None
    ):
        """Inicializa el cliente HTTP.
        
        Parámetros:
        - base_url: URL base del servidor (ej. http://localhost:8080).
        - reloj_inicial: Valor inicial del reloj lógico de Lamport (por defecto 0).
        - token: Token JWT opcional para autenticación Bearer (Requisito R4).
        """
        self.base_url = base_url.rstrip("/")
        # Reloj escalar entero no negativo según la definición de Lamport
        self.reloj_lamport = reloj_inicial
        # Token Bearer para peticiones autenticadas
        self.token = token or ""

    def enviar_tarea(
        self,
        calculo: str,
        parametros: dict[str, Any],
        imagen: str = "cerberusdistribuido/tarea:1.0.0",
        datos: Optional[dict[str, Any]] = None,
        timeout: float = 60.0,
        token: Optional[str] = None
    ) -> dict[str, Any]:
        """Envía una tarea remota incrementando el reloj de Lamport local y actualizándolo con la respuesta.
        
        Secuencia de Protocolo:
        1. Regla 1 de Lamport: Incrementa el reloj local para el evento de envío (ts_envio = L + 1).
        2. Inyección de Cabeceras: Inyecta `X-Lamport-Clock: <ts_envio>` y `Authorization: Bearer <token>`.
        3. Despacho HTTP POST: Envía el JSON a `/getRemoteTask`.
        4. Recepción: Lee el código de respuesta y extrae la cabecera `X-Lamport-Clock` del servidor.
        5. Regla 2 de Lamport: Actualiza el reloj local a `max(L, ts_servidor) + 1`.
        """
        # Paso 1: Regla 1 de Lamport (Evento de envío)
        self.reloj_lamport += 1
        ts_envio = self.reloj_lamport

        # Estructuramos el payload de la tarea según la especificación del TP2
        payload = {
            "calculo": calculo,
            "parametros": parametros,
            "datos": datos or {},
            "imagen": imagen,
        }
        data_bytes = json.dumps(payload).encode("utf-8")

        # Adjuntamos cabecera de tipo de contenido y reloj lógico de Lamport
        headers = {
            "Content-Type": "application/json",
            "X-Lamport-Clock": str(ts_envio)
        }
        # Inyectamos el token de autenticación si está disponible
        token_a_usar = token if token is not None else self.token
        if token_a_usar:
            headers["Authorization"] = f"Bearer {token_a_usar}"

        url = f"{self.base_url}/getRemoteTask"
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")

        try:
            # Enviamos la petición y leemos la respuesta
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                estado = resp.status
                crudo = resp.read()
                header_ts = resp.headers.get("X-Lamport-Clock")
        except urllib.error.HTTPError as error:
            # Capturamos respuestas HTTP 4xx/5xx que aún contienen sobre JSON estructurado
            estado = error.code
            crudo = error.read()
            header_ts = error.headers.get("X-Lamport-Clock")

        # Parseamos el sobre JSON devuelto por el servidor
        respuesta = json.loads(crudo.decode("utf-8")) if crudo else {}

        # Paso 2: Regla 2 de Lamport al recibir la respuesta
        ts_remoto = 0
        if header_ts and header_ts.isdigit():
            ts_remoto = int(header_ts)
        elif isinstance(respuesta.get("contenido"), dict) and "lamport_ts" in respuesta["contenido"]:
            ts_remoto = int(respuesta["contenido"]["lamport_ts"])

        # Si el servidor devolvió un timestamp válido, avanzamos el reloj local
        if ts_remoto > 0:
            self.reloj_lamport = max(self.reloj_lamport, ts_remoto) + 1

        return {
            "codigo": estado,
            "contenido": respuesta.get("contenido", {}),
            "lamport_ts_cliente": self.reloj_lamport,
            "lamport_ts_servidor": ts_remoto
        }

    def health(self, token: Optional[str] = None) -> dict[str, Any]:
        """Consulta el estado de salud del servidor y sincroniza el reloj de Lamport."""
        # Regla 1 de Lamport: Incremento local por envío
        self.reloj_lamport += 1
        headers = {"X-Lamport-Clock": str(self.reloj_lamport)}
        token_a_usar = token if token is not None else self.token
        if token_a_usar:
            headers["Authorization"] = f"Bearer {token_a_usar}"

        req = urllib.request.Request(
            f"{self.base_url}/health",
            headers=headers,
            method="GET"
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            crudo = resp.read()
            header_ts = resp.headers.get("X-Lamport-Clock")
            respuesta = json.loads(crudo.decode("utf-8"))

        # Regla 2 de Lamport: Sincronización con el timestamp devuelto por el servidor
        ts_remoto = int(header_ts) if header_ts and header_ts.isdigit() else 0
        if ts_remoto > 0:
            self.reloj_lamport = max(self.reloj_lamport, ts_remoto) + 1

        return respuesta


def main():
    """Punto de entrada de línea de comandos para invocar tareas con Lamport."""
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(description="Cliente del Hit 2 con Relojes de Lamport")
    parser.add_argument("calculo", help="suma, resta, multiplicacion o division")
    parser.add_argument("parametros", type=json.loads, help="objeto JSON, p. ej. '{\"a\": 3, \"b\": 4}'")
    parser.add_argument("--datos", type=json.loads, default={}, help="objeto JSON con datos adicionales")
    parser.add_argument("--imagen", default="cerberusdistribuido/tarea:1.0.0", help="imagen Docker de la tarea")
    parser.add_argument("--servidor", default=os.environ.get("TP2_SERVIDOR", "http://localhost:8080"))
    parser.add_argument("--token", default=os.environ.get("TP2_TOKEN", ""), help="JWT (Authorization: Bearer), ver evidencias/emitir_token.py")
    args = parser.parse_args()

    cliente = ClienteHit2(args.servidor, token=args.token)
    try:
        resp = cliente.enviar_tarea(args.calculo, args.parametros, imagen=args.imagen, datos=args.datos)
    except urllib.error.URLError as error:
        sys.exit(f"no se pudo conectar con {args.servidor}: {error.reason}")

    print(json.dumps(resp, ensure_ascii=False, indent=2))
    return 0 if resp.get("codigo") == 200 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())

