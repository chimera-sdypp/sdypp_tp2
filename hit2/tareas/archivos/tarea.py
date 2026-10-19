r"""Microservicio de Tarea: Procesamiento de Archivos y Métricas de Información (Requisito R5).

Arquitectura y Propósito:
1. EVALUACIÓN COMBINATORIA V1 x V2:
   Este contenedor implementa la carga de trabajo orientada a procesamiento intensivo de datos
   en memoria. Recibe un lote de hasta 200 archivos de texto (parámetro V1) y aplica sobre cada uno
   un subconjunto de acciones algorítmicas (parámetro V2).
2. CATÁLOGO DE ACCIONES SOPORTADAS (V2):
   - `hash_sha256`: Digest criptográfico SHA-256 para validación de integridad.
   - `comprimir`: Compresión en memoria mediante algoritmo zlib nivel 6, reportando ratio de compresión.
   - `contar_palabras`: Análisis léxico de palabras, líneas y longitud de caracteres.
   - `rotar_lineas`: Inversión de secuencias de líneas y cálculo de checksum MD5 de validación.
   - `analisis_frecuencia`: Estimación empírica de la Entropía de la Información de Shannon
     $H(X) = -\sum p(x) \log_2 p(x)$ y conteo de caracteres únicos.
3. LÍMITES DE SEGURIDAD Y MEMORIA (FAIL-SAFE):
   - Acota el tamaño máximo de cada archivo individual a 512 KiB para evitar desbordes de memoria en el contenedor.
   - Acota la cantidad total de archivos por lote a un máximo de 200.
   - Rechaza cualquier acción no registrada en el catálogo con error semántico 422 PARAMETROS_INVALIDOS.
"""

import hashlib
import json
import math
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Conjunto inmutable de acciones algorítmicas permitidas contractualmente
ACCIONES_PERMITIDAS = {
    "hash_sha256",
    "comprimir",
    "contar_palabras",
    "rotar_lineas",
    "analisis_frecuencia",
}


class ParametrosInvalidos(Exception):
    """Excepción lanzada cuando la solicitud viola las restricciones contractuales o límites de tamaño."""
    pass


def _generar_contenido_archivo(item: dict, idx: int) -> str:
    """Obtiene el contenido provisto o sintetiza un payload determinista según tamaño.
    
    Protecciones:
    - Comprueba que el contenido no supere los 512 KiB por archivo individual.
    - Si no viene contenido literal, genera bloques repetitivos reproducibles.
    """
    if "contenido" in item and isinstance(item["contenido"], str):
        if len(item["contenido"]) > 512 * 1024:
            raise ParametrosInvalidos("el contenido del archivo supera el límite de 512 KB")
        return item["contenido"]

    # Generación sintética determinista basada en el parámetro tamano_kb
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
    """Calcula la entropía de información de Shannon en bits por carácter.
    
    Fórmula: H = - sum(P(c) * log2(P(c)))
    """
    if not texto:
        return 0.0
    frecuencias = {}
    for c in texto:
        frecuencias[c] = frecuencias.get(c, 0) + 1
    longitud = len(texto)
    return round(-sum((cnt / longitud) * math.log2(cnt / longitud) for cnt in frecuencias.values()), 3)


def _ejecutar_accion(accion: str, texto: str) -> dict:
    """Aplica una transformación o análisis específico sobre el texto de un archivo."""
    if accion == "hash_sha256":
        # Hash criptográfico SHA-256
        return {"hash": hashlib.sha256(texto.encode("utf-8")).hexdigest()}
    if accion == "comprimir":
        # Compresión zlib estándar nivel 6
        crudo = texto.encode("utf-8")
        comprimido = zlib.compress(crudo, level=6)
        ratio = round(len(comprimido) / max(1, len(crudo)), 3)
        return {"tamano_original": len(crudo), "tamano_comprimido": len(comprimido), "ratio": ratio}
    if accion == "contar_palabras":
        # Análisis léxico de palabras, líneas y caracteres
        palabras = texto.split()
        lineas = texto.splitlines()
        return {"palabras": len(palabras), "lineas": len(lineas), "caracteres": len(texto)}
    if accion == "rotar_lineas":
        # Inversión de líneas y checksum MD5 rápido
        lineas = texto.splitlines()
        rotadas = lineas[::-1] if lineas else []
        return {"total_lineas": len(rotadas), "checksum": hashlib.md5("\n".join(rotadas).encode()).hexdigest()[:8]}
    if accion == "analisis_frecuencia":
        # Entropía de Shannon y cardinalidad del alfabeto utilizado
        entropia = _calcular_entropia(texto)
        return {"entropia_shannon": entropia, "caracteres_unicos": len(set(texto))}
    raise ParametrosInvalidos(f"acción no soportada: {accion}")


def ejecutarTarea(calculo: str, parametros: dict) -> dict:  # noqa: N802 (nombre fijado por enunciado)
    """Punto de entrada de ejecución de la tarea de archivos."""
    if calculo != "procesar_archivos":
        raise ParametrosInvalidos(f"cálculo desconocido: {calculo}")

    if not isinstance(parametros, dict):
        raise ParametrosInvalidos("'parametros' debe ser un objeto")

    archivos = parametros.get("archivos")
    acciones = parametros.get("acciones")

    # Validación de parámetro V1: lista de archivos
    if not isinstance(archivos, list) or len(archivos) == 0:
        raise ParametrosInvalidos("'archivos' debe ser una lista no vacía")
    if len(archivos) > 200:
        raise ParametrosInvalidos("la cantidad de archivos supera el límite permitido (200)")

    # Validación de parámetro V2: lista de acciones
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

    # Iterar sobre cada archivo del lote V1
    for idx, item in enumerate(archivos):
        if isinstance(item, str):
            item = {"id": item}
        elif not isinstance(item, dict):
            raise ParametrosInvalidos(f"el archivo en posición {idx} debe ser un objeto o string")

        archivo_id = item.get("id", f"archivo_{idx+1:03d}")
        contenido = _generar_contenido_archivo(item, idx)

        # Aplicar cada una de las V2 acciones sobre este archivo
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
    """Manejador HTTP para el contenedor de procesamiento de archivos."""

    def _responder(self, codigo: int, contenido: dict) -> None:
        """Serializa y emite el sobre estándar {"codigo": ..., "contenido": ...}."""
        datos = json.dumps({"codigo": codigo, "contenido": contenido}).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def _error(self, codigo: int, tipo: str, mensaje: str) -> None:
        """Emite error estructurado."""
        self._responder(codigo, {"error": {"tipo": tipo, "mensaje": mensaje}})

    def do_GET(self) -> None:
        """Endpoint de verificación de salud."""
        if self.path == "/health":
            self._responder(200, {"tarea": "ok", "tipo": "archivos"})
        else:
            self._error(404, "RUTA_INEXISTENTE", "ruta inexistente")

    def do_POST(self) -> None:
        """Endpoint de ejecución de tarea."""
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

