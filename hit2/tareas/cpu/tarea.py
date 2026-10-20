r"""Microservicio de Tarea: Cargas Intensivas de Cómputo CPU-Bound (Requisito R5).

Arquitectura y Propósito:
1. BENCHMARK DE CONSUMO PURO DE CPU:
   Esta tarea fue diseñada para evaluar la escalabilidad del servidor ante procesos
   que demandan 100% de ocupación de núcleo de procesador sin generar I/O de disco ni red.
2. OPERACIONES SOPORTADAS:
   - `primos`: Criba de Eratóstenes optimizada hasta un límite superior configurable ($N \le 1.000.000$).
     Permite generar saturación controlada de CPU proporcional al límite solicitado.
   - `pow`: Algoritmo de Prueba de Trabajo (Proof-of-Work) calculando hashes SHA-256 incrementales
     hasta hallar un nonce cuya salida comience con una cantidad prefijada de ceros hexadecimales (dificultad 1 a 7).
3. SEGURIDAD ARITMÉTICA Y NUMÉRICA:
   - Rechaza valores booleanos explícitamente (`isinstance(limite, bool)`).
   - Acota el límite superior de primos a 1.000.000 para evitar agotar el timeout global del servidor.
   - Acota la dificultad de PoW a 7 para evitar bucles de cómputo inmanejables.
"""

import hashlib
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ParametrosInvalidos(Exception):
    """Excepción lanzada cuando los parámetros numéricos violan las restricciones del contrato."""
    pass


def _calcular_primos(limite: int) -> list[int]:
    r"""Calcula todos los números primos menores o iguales a `limite` mediante la Criba de Eratóstenes.
    
    Complejidad Temporal: $O(N \log \log N)$.
    Protecciones:
    - Excluye booleanos y exige enteros mayores a 1.
    - Rechaza límites superiores a 1.000.000 (cota contractual de seguridad).
    """
    if not isinstance(limite, int) or isinstance(limite, bool) or limite <= 1:
        raise ParametrosInvalidos("'limite' debe ser un entero mayor a 1")
    if limite > 1_000_000:
        raise ParametrosInvalidos("'limite' supera el máximo permitido (1000000)")

    # Inicialización del vector booleano de la criba
    es_primo = [True] * (limite + 1)
    es_primo[0] = es_primo[1] = False
    p = 2
    while p * p <= limite:
        if es_primo[p]:
            # Marcar múltiplos sucesivos como no primos
            for i in range(p * p, limite + 1, p):
                es_primo[i] = False
        p += 1

    # Recolección de los enteros sobrevivientes
    return [i for i, primo in enumerate(es_primo) if primo]


def _calcular_pow(dificultad: int, prefijo: str = "cerberus") -> dict:
    """Ejecuta un desafío de Proof-of-Work calculando SHA-256 hasta hallar 'dificultad' ceros iniciales.
    
    Protecciones:
    - Acota la dificultad a un máximo de 7 (para prevenir congelamiento indefinido del worker).
    """
    if not isinstance(dificultad, int) or isinstance(dificultad, bool) or dificultad < 1:
        raise ParametrosInvalidos("'dificultad' debe ser un entero positivo")
    if dificultad > 7:
        raise ParametrosInvalidos("'dificultad' demasiado alta (máximo 7)")
    if not isinstance(prefijo, str) or not prefijo:
        raise ParametrosInvalidos("'prefijo' debe ser un string no vacío")

    patron = "0" * dificultad
    nonce = 0
    while True:
        candidato = f"{prefijo}:{nonce}".encode("utf-8")
        h = hashlib.sha256(candidato).hexdigest()
        if h.startswith(patron):
            return {"nonce": nonce, "hash": h, "dificultad": dificultad, "prefijo": prefijo}
        nonce += 1


def ejecutarTarea(calculo: str, parametros: dict) -> dict:  # noqa: N802 (nombre fijado por enunciado)
    """Punto de entrada de despacho para tareas de cómputo intensivo."""
    if not isinstance(parametros, dict):
        raise ParametrosInvalidos("'parametros' debe ser un objeto")

    t_inicio = time.perf_counter()

    # Cálculo 1: Criba de números primos
    if calculo == "primos":
        limite = parametros.get("limite")
        if limite is None:
            raise ParametrosInvalidos("falta parámetro obligatorio 'limite'")
        primos = _calcular_primos(limite)
        t_ms = round((time.perf_counter() - t_inicio) * 1000, 2)
        return {
            "calculo": "primos",
            "limite": limite,
            "total_primos": len(primos),
            "max_primo": primos[-1] if primos else None,
            "tiempo_cpu_ms": t_ms,
        }

    # Cálculo 2: Prueba de Trabajo (Proof-of-Work)
    if calculo == "pow":
        dificultad = parametros.get("dificultad")
        prefijo = parametros.get("prefijo", "cerberus")
        if dificultad is None:
            raise ParametrosInvalidos("falta parámetro obligatorio 'dificultad'")
        res = _calcular_pow(dificultad, prefijo)
        t_ms = round((time.perf_counter() - t_inicio) * 1000, 2)
        res["tiempo_cpu_ms"] = t_ms
        return res

    raise ParametrosInvalidos(f"cálculo desconocido: {calculo}")


class Handler(BaseHTTPRequestHandler):
    """Manejador HTTP para el contenedor CPU-bound."""

    def _responder(self, codigo: int, contenido: dict) -> None:
        """Serializa y transmite la respuesta en sobre JSON estándar."""
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
            self._responder(200, {"tarea": "ok", "tipo": "cpu"})
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
        except (ValueError, KeyError, TypeError, AttributeError):
            self._error(400, "PEDIDO_INVALIDO", "se espera un JSON con calculo y parametros")
        else:
            self._responder(200, {"resultado": resultado})


if __name__ == "__main__":
    # Iniciar servidor HTTP en el puerto 8080 del contenedor
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

