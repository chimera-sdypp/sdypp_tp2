"""Servicio tarea de PRUEBA para los tests de integración del servidor.

No es el servicio tarea del Hit 1 (ese se publica en Docker Hub): es un doble
que cumple el mismo contrato (hit3/contrato.md §5) y agrega cálculos para forzar
cada caso de falla.
"""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _error(codigo, tipo, mensaje):
    return codigo, {"error": {"tipo": tipo, "mensaje": mensaje}}


def ejecutarTarea(calculo, parametros):  # noqa: N802 (nombre del enunciado)
    """Devuelve (código, contenido)."""
    if calculo == "suma":
        return 200, {"resultado": parametros["a"] + parametros["b"]}
    if calculo == "division":
        if parametros["b"] == 0:
            return _error(422, "PARAMETROS_INVALIDOS", "división por cero")
        return 200, {"resultado": parametros["a"] / parametros["b"]}
    if calculo == "dormir":
        time.sleep(parametros["segundos"])
        return 200, {"resultado": "desperté"}
    if calculo == "explotar":
        return _error(500, "ERROR_INTERNO", "falla simulada")
    return _error(400, "CALCULO_DESCONOCIDO", f"cálculo desconocido: {calculo}")


class Handler(BaseHTTPRequestHandler):
    def _responder(self, codigo, contenido):
        """Mismo sobre que el servidor: codigo + contenido, salga bien o mal."""
        datos = json.dumps({"codigo": codigo, "contenido": contenido}).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def do_GET(self):
        if self.path == "/health":
            self._responder(200, {"tarea": "ok"})
        else:
            self._responder(*_error(404, "RUTA_INEXISTENTE", "no existe"))

    def do_POST(self):
        if self.path != "/ejecutarTarea":
            self._responder(*_error(404, "RUTA_INEXISTENTE", "no existe"))
            return
        pedido = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        try:
            self._responder(*ejecutarTarea(pedido["calculo"], pedido["parametros"]))
        except (KeyError, TypeError) as error:
            self._responder(*_error(400, "PARAMETROS_INVALIDOS", f"parámetros inválidos: {error}"))

    def log_message(self, formato, *args):
        print(formato % args, flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
