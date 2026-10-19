"""Microservicio de Tarea de Cómputo (Hit 1/Hit 3): Contenedor Efímero de Ejecución.

Arquitectura y Ciclo de Vida:
1. DESPLIEGUE DINÁMICO AISLADO:
   Cada nodo ejecutor del cluster arranca una instancia de este contenedor Docker por cada tarea
   solicitada (`cerberusdistribuido/tarea:1.0.0`). El contenedor expone un servidor HTTP interno en el
   puerto 8080 acotado a la red interna Docker.
2. VERIFICACIÓN DE SALUD (HEALTHCHECK):
   El lanzador del nodo sondea `GET /health` hasta recibir HTTP 200 `{"codigo": 200, "contenido": {"tarea": "ok"}}`
   antes de inyectar la carga de trabajo.
3. PROTOCOLO DE CONTRATO (POST /ejecutarTarea):
   Recibe el cálculo ('suma', 'resta', 'multiplicacion', 'division') y los operandos `a` y `b`.
   Devuelve un sobre JSON estandarizado idéntico al contrato del servidor:
     - Éxito: `{"codigo": 200, "contenido": {"resultado": <numero>}}`
     - Error: `{"codigo": 422, "contenido": {"error": {"tipo": "PARAMETROS_INVALIDOS", "mensaje": ...}}}`
4. SEGURIDAD ARITMÉTICA Y NUMÉRICA:
   - Excluye booleanos explícitamente (en Python, `bool` hereda de `int`, por lo que `isinstance(True, int)` es True).
   - Captura `ZeroDivisionError` en divisiones por cero.
   - Captura `OverflowError` y floats infinitos o NaN (`math.isfinite()`).
"""

import json
import math
import operator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Mapa de operaciones aritméticas soportadas asociadas a funciones del módulo operator
CALCULOS = {
    "suma": operator.add,
    "resta": operator.sub,
    "multiplicacion": operator.mul,
    "division": operator.truediv,
}


class ParametrosInvalidos(Exception):
    """Excepción lanzada ante violaciones en los tipos o dominio de los operandos."""
    pass


def _es_numero(valor: object) -> bool:
    """Verifica que un valor sea estrictamente un entero o flotante, descartando booleanos.
    
    ¿Por qué se descarta `bool` explícitamente?
    En Python, la clase `bool` es una subclase de `int` (`issubclass(bool, int) is True`).
    Sin esta cláusula defensiva, `isinstance(True, int)` evaluaría a `True` y permitiría
    operar aritméticamente booleanos como enteros (ej. `True + 4 = 5`), violando el contrato de la API.
    """
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def ejecutarTarea(calculo: str, parametros: dict) -> int | float:  # noqa: N802 (nombre fijado por enunciado)
    """Ejecuta el cálculo solicitado sobre los parámetros 'a' y 'b'.
    
    Validaciones y Manejo de Errores:
    1. Existencia de la operación en el catálogo `CALCULOS`.
    2. Presencia y validación de tipo numérico estricto para `a` y `b`.
    3. Protección contra `ZeroDivisionError`.
    4. Protección contra `OverflowError` (ej. desbordamiento de punto flotante).
    5. Rechazo de números no finitos (`inf`, `-inf`, `nan`).
    """
    # Validación 1: Verificar que la operación solicitada sea una de las cuatro soportadas
    if calculo not in CALCULOS:
        raise ParametrosInvalidos(f"cálculo desconocido: {calculo}")

    # Obtención de los operandos del diccionario de parámetros
    a = parametros.get("a")
    b = parametros.get("b")

    # Validación 2: Ambos operandos deben existir y ser numéricos estrictos
    if not (_es_numero(a) and _es_numero(b)):
        raise ParametrosInvalidos("'a' y 'b' tienen que ser números")

    # Ejecución aritmética protegida contra excepciones matemáticas del runtime
    try:
        resultado = CALCULOS[calculo](a, b)
    except ZeroDivisionError:
        # Falla de dominio: división por cero
        raise ParametrosInvalidos("división por cero") from None
    except OverflowError:
        # Desbordamiento aritmético a nivel de CPU/IEEE-754
        raise ParametrosInvalidos("el resultado se va de rango") from None

    # Validación 3: Protección contra infinitos o NaN resultantes de operaciones con floats
    if isinstance(resultado, float) and not math.isfinite(resultado):
        raise ParametrosInvalidos("el resultado se va de rango")

    return resultado


class Handler(BaseHTTPRequestHandler):
    """Manejador HTTP para la API del contenedor de tarea."""

    def _responder(self, codigo: int, contenido: dict) -> None:
        """Serializa y transmite la respuesta en el sobre canónico `{"codigo": ..., "contenido": ...}`."""
        # Ensamblar el sobre uniforme
        sobre = {"codigo": codigo, "contenido": contenido}
        datos = json.dumps(sobre).encode("utf-8")

        # Emitir cabeceras HTTP
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()

        # Escribir el payload en el socket de red
        self.wfile.write(datos)

    def _error(self, codigo: int, tipo: str, mensaje: str) -> None:
        """Emite una respuesta de error con la estructura uniforme contractualmente exigida."""
        self._responder(codigo, {"error": {"tipo": tipo, "mensaje": mensaje}})

    def do_GET(self) -> None:
        """Maneja solicitudes GET entrantes."""
        if self.path == "/health":
            # Endpoint de healthcheck utilizado por el lanzador del servidor antes de despachar tareas
            self._responder(200, {"tarea": "ok"})
        else:
            # Cualquier otra ruta GET no existe en el contenedor
            self._error(404, "RUTA_INEXISTENTE", "ruta inexistente")

    def do_POST(self) -> None:
        """Maneja solicitudes POST entrantes para ejecución de tareas."""
        if self.path != "/ejecutarTarea":
            # Ruta no reconocida
            self._error(404, "RUTA_INEXISTENTE", "ruta inexistente")
            return

        try:
            # Leer el cuerpo completo de la petición a partir del largo indicado en Content-Length
            longitud = int(self.headers.get("Content-Length", 0))
            cuerpo_bytes = self.rfile.read(longitud)
            pedido = json.loads(cuerpo_bytes.decode("utf-8"))

            # Ejecutar el cálculo validando sus operandos
            resultado = ejecutarTarea(pedido["calculo"], pedido["parametros"])
        except ParametrosInvalidos as error:
            # Violación en las reglas de dominio matemático o tipos de datos (HTTP 422)
            self._error(422, "PARAMETROS_INVALIDOS", str(error))
        except (ValueError, KeyError, TypeError, AttributeError):
            # Error de sintaxis JSON o ausencia de claves requeridas en el payload (HTTP 400)
            self._error(400, "PEDIDO_INVALIDO", "se espera un JSON con calculo y parametros")
        else:
            # Cómputo exitoso: emitir el resultado en sobre HTTP 200
            self._responder(200, {"resultado": resultado})


if __name__ == "__main__":
    # Servidor HTTP multihilo escuchando en todas las interfaces del contenedor en el puerto 8080
    servidor = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    servidor.serve_forever()

