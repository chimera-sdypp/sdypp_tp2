"""Microservicio de Tarea: Inyección de Fallas, Timeouts y Resiliencia (Requisito R5).

Arquitectura y Propósito:
1. ARNÉS DE PRUEBA DE RESILIENCIA DEL SERVIDOR:
   Este microservicio fue concebido expresamente para validar cómo reacciona el pool
   de workers del servidor ante condiciones anómalas en el contenedor de ejecución.
2. ESCENARIOS SIMULADOS:
   - `dormir`: Introduce retardos arbitrarios (`time.sleep(segundos)`). Permite evaluar
     la expiración del timeout configurable del servidor (`TP2_TIMEOUT_TAREA_S`) y comprobar
     que el lanzador mate el contenedor y libere el worker del pool de inmediato.
   - `division`: Valida la propagación de errores de dominio matemático (división por cero -> 422).
   - `explotar`: Lanza deliberadamente una excepción `ErrorSimulado`, provocando un HTTP 500
     dentro del microservicio para verificar que el servidor capture el fallo y limpie el entorno.
3. ESTRICTA CONFORMIDAD CONTRACTUAL:
   Emite el sobre canónico {"codigo": ..., "contenido": ...} en todas las respuestas.
"""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ParametrosInvalidos(Exception):
    """Excepción lanzada cuando los parámetros no respetan tipos o cotas válidas."""
    pass


class ErrorSimulado(Exception):
    """Excepción utilizada para provocar intencionalmente una falla interna 500 no recuperable."""
    pass


def ejecutarTarea(calculo: str, parametros: dict) -> dict:  # noqa: N802 (nombre fijado por enunciado)
    """Punto de despacho de la tarea de fallas e inyección de latencia."""
    if not isinstance(parametros, dict):
        raise ParametrosInvalidos("'parametros' debe ser un objeto")

    # Escenario 1: Latencia controlada para pruebas de timeout del pool
    if calculo == "dormir":
        segundos = parametros.get("segundos")
        if not isinstance(segundos, (int, float)) or isinstance(segundos, bool):
            raise ParametrosInvalidos("'segundos' debe ser un número")
        if segundos < 0:
            raise ParametrosInvalidos("'segundos' no puede ser negativo")
        if segundos > 300:
            raise ParametrosInvalidos("'segundos' excede el máximo permitido (300)")

        # Bloqueo sincrónico para simular procesamiento demorado
        time.sleep(segundos)
        return {"calculo": "dormir", "segundos_dormidos": segundos, "estado": "desperté"}

    # Escenario 2: Operación aritmética con validación de división por cero
    if calculo == "division":
        a = parametros.get("a")
        b = parametros.get("b")
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            raise ParametrosInvalidos("'a' y 'b' deben ser números")
        if b == 0:
            raise ParametrosInvalidos("división por cero")
        return {"calculo": "division", "resultado": a / b}

    # Escenario 3: Crash simulado no recuperable
    if calculo == "explotar":
        # Simula un crash / error 500 no recuperable dentro del contenedor
        raise ErrorSimulado("falla crítica simulada en contenedor tarea")

    raise ParametrosInvalidos(f"cálculo desconocido: {calculo}")


class Handler(BaseHTTPRequestHandler):
    """Manejador HTTP para el contenedor de inyección de fallas."""

    def _responder(self, codigo: int, contenido: dict) -> None:
        """Serializa y emite el sobre estándar en formato JSON."""
        datos = json.dumps({"codigo": codigo, "contenido": contenido}).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def _error(self, codigo: int, tipo: str, mensaje: str) -> None:
        """Emite sobre estructurado de error."""
        self._responder(codigo, {"error": {"tipo": tipo, "mensaje": mensaje}})

    def do_GET(self) -> None:
        """Endpoint de salud consultado por el lanzador antes de inyectar trabajo."""
        if self.path == "/health":
            self._responder(200, {"tarea": "ok", "tipo": "falla"})
        else:
            self._error(404, "RUTA_INEXISTENTE", "ruta inexistente")

    def do_POST(self) -> None:
        """Endpoint de ejecución remota de tareas."""
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
    # Iniciar servidor HTTP en el puerto 8080 del contenedor
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

