"""Microservicio de Tarea Computacional (Hit 2).

Servicio HTTP liviano diseñado para ejecutarse dentro del contenedor efímero de Docker
(`cerberusdistribuido/tarea`).

Decisiones de Arquitectura:
1. Sin Dependencias Externas (Pure Python Standard Library):
   - Utiliza exclusivamente `http.server.ThreadingHTTPServer` de la biblioteca estándar.
   - Reduce el tamaño de la imagen Docker a mínimos (<50 MB con Alpine/slim), acelera
     drásticamente el tiempo de `docker pull` y `container start`, y minimiza la superficie de ataque.
2. Contrato de Comunicación Inter-Contenedor (Contrato §5):
   - Expone `GET /health` en el puerto 8080 para que el servidor sondee la disponibilidad.
   - Expone `POST /ejecutarTarea` recibiendo `{"calculo": ..., "parametros": ..., "datos": ...}`.
   - Responde con el mismo formato de sobre uniforme: `{"codigo": <int>, "contenido": <dict>}`.
3. Protección Aritmética y Casos Borde:
   - Rechazo de booleanos: En Python `bool` es subclase de `int` (`isinstance(True, int) == True`).
     Se valida explícitamente `not isinstance(valor, bool)` para evitar que `True + 1` dé `2`.
   - División por cero (`ZeroDivisionError`) -> 422 PARAMETROS_INVALIDOS.
   - Desbordamiento numérico (`OverflowError`) o valores no finitos (`math.isfinite`) -> 422.
"""

import json
import math
import operator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict

# Diccionario de operaciones aritméticas soportadas por el motor de cálculo
CALCULOS: Dict[str, Callable[[float, float], float]] = {
    "suma": operator.add,
    "resta": operator.sub,
    "multiplicacion": operator.mul,
    "division": operator.truediv,
}


class ParametrosInvalidos(Exception):
    """Excepción de dominio que representa violación de reglas matemáticas o de tipos."""
    pass


def _es_numero(valor: Any) -> bool:
    """Verifica si el valor es numérico estricto (int o float), rechazando booleanos.

    ¿Por qué?
    En Python `bool` hereda de `int` (`issubclass(bool, int)` es True). Sin este filtro,
    `{"a": True, "b": 4}` sería evaluado como `1 + 4 = 5`, violando la rigurosidad del contrato.
    """
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def ejecutarTarea(calculo: str, parametros: dict) -> float:  # noqa: N802 (nombre del enunciado)
    """Aplica la operación matemática especificada sobre los operandos 'a' y 'b'.

    Parámetros:
        calculo: Identificador de la operación ('suma', 'resta', 'multiplicacion', 'division').
        parametros: Diccionario que debe contener {"a": <numero>, "b": <numero>}.

    Retorna:
        Resultado numérico del cálculo.

    Lanza:
        ParametrosInvalidos: Si el cálculo no existe, los operandos no son números,
                             o la operación produce división por cero o desbordamiento.
    """
    # 1. Validación de cálculo soportado
    if calculo not in CALCULOS:
        raise ParametrosInvalidos(f"cálculo desconocido: {calculo}")

    # 2. Extracción y validación estricta de operandos
    a, b = parametros.get("a"), parametros.get("b")
    if not (_es_numero(a) and _es_numero(b)):
        raise ParametrosInvalidos("'a' y 'b' tienen que ser números")

    # 3. Ejecución segura con captura de fallas aritméticas
    try:
        resultado = CALCULOS[calculo](a, b)
    except ZeroDivisionError:
        raise ParametrosInvalidos("división por cero") from None
    except OverflowError:
        raise ParametrosInvalidos("el resultado se va de rango") from None

    # 4. Verificación de números no finitos (NaN, +Infinity, -Infinity)
    if isinstance(resultado, float) and not math.isfinite(resultado):
        raise ParametrosInvalidos("el resultado se va de rango")

    return resultado


class Handler(BaseHTTPRequestHandler):
    """Manejador HTTP para atender las peticiones que el servidor envía al contenedor."""

    def _responder(self, codigo: int, contenido: Any) -> None:
        """Serializa y emite la respuesta empaquetada en el sobre contractual estándar."""
        datos = json.dumps({"codigo": codigo, "contenido": contenido}).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def _error(self, codigo: int, tipo: str, mensaje: str) -> None:
        """Emite una respuesta de error con la estructura oficial del sobre."""
        self._responder(codigo, {"error": {"tipo": tipo, "mensaje": mensaje}})

    def do_GET(self) -> None:  # noqa: N802
        """Atiende peticiones GET.

        Endpoints:
            /health: Devuelve 200 OK para que el lanzador detecte que el servicio está listo.
        """
        if self.path == "/health":
            self._responder(200, {"tarea": "ok"})
        else:
            self._error(404, "RUTA_INEXISTENTE", "ruta inexistente")

    def do_POST(self) -> None:  # noqa: N802
        """Atiende peticiones POST.

        Endpoints:
            /ejecutarTarea: Procesa el cálculo aritmético solicitado y devuelve el resultado.
        """
        if self.path != "/ejecutarTarea":
            self._error(404, "RUTA_INEXISTENTE", "ruta inexistente")
            return

        try:
            # Lectura del cuerpo HTTP respetando exactamente el tamaño especificado en Content-Length
            longitud = int(self.headers.get("Content-Length", 0))
            cuerpo_crudo = self.rfile.read(longitud)
            pedido = json.loads(cuerpo_crudo)

            # Ejecución de la lógica de dominio
            resultado = ejecutarTarea(pedido["calculo"], pedido["parametros"])
        except ParametrosInvalidos as error:
            # 422: Parámetros semánticamente inválidos (división por cero, NaN, no números)
            self._error(422, "PARAMETROS_INVALIDOS", str(error))
        except (ValueError, KeyError, TypeError, AttributeError):
            # 400: JSON corrupto o faltan campos obligatorios
            self._error(400, "PEDIDO_INVALIDO", "se espera un JSON con calculo y parametros")
        else:
            # 200: Éxito con el cálculo resuelto
            self._responder(200, {"resultado": resultado})


if __name__ == "__main__":
    # Inicia el servidor multi-hilo en todas las interfaces de red del contenedor (0.0.0.0:8080)
    # ThreadingHTTPServer despacha cada petición en un hilo separado evitando bloqueos
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
