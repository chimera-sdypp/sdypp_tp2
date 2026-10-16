"""Servicio tarea de inyección de fallas y timeouts (Hit 2 — R5).

Permite probar intencionalmente:
1. Respuestas normales con latencia controlada (dormir).
2. Tareas que exceden el timeout configurado en el servidor (dormir > timeout).
3. Fallas internas 500 en el contenedor (explotar).
4. Rechazo de parámetros inválidos 422 (división por cero, parámetros malformados).

Cumple el contrato del servicio tarea (hit2/contrato.md §5).
"""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ParametrosInvalidos(Exception):
    pass


class ErrorSimulado(Exception):
    pass


def ejecutarTarea(calculo: str, parametros: dict):
    if not isinstance(parametros, dict):
        raise ParametrosInvalidos("'parametros' debe ser un objeto")

    if calculo == "dormir":
        segundos = parametros.get("segundos")
        if not isinstance(segundos, (int, float)) or isinstance(segundos, bool):
            raise ParametrosInvalidos("'segundos' debe ser un número")
        if segundos < 0:
            raise ParametrosInvalidos("'segundos' no puede ser negativo")
        if segundos > 300:
            raise ParametrosInvalidos("'segundos' excede el máximo permitido (300)")

        time.sleep(segundos)
        return {"calculo": "dormir", "segundos_dormidos": segundos, "estado": "desperté"}

    if calculo == "division":
        a = parametros.get("a")
        b = parametros.get("b")
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            raise ParametrosInvalidos("'a' y 'b' deben ser números")
        if b == 0:
            raise ParametrosInvalidos("división por cero")
        return {"calculo": "division", "resultado": a / b}

    if calculo == "explotar":
        # Simula un crash / error 500 no recuperable dentro del contenedor
        raise ErrorSimulado("falla crítica simulada en contenedor tarea")

    raise ParametrosInvalidos(f"cálculo desconocido: {calculo}")


class Handler(BaseHTTPRequestHandler):
    def _responder(self, codigo, contenido):
        datos = json.dumps({"codigo": codigo, "contenido": contenido}).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def _error(self, codigo, tipo, mensaje):
        self._responder(codigo, {"error": {"tipo": tipo, "mensaje": mensaje}})

    def do_GET(self):
        if self.path == "/health":
            self._responder(200, {"tarea": "ok", "tipo": "falla"})
        else:
            self._error(404, "RUTA_INEXISTENTE", "ruta inexistente")

    def do_POST(self):
        if self.path != "/ejecutarTarea":
            self._error(404, "RUTA_INEXISTENTE", "ruta inexistente")
            return
        try:
            longitud = int(self.headers.get("Content-Length", 0))
            pedido = json.loads(self.rfile.read(longitud))
            resultado = ejecutarTarea(pedido["calculo"], pedido.get("parametros", {}))
        except ParametrosInvalidos as error:
            self._error(422, "PARAMETROS_INVALIDOS", str(error))
        except ErrorSimulado as error:
            self._error(500, "ERROR_INTERNO", str(error))
        except (ValueError, KeyError, TypeError, AttributeError):
            self._error(400, "PEDIDO_INVALIDO", "se espera un JSON con calculo y parametros")
        else:
            self._responder(200, {"resultado": resultado})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
