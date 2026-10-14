"""Servicio tarea CPU-bound (Hit 2 — R5).

Ejecuta cómputos intensivos de CPU (criba de primos, Proof-of-Work SHA-256)
dentro del contenedor efímero.
Cumple el contrato del servicio tarea (hit2/contrato.md §5).
"""

import hashlib
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ParametrosInvalidos(Exception):
    pass


def _calcular_primos(limite: int) -> list[int]:
    if not isinstance(limite, int) or isinstance(limite, bool) or limite <= 1:
        raise ParametrosInvalidos("'limite' debe ser un entero mayor a 1")
    if limite > 1_000_000:
        raise ParametrosInvalidos("'limite' supera el máximo permitido (1000000)")

    # Criba de Eratóstenes
    es_primo = [True] * (limite + 1)
    es_primo[0] = es_primo[1] = False
    p = 2
    while p * p <= limite:
        if es_primo[p]:
            for i in range(p * p, limite + 1, p):
                es_primo[i] = False
        p += 1
    return [i for i, primo in enumerate(es_primo) if primo]


def _calcular_pow(dificultad: int, prefijo: str = "cerberus") -> dict:
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


def ejecutarTarea(calculo: str, parametros: dict):
    if not isinstance(parametros, dict):
        raise ParametrosInvalidos("'parametros' debe ser un objeto")

    t_inicio = time.perf_counter()
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
            self._responder(200, {"tarea": "ok", "tipo": "cpu"})
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
        except (ValueError, KeyError, TypeError, AttributeError):
            self._error(400, "PEDIDO_INVALIDO", "se espera un JSON con calculo y parametros")
        else:
            self._responder(200, {"resultado": resultado})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
