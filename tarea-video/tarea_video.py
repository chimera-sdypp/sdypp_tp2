"""Servicio tarea-video: transcodifica un video a varias calidades con ffmpeg.

El servidor (Hit 2 o Hit 3) lo levanta como contenedor con el volumen de trabajos
montado en /trabajos, espera su GET /health y le manda el trabajo por
POST /ejecutarTarea. El video no viaja por HTTP: el pedido trae las rutas de la
entrada y de la salida dentro del volumen. Responde con el mismo sobre que el
servidor (anexo de contrato-video.md).
"""

import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RAIZ = Path(os.environ.get("TAREA_VIDEO_RAIZ", "/trabajos"))

# calidad (alto en píxeles) → (CRF, maxrate de video en kb/s, bitrate de audio en kb/s).
# Es la tabla de §2 del contrato: si cambia una, cambia la otra.
CALIDADES = {
    1080: (23, 5000, 128),
    720: (23, 3000, 128),
    480: (24, 1500, 96),
    360: (26, 800, 96),
    240: (28, 400, 64),
}

LINEAS_DE_ERROR = 20     # cuánto del stderr de ffmpeg vuelve en el mensaje
INTERVALO_PROGRESO = 1.0  # cada cuánto se reescribe progreso.json, como mucho


class ParametrosInvalidos(Exception):
    pass


class TranscodificacionFallida(Exception):
    pass


def _ruta(datos, clave, raiz):
    """La ruta `datos[clave]`, que tiene que quedar dentro de la raíz del volumen:
    la tarea no lee ni escribe nada fuera de /trabajos (ni con `..` ni con links)."""
    valor = datos.get(clave)
    if not isinstance(valor, str) or not valor:
        raise ParametrosInvalidos(f"falta datos.{clave}")
    ruta = Path(valor).resolve()
    if not ruta.is_relative_to(raiz.resolve()):
        raise ParametrosInvalidos(f"datos.{clave} tiene que estar dentro de {raiz}")
    return ruta


def validar(parametros, datos, raiz=RAIZ):
    """Valida el pedido → (calidades, entrada, salida, progreso, duracion_s o None)."""
    if not isinstance(parametros, dict) or not isinstance(datos, dict):
        raise ParametrosInvalidos("parametros y datos tienen que ser objetos")
    calidades = parametros.get("calidades")
    if not isinstance(calidades, list) or not calidades:
        raise ParametrosInvalidos("calidades tiene que ser una lista no vacía")
    for calidad in calidades:
        if isinstance(calidad, bool) or calidad not in CALIDADES:
            raise ParametrosInvalidos(
                f"calidad inválida: {calidad!r} (válidas: {', '.join(map(str, CALIDADES))})")
    if len(set(calidades)) != len(calidades):
        raise ParametrosInvalidos("calidades tiene valores repetidos")

    entrada = _ruta(datos, "entrada", raiz)
    if not entrada.is_file():
        raise ParametrosInvalidos(f"no existe la entrada {entrada}")
    salida = _ruta(datos, "salida", raiz)
    progreso = _ruta(datos, "progreso", raiz)

    duracion = datos.get("duracion_s")
    if duracion is not None and (isinstance(duracion, bool) or not isinstance(duracion, (int, float))
                                 or duracion <= 0):
        raise ParametrosInvalidos("duracion_s tiene que ser un número positivo")
    return calidades, entrada, salida, progreso, duracion


def sondear(ruta):
    """ffprobe de la primera pista de video → {"ancho", "alto", "duracion_s"}.
    `None` si el archivo no se puede leer como video."""
    proceso = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration", "-of", "json", str(ruta)],
        capture_output=True, text=True, check=False)
    if proceso.returncode != 0:
        return None
    try:
        info = json.loads(proceso.stdout)
        pista = info["streams"][0]
        duracion = info.get("format", {}).get("duration")
        return {"ancho": int(pista["width"]), "alto": int(pista["height"]),
                "duracion_s": float(duracion) if duracion not in (None, "N/A") else None}
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def hilos(cpu_max=Path("/sys/fs/cgroup/cpu.max")):
    """Cuántos hilos usar: las CPU que le dio Docker al contenedor (`--cpus`), no las del
    host. ffmpeg ve todos los núcleos del host y abre un hilo por núcleo en el decoder y
    en cada encoder, y cada hilo retiene frames: con 16 núcleos, un 4K a 5 calidades pasa
    de 1 GiB de memoria."""
    try:
        cuota, periodo = cpu_max.read_text().split()
        if cuota != "max":
            return max(1, -(-int(cuota) // int(periodo)))
    except (OSError, ValueError):
        pass
    return os.process_cpu_count() or 1


def comando(entrada, salida, calidades, n_hilos=1):
    """Un solo ffmpeg para todas las calidades: decodifica la entrada una vez y
    codifica una salida por calidad. El progreso sale por stdout (`-progress`)."""
    args = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error",
            "-y", "-threads", str(n_hilos), "-i", str(entrada), "-progress", "pipe:1"]
    for alto in calidades:
        crf, video_k, audio_k = CALIDADES[alto]
        args += [
            # `?`: si la entrada no tiene audio, esa salida tampoco, en lugar de fallar.
            "-map", "0:v:0", "-map", "0:a:0?",
            # -2: el ancho que mantiene la proporción, redondeado a par (lo pide H.264).
            "-vf", f"scale=-2:{alto}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-threads", str(n_hilos),
            "-maxrate", f"{video_k}k", "-bufsize", f"{2 * video_k}k", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", f"{audio_k}k",
            "-movflags", "+faststart",
            str(salida / f"{alto}.mp4"),
        ]
    return args


def escribir_progreso(ruta, progreso):
    """Escribe y renombra: el servidor nunca lee un progreso.json a medio escribir."""
    temporal = ruta.with_name(ruta.name + ".tmp")
    temporal.write_text(json.dumps({"progreso": progreso}), encoding="utf-8")
    os.replace(temporal, ruta)


def _seguir_progreso(stdout, ruta, duracion, reloj=time.monotonic):
    """Lee las líneas `clave=valor` de `-progress` y va actualizando progreso.json."""
    ultimo = None
    for linea in stdout:
        clave, _, valor = linea.strip().partition("=")
        if clave == "progress" and valor == "end":
            escribir_progreso(ruta, 100)
        elif clave == "out_time_us" and duracion and valor.isdigit():
            ahora = reloj()
            if ultimo is None or ahora - ultimo >= INTERVALO_PROGRESO:
                escribir_progreso(ruta, round(min(int(valor) / 1e6 / duracion * 100, 99.9), 1))
                ultimo = ahora


def _incompleta(obtenida, esperada):
    """Un original truncado no hace fallar a ffmpeg: termina con 0 y una salida más
    corta. Se compara la duración: más de 1 s y más de un 5 % de diferencia es un
    video que no se pudo leer entero (el margen cubre el redondeo de los frames)."""
    if not obtenida or not esperada:
        return False
    faltante = esperada - obtenida
    return faltante > 1 and faltante > 0.05 * esperada


def _borrar(archivos):
    for archivo in archivos.values():
        archivo.unlink(missing_ok=True)


def transcodificar(parametros, datos, raiz=RAIZ):
    """Transcodifica la entrada a cada calidad → {calidad: {archivo, bytes, ancho, alto}}."""
    calidades, entrada, salida, progreso, duracion = validar(parametros, datos, raiz)
    original = sondear(entrada)
    if original is None:
        raise ParametrosInvalidos("la entrada no se puede leer como video")
    # El servidor ya lo controla antes de aceptar el trabajo; acá es la segunda línea.
    mayores = [c for c in calidades if c > original["alto"]]
    if mayores:
        raise ParametrosInvalidos(
            f"calidad {max(mayores)} mayor que el original ({original['ancho']}x{original['alto']}): "
            "no se agranda")
    duracion = duracion or original["duracion_s"]

    salida.mkdir(parents=True, exist_ok=True)
    escribir_progreso(progreso, 0)
    proceso = subprocess.Popen(comando(entrada, salida, calidades, hilos()), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, errors="replace")
    # stderr en otro hilo: si se llena su buffer mientras se lee stdout, ffmpeg se traba.
    errores = deque(maxlen=LINEAS_DE_ERROR)
    lector = threading.Thread(target=lambda: errores.extend(proceso.stderr), daemon=True)
    lector.start()
    _seguir_progreso(proceso.stdout, progreso, duracion)
    codigo = proceso.wait()
    lector.join()

    archivos = {alto: salida / f"{alto}.mp4" for alto in calidades}
    if codigo != 0:
        _borrar(archivos)
        if codigo == -signal.SIGKILL:
            # Con --memory, el kernel mata a ffmpeg cuando se pasa (OOM): no deja stderr.
            raise TranscodificacionFallida(
                "ffmpeg se quedó sin memoria y el sistema lo terminó: pedir menos calidades "
                "o un original de menor resolución")
        detalle = "".join(errores).strip() or "sin salida de error"
        raise TranscodificacionFallida(f"ffmpeg terminó con código {codigo}: {detalle}")

    resultado = {}
    for alto, archivo in archivos.items():
        info = sondear(archivo)
        if info is None:
            _borrar(archivos)
            raise TranscodificacionFallida(f"ffmpeg terminó bien pero {archivo.name} no es un video válido")
        if _incompleta(info["duracion_s"], duracion):
            _borrar(archivos)
            raise TranscodificacionFallida(
                f"el original está dañado: se pudieron leer {info['duracion_s']:.1f} s de {duracion:.1f} s")
        resultado[str(alto)] = {"archivo": archivo.name, "bytes": archivo.stat().st_size,
                                "ancho": info["ancho"], "alto": info["alto"]}
    escribir_progreso(progreso, 100)
    return resultado


def version_ffmpeg():
    try:
        primera = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True,
                                 check=False).stdout.split("\n", 1)[0]
    except OSError:
        return None
    return primera.removeprefix("ffmpeg version ").split(" ", 1)[0] or None


class Handler(BaseHTTPRequestHandler):
    ffmpeg = None

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
            self._responder(200, {"tarea": "ok", "ffmpeg": self.ffmpeg})
        else:
            self._error(404, "RUTA_INEXISTENTE", "ruta inexistente")

    def do_POST(self):
        if self.path != "/ejecutarTarea":
            self._error(404, "RUTA_INEXISTENTE", "ruta inexistente")
            return
        try:
            pedido = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if pedido.get("calculo") != "transcodificar":
                raise ParametrosInvalidos(f"cálculo desconocido: {pedido.get('calculo')!r}")
            resultado = transcodificar(pedido.get("parametros"), pedido.get("datos"))
        except ParametrosInvalidos as error:
            self._error(422, "PARAMETROS_INVALIDOS", str(error))
        except TranscodificacionFallida as error:
            self._error(422, "TRANSCODIFICACION_FALLIDA", str(error))
        except (ValueError, TypeError, AttributeError):
            self._error(400, "PEDIDO_INVALIDO", "se espera un JSON con calculo, parametros y datos")
        except OSError as error:
            self._error(500, "ERROR_INTERNO", f"no se pudo ejecutar ffmpeg: {error}")
        else:
            self._responder(200, {"resultado": resultado})


if __name__ == "__main__":
    Handler.ffmpeg = version_ffmpeg()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
