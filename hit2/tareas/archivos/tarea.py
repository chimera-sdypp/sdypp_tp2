"""Servicio tarea de procesamiento de archivos (Hit 2 — R5).

Procesa una colección de V1 archivos de entrada y aplica sobre cada uno
un conjunto de V2 acciones (hash SHA-256, compresión zlib, conteo de palabras,
rotación de líneas, cálculo de entropía).
Cumple el contrato del servicio tarea (hit2/contrato.md §5).
"""

import hashlib
import json
import math
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ACCIONES_PERMITIDAS = {
    "hash_sha256",
    "comprimir",
    "contar_palabras",
    "rotar_lineas",
    "analisis_frecuencia",
}


class ParametrosInvalidos(Exception):
    pass


def _generar_contenido_archivo(item: dict, idx: int) -> str:
    """Obtiene el contenido provisto o sintetiza un payload determinista según tamaño."""
    if "contenido" in item and isinstance(item["contenido"], str):
        if len(item["contenido"]) > 512 * 1024:
            raise ParametrosInvalidos("el contenido del archivo supera el límite de 512 KB")
        return item["contenido"]
    tamano_kb = item.get("tamano_kb", 1)
    if not isinstance(tamano_kb, (int, float)) or tamano_kb <= 0:
        tamano_kb = 1
    if tamano_kb > 512:
        raise ParametrosInvalidos(f"tamano_kb ({tamano_kb} KB) supera el límite máximo permitido (512 KB)")
    # Generar texto con patrones pseudoaleatorios reproducibles
    bloque = f"Linea de datos {idx} para prueba de rendimiento Cerberus SDyPP 2026.\n"
    repeticiones = max(1, int(tamano_kb * 1024 / len(bloque)))
    return bloque * repeticiones


def _calcular_entropia(texto: str) -> float:
    if not texto:
        return 0.0
    frecuencias = {}
    for c in texto:
        frecuencias[c] = frecuencias.get(c, 0) + 1
    longitud = len(texto)
    return round(-sum((cnt / longitud) * math.log2(cnt / longitud) for cnt in frecuencias.values()), 3)


def _ejecutar_accion(accion: str, texto: str) -> dict:
    if accion == "hash_sha256":
        return {"hash": hashlib.sha256(texto.encode("utf-8")).hexdigest()}
    if accion == "comprimir":
        crudo = texto.encode("utf-8")
        comprimido = zlib.compress(crudo, level=6)
        ratio = round(len(comprimido) / max(1, len(crudo)), 3)
        return {"tamano_original": len(crudo), "tamano_comprimido": len(comprimido), "ratio": ratio}
    if accion == "contar_palabras":
        palabras = texto.split()
        lineas = texto.splitlines()
        return {"palabras": len(palabras), "lineas": len(lineas), "caracteres": len(texto)}
    if accion == "rotar_lineas":
        lineas = texto.splitlines()
        rotadas = lineas[::-1] if lineas else []
        return {"total_lineas": len(rotadas), "checksum": hashlib.md5("\n".join(rotadas).encode()).hexdigest()[:8]}
    if accion == "analisis_frecuencia":
        entropia = _calcular_entropia(texto)
        return {"entropia_shannon": entropia, "caracteres_unicos": len(set(texto))}
    raise ParametrosInvalidos(f"acción no soportada: {accion}")


def ejecutarTarea(calculo: str, parametros: dict):
    if calculo != "procesar_archivos":
        raise ParametrosInvalidos(f"cálculo desconocido: {calculo}")

    if not isinstance(parametros, dict):
        raise ParametrosInvalidos("'parametros' debe ser un objeto")

    archivos = parametros.get("archivos")
    acciones = parametros.get("acciones")

    if not isinstance(archivos, list) or len(archivos) == 0:
        raise ParametrosInvalidos("'archivos' debe ser una lista no vacía")
    if len(archivos) > 200:
        raise ParametrosInvalidos("la cantidad de archivos supera el límite permitido (200)")

    if not isinstance(acciones, list) or len(acciones) == 0:
        raise ParametrosInvalidos("'acciones' debe ser una lista no vacía")

    for acc in acciones:
        if not isinstance(acc, str) or acc not in ACCIONES_PERMITIDAS:
            raise ParametrosInvalidos(
                f"acción inválida '{acc}'. Permitidas: {sorted(ACCIONES_PERMITIDAS)}"
            )

    t_inicio = time.perf_counter()
    reporte_archivos = []
    total_ops = 0

    for idx, item in enumerate(archivos):
        if isinstance(item, str):
            item = {"id": item}
        elif not isinstance(item, dict):
            raise ParametrosInvalidos(f"el archivo en posición {idx} debe ser un objeto o string")

        archivo_id = item.get("id", f"archivo_{idx+1:03d}")
        contenido = _generar_contenido_archivo(item, idx)

        resultados_acciones = {}
        for accion in acciones:
            resultados_acciones[accion] = _ejecutar_accion(accion, contenido)
            total_ops += 1

        reporte_archivos.append({
            "id": archivo_id,
            "tamano_caracteres": len(contenido),
            "acciones": resultados_acciones,
        })

    duracion_ms = round((time.perf_counter() - t_inicio) * 1000, 2)
    return {
        "calculo": "procesar_archivos",
        "archivos_procesados": len(archivos),
        "acciones_por_archivo": len(acciones),
        "total_operaciones": total_ops,
        "tiempo_procesamiento_ms": duracion_ms,
        "resumen": reporte_archivos[:10],  # primer bloque para no inflar payload
    }


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
            self._responder(200, {"tarea": "ok", "tipo": "archivos"})
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
