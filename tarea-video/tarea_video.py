"""Microservicio de Transcodificación de Video Multi-Calidad (Requisito Video).

Arquitectura y Principios de Diseño:
1. DESACOPLAMIENTO DE TRÁFICO HTTP vs. I/O DE ARCHIVOS:
   Los archivos de video en alta definición (1080p, 4K) ocupan decenas o cientos de megabytes.
   Transmitirlos dentro de un payload JSON HTTP saturaría la memoria del servidor y la red.
   Por este motivo, el cliente sube previamente el video al volumen compartido `/trabajos`,
   y la petición `POST /ejecutarTarea` únicamente transmite rutas locales seguras y metadatos.
2. PIPELINE DE UN SOLO PASE (SINGLE-PASS MULTI-OUTPUT):
   En lugar de ejecutar ffmpeg 5 veces por separado (lo cual decodificaría el archivo original
   5 veces consumiendo un 500% de CPU innecesaria), se invoca un único proceso `ffmpeg` con
   múltiples salidas concurrentes (`-map 0:v:0 -vf scale=-2:1080 ... -map 0:v:0 -vf scale=-2:720 ...`).
   El decoder lee el frame de entrada una sola vez y lo distribuye a los respectivos encoders.
3. CONFINAMIENTO DE RECURSOS SEGÚN CGROUPS V2:
   FFmpeg por defecto detecta todas las CPUs físicas del host y crea pools masivos de hilos
   en cada decoder y encoder. En entornos contenerizados con límites `--cpus`, esto satura la
   memoria RAM y el scheduler de Linux. La función `hilos()` consulta directamente la cuota CFS
   en `/sys/fs/cgroup/cpu.max` para acotar el paralelismo a la capacidad real asignada.
4. SEGURIDAD CONTRA ESCAPE DE VOLUMEN (PATH TRAVERSAL):
   Las rutas de entrada, salida y progreso se resuelven de forma canónica (`resolve()`) y se
   valida estrictamente que sean descendientes de `/trabajos` (`is_relative_to`), neutralizando
   intentos de lectura de archivos sensibles del sistema (ej. `/etc/passwd` o `../../`).
5. ATOMICIDAD EN LA PERSISTENCIA DEL PROGRESO:
   La telemetría de avance se escribe primero en un archivo temporal (`.tmp`) y se aplica
   mediante renombrado atómico (`os.replace`), garantizando que el servidor web nunca lea un
   JSON parcial o truncado a mitad de escritura.
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

# Directorio raíz del volumen compartido donde se alojan los videos
RAIZ = Path(os.environ.get("TAREA_VIDEO_RAIZ", "/trabajos"))

# Matriz canónica de perfiles de codificación (alto en píxeles -> (CRF, bitrate video kb/s, bitrate audio kb/s))
# Conforme a la especificación formal del contrato (contrato-video.md §2).
CALIDADES = {
    1080: (23, 5000, 128),
    720: (23, 3000, 128),
    480: (24, 1500, 96),
    360: (26, 800, 96),
    240: (28, 400, 64),
}

# Líneas de error a retener en la cola circular de stderr en caso de falla de ffmpeg
LINEAS_DE_ERROR = 20

# Intervalo mínimo de refresco de progreso en disco (1 segundo) para evitar saturación de I/O
INTERVALO_PROGRESO = 1.0


class ParametrosInvalidos(Exception):
    """Excepción lanzada cuando la solicitud viola las restricciones del contrato o seguridad."""
    pass


class TranscodificacionFallida(Exception):
    """Excepción lanzada cuando FFmpeg o la integridad del video fallan durante el proceso."""
    pass


def _ruta(datos: dict, clave: str, raiz: Path) -> Path:
    """Resuelve y convalida que una ruta resida estrictamente dentro del volumen seguro.
    
    Mitigación de Vulnerabilidad:
    Previene ataques de directory traversal (`../`) y accesos indebidos fuera de la raíz `/trabajos`.
    """
    valor = datos.get(clave)
    if not isinstance(valor, str) or not valor:
        raise ParametrosInvalidos(f"falta datos.{clave}")
    
    # Resolver ruta absoluta y eliminar enlaces simbólicos o componentes relativos
    ruta = Path(valor).resolve()
    
    # Comprobar contención estricta dentro del directorio raíz autorizado
    if not ruta.is_relative_to(raiz.resolve()):
        raise ParametrosInvalidos(f"datos.{clave} tiene que estar dentro de {raiz}")
    
    return ruta


def validar(parametros: dict, datos: dict, raiz: Path = RAIZ) -> tuple[list[int], Path, Path, Path, float | None]:
    """Valida exhaustivamente los parámetros y metadatos de la tarea de transcodificación.
    
    Reglas Contractuales:
    1. `parametros` y `datos` deben ser objetos JSON válidos (diccionarios).
    2. `calidades` debe ser una lista no vacía, sin repetidos y conteniendo solo alturas válidas (1080..240).
    3. No se admiten booleanos como valores de calidad (defensa `bool` is subclass of `int`).
    4. La ruta de `entrada` debe existir físicamente como archivo regular.
    5. Las rutas de `salida` y `progreso` deben residir dentro del volumen permitido.
    6. Si se provee `duracion_s`, debe ser un número estrictamente positivo.
    """
    if not isinstance(parametros, dict) or not isinstance(datos, dict):
        raise ParametrosInvalidos("parametros y datos tienen que ser objetos")

    calidades = parametros.get("calidades")
    if not isinstance(calidades, list) or not calidades:
        raise ParametrosInvalidos("calidades tiene que ser una lista no vacía")

    # Validar cada resolución solicitada
    for calidad in calidades:
        if isinstance(calidad, bool) or calidad not in CALIDADES:
            raise ParametrosInvalidos(
                f"calidad inválida: {calidad!r} (válidas: {', '.join(map(str, CALIDADES))})"
            )

    # Validar que no existan resoluciones duplicadas en la lista
    if len(set(calidades)) != len(calidades):
        raise ParametrosInvalidos("calidades tiene valores repetidos")

    # Validación de rutas dentro del volumen compartido
    entrada = _ruta(datos, "entrada", raiz)
    if not entrada.is_file():
        raise ParametrosInvalidos(f"no existe la entrada {entrada}")
    salida = _ruta(datos, "salida", raiz)
    progreso = _ruta(datos, "progreso", raiz)

    # Validación de duración estimada opcional
    duracion = datos.get("duracion_s")
    if duracion is not None and (
        isinstance(duracion, bool) or not isinstance(duracion, (int, float)) or duracion <= 0
    ):
        raise ParametrosInvalidos("duracion_s tiene que ser un número positivo")

    return calidades, entrada, salida, progreso, duracion


def sondear(ruta: Path) -> dict | None:
    """Inspecciona el contenedor multimedia mediante ffprobe para obtener dimensiones y duración.
    
    Retorna:
      Diccionario con `ancho`, `alto` y `duracion_s` de la primera pista de video (`v:0`),
      o `None` si el archivo está corrupto, truncado o no es un formato de video válido.
    """
    proceso = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json", str(ruta)
        ],
        capture_output=True,
        text=True,
        check=False
    )
    if proceso.returncode != 0:
        return None

    try:
        info = json.loads(proceso.stdout)
        pista = info["streams"][0]
        duracion = info.get("format", {}).get("duration")
        return {
            "ancho": int(pista["width"]),
            "alto": int(pista["height"]),
            "duracion_s": float(duracion) if duracion not in (None, "N/A") else None,
        }
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def hilos(cpu_max: Path = Path("/sys/fs/cgroup/cpu.max")) -> int:
    """Determina la cantidad óptima de hilos para FFmpeg basándose en los límites de Docker (cgroups v2).
    
    Justificación Arquitectural:
    En máquinas con muchos cores (ej. 16 o 32 núcleos), FFmpeg por defecto asigna 1 hilo por core
    a cada decoder y a cada una de las 5 salidas encoder. Esto causa que un solo proceso retenga
    decenas de frames concurrentes en RAM, excediendo fácilmente 1 GiB de memoria y provocando
    un OOM Kill (Out Of Memory) por parte del kernel. Al consultar `/sys/fs/cgroup/cpu.max`,
    se respetan exactamente las CPUs virtuales asignadas por Docker (`--cpus`).
    """
    try:
        cuota_texto, periodo_texto = cpu_max.read_text().split()
        if cuota_texto != "max":
            # División entera con redondeo hacia arriba: ceil(cuota / periodo)
            return max(1, -(-int(cuota_texto) // int(periodo_texto)))
    except (OSError, ValueError):
        pass

    # Si cgroups no está disponible o no tiene límite, utilizar los cores asignados al proceso
    return os.process_cpu_count() or 1


def comando(entrada: Path, salida: Path, calidades: list[int], n_hilos: int = 1) -> list[str]:
    """Construye la línea de comandos optimizada de FFmpeg para transcodificación multi-salida.
    
    Técnicas de Optimización y Robustez Aplicadas:
    1. `-progress pipe:1`: Emite métricas de progreso estructuradas por stdout en tiempo real.
    2. `-map 0:a:0?`: El modificador '?' tolera videos de entrada mudos (sin pista de audio)
       sin provocar la caída del comando de transcodificación.
    3. `-vf scale=-2:{alto}`: Escala la altura a la calidad pedida y ajusta el ancho manteniendo
       el aspecto original, forzando que el ancho sea un número par (requisito de H.264 / yuv420p).
    4. `-movflags +faststart`: Reubica el átomo 'moov' al comienzo del archivo MP4, habilitando
       reproducción progresiva instantánea (streaming) en navegadores web.
    """
    args = [
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error",
        "-y", "-threads", str(n_hilos), "-i", str(entrada), "-progress", "pipe:1"
    ]
    for alto in calidades:
        crf, video_k, audio_k = CALIDADES[alto]
        args += [
            # Mapear video y audio (con '?' para tolerar videos mudos)
            "-map", "0:v:0", "-map", "0:a:0?",
            # Filtro de escalado respetando relación de aspecto y paridad de píxeles
            "-vf", f"scale=-2:{alto}",
            # Configuración del encoder de video H.264
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-threads", str(n_hilos),
            "-maxrate", f"{video_k}k", "-bufsize", f"{2 * video_k}k", "-pix_fmt", "yuv420p",
            # Configuración del encoder de audio AAC
            "-c:a", "aac", "-b:a", f"{audio_k}k",
            # Habilitar reproducción web inmediata
            "-movflags", "+faststart",
            # Archivo MP4 de destino para esta resolución
            str(salida / f"{alto}.mp4"),
        ]
    return args


def escribir_progreso(ruta: Path, progreso: float | int) -> None:
    """Escribe el progreso porcentual en disco mediante reemplazo atómico.
    
    Evita lecturas concurrentes corruptas por parte del servidor web escribiendo
    primero a un archivo temporal `.tmp` y ejecutando luego un `os.replace`.
    """
    temporal = ruta.with_name(ruta.name + ".tmp")
    temporal.write_text(json.dumps({"progreso": progreso}), encoding="utf-8")
    os.replace(temporal, ruta)


def _seguir_progreso(stdout, ruta: Path, duracion: float | None, reloj=time.monotonic) -> None:
    """Parsea la salida de `-progress pipe:1` de FFmpeg y actualiza progreso.json periódicamente."""
    ultimo = None
    for linea in stdout:
        clave, _, valor = linea.strip().partition("=")
        if clave == "progress" and valor == "end":
            # FFmpeg completó la codificación de todas las salidas
            escribir_progreso(ruta, 100)
        elif clave == "out_time_us" and duracion and valor.isdigit():
            ahora = reloj()
            # Limitar la tasa de escritura a disco a como máximo una vez por segundo
            if ultimo is None or ahora - ultimo >= INTERVALO_PROGRESO:
                segundos_procesados = int(valor) / 1e6
                porcentaje = min(segundos_procesados / duracion * 100, 99.9)
                escribir_progreso(ruta, round(porcentaje, 1))
                ultimo = ahora


def _incompleta(obtenida: float | None, esperada: float | None) -> bool:
    """Detecta si un video original estaba dañado o truncado.
    
    FFmpeg no falla ante un video original truncado: simplemente codifica hasta donde puede,
    termina con código 0 y genera una salida más corta. Comparamos la duración: si falta más de 1s
    y la diferencia supera el 5%, se clasifica como video corrupto.
    """
    if not obtenida or not esperada:
        return False
    faltante = esperada - obtenida
    return faltante > 1 and faltante > 0.05 * esperada


def _borrar(archivos: dict[int, Path]) -> None:
    """Elimina archivos generados ante cualquier fallo para no dejar artefactos residuales."""
    for archivo in archivos.values():
        archivo.unlink(missing_ok=True)


def transcodificar(parametros: dict, datos: dict, raiz: Path = RAIZ) -> dict:
    """Función orquestadora principal de la transcodificación de video.
    
    Flujo de Ejecución:
    1. Valida las entradas, rutas y perfiles de calidad solicitados.
    2. Sondea el video original con ffprobe para conocer sus dimensiones nativas.
    3. Aplica la regla de NO-UPSCALING (no agrandar el video a resoluciones superiores a la original).
    4. Lanza FFmpeg en un subproceso con lectura concurrente de stderr (evita bloqueo por pipe buffer lleno).
    5. Monitorea el progreso y lo escribe atómicamente a disco.
    6. Valida la integridad y duración de cada archivo generado.
    7. Retorna el diccionario estructurado con los detalles de cada calidad generada.
    """
    calidades, entrada, salida, progreso, duracion = validar(parametros, datos, raiz)

    # Inspección del video de entrada
    original = sondear(entrada)
    if original is None:
        raise ParametrosInvalidos("la entrada no se puede leer como video")

    # Regla de calidad: Prohibido agrandar (upscaling) por encima de la resolución original
    mayores = [c for c in calidades if c > original["alto"]]
    if mayores:
        raise ParametrosInvalidos(
            f"calidad {max(mayores)} mayor que el original ({original['ancho']}x{original['alto']}): "
            "no se agranda"
        )

    # Duración de referencia para el cálculo de porcentaje
    duracion = duracion or original["duracion_s"]

    # Crear directorio de salida y marcar progreso inicial en 0%
    salida.mkdir(parents=True, exist_ok=True)
    escribir_progreso(progreso, 0)

    # Invocar proceso ffmpeg con pipes para stdout y stderr
    proceso = subprocess.Popen(
        comando(entrada, salida, calidades, hilos()),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace"
    )

    # Lector concurrente de stderr: si el buffer de stderr del kernel (64 KiB) se llena
    # mientras se lee stdout, el subproceso FFmpeg se cuelga indefinidamente en deadlock.
    errores = deque(maxlen=LINEAS_DE_ERROR)
    lector = threading.Thread(target=lambda: errores.extend(proceso.stderr), daemon=True)
    lector.start()

    # Consumir el progreso en tiempo real desde stdout
    _seguir_progreso(proceso.stdout, progreso, duracion)

    # Esperar la finalización del proceso
    codigo = proceso.wait()
    lector.join()

    archivos = {alto: salida / f"{alto}.mp4" for alto in calidades}

    # Manejo de fallas del proceso FFmpeg
    if codigo != 0:
        _borrar(archivos)
        if codigo == -signal.SIGKILL:
            # Señal de OOM Killer de Linux ante consumo de memoria excesivo
            raise TranscodificacionFallida(
                "ffmpeg se quedó sin memoria y el sistema lo terminó: pedir menos calidades "
                "o un original de menor resolución"
            )
        detalle = "".join(errores).strip() or "sin salida de error"
        raise TranscodificacionFallida(f"ffmpeg terminó con código {codigo}: {detalle}")

    # Validación pos-procesamiento de cada archivo generado
    resultado = {}
    for alto, archivo in archivos.items():
        info = sondear(archivo)
        if info is None:
            _borrar(archivos)
            raise TranscodificacionFallida(f"ffmpeg terminó bien pero {archivo.name} no es un video válido")

        # Comprobar que el video no haya resultado truncado
        if _incompleta(info["duracion_s"], duracion):
            _borrar(archivos)
            raise TranscodificacionFallida(
                f"el original está dañado: se pudieron leer {info['duracion_s']:.1f} s de {duracion:.1f} s"
            )

        resultado[str(alto)] = {
            "archivo": archivo.name,
            "bytes": archivo.stat().st_size,
            "ancho": info["ancho"],
            "alto": info["alto"],
        }

    # Marcación final de progreso completado al 100%
    escribir_progreso(progreso, 100)
    return resultado


def version_ffmpeg() -> str | None:
    """Detecta la versión instalada del binario ffmpeg en el contenedor."""
    try:
        primera = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            check=False
        ).stdout.split("\n", 1)[0]
    except OSError:
        return None
    return primera.removeprefix("ffmpeg version ").split(" ", 1)[0] or None


class Handler(BaseHTTPRequestHandler):
    """Manejador HTTP del microservicio de tarea-video."""
    ffmpeg: str | None = None

    def _responder(self, codigo: int, contenido: dict) -> None:
        """Emite el sobre canónico {"codigo": ..., "contenido": ...} en formato JSON."""
        datos = json.dumps({"codigo": codigo, "contenido": contenido}).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def _error(self, codigo: int, tipo: str, mensaje: str) -> None:
        """Emite respuesta estructurada de error."""
        self._responder(codigo, {"error": {"tipo": tipo, "mensaje": mensaje}})

    def do_GET(self) -> None:
        """Endpoint de salud consultado por el lanzador antes de enviar carga de trabajo."""
        if self.path == "/health":
            self._responder(200, {"tarea": "ok", "ffmpeg": self.ffmpeg})
        else:
            self._error(404, "RUTA_INEXISTENTE", "ruta inexistente")

    def do_POST(self) -> None:
        """Endpoint de ejecución remota de tareas de video."""
        if self.path != "/ejecutarTarea":
            self._error(404, "RUTA_INEXISTENTE", "ruta inexistente")
            return

        try:
            # Leer el payload JSON desde el socket
            longitud = int(self.headers.get("Content-Length", 0))
            pedido = json.loads(self.rfile.read(longitud).decode("utf-8"))

            if pedido.get("calculo") != "transcodificar":
                raise ParametrosInvalidos(f"cálculo desconocido: {pedido.get('calculo')!r}")

            # Ejecutar transcodificación
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
    # Inicialización del microservicio de tarea-video
    Handler.ffmpeg = version_ffmpeg()
    servidor = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    servidor.serve_forever()

