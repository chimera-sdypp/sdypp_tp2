"""Servicio tarea del Hit 2: resuelve un cálculo recibido por HTTP.

El servidor lo levanta como contenedor, espera su GET /health y le manda el trabajo por
POST /ejecutarTarea. Responde con el mismo sobre que el servidor (hit2/contrato.md).
"""

import json
import math
import operator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CALCULOS = {
    "suma": operator.add,
    "resta": operator.sub,
    "multiplicacion": operator.mul,
    "division": operator.truediv,
}


class ParametrosInvalidos(Exception):
    pass


def _es_numero(valor):
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def ejecutarTarea(calculo, parametros):  # noqa: N802 (nombre del enunciado)
    """Aplica el cálculo a parametros["a"] y parametros["b"]."""
    if calculo not in CALCULOS:
        raise ParametrosInvalidos(f"cálculo desconocido: {calculo}")
    a, b = parametros.get("a"), parametros.get("b")
    if not (_es_numero(a) and _es_numero(b)):
        raise ParametrosInvalidos("'a' y 'b' tienen que ser números")
    try:
        resultado = CALCULOS[calculo](a, b)
    except ZeroDivisionError:
        raise ParametrosInvalidos("división por cero") from None
    except OverflowError:
        raise ParametrosInvalidos("el resultado se va de rango") from None
    if isinstance(resultado, float) and not math.isfinite(resultado):
        raise ParametrosInvalidos("el resultado se va de rango")
    return resultado


class Handler(BaseHTTPRequestHandler):
    def _responder(self, codigo, contenido):
        datos = json.dumps({"codigo": codigo, "contenido": contenido}).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def _error(self, codigo, tipo, mensaje):
        self._responder(codigo, {"error": {"tipo": tipo, "mensaje": mensaje}})

    def do_GET(self):
        if self.path == "/health":
            self._responder(200, {"tarea": "ok"})
        else:
            self._error(404, "RUTA_INEXISTENTE", "ruta inexistente")

    def do_POST(self):
        if self.path != "/ejecutarTarea":
            self._error(404, "RUTA_INEXISTENTE", "ruta inexistente")
            return
        try:
            pedido = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            resultado = ejecutarTarea(pedido["calculo"], pedido["parametros"])
        except ParametrosInvalidos as error:
            self._error(422, "PARAMETROS_INVALIDOS", str(error))
        except (ValueError, KeyError, TypeError, AttributeError):
            self._error(400, "PEDIDO_INVALIDO", "se espera un JSON con calculo y parametros")
        else:
            self._responder(200, {"resultado": resultado})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
