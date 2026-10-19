"""Servicio de gestión y transcodificación asíncrona de video (Hits 2 y 3).

Implementa el ciclo de vida completo de los trabajos de video según contrato-video.md:
- Validación previa estricta con ffprobe (duración, resolución, calidades no superiores al original).
- Recepción por streaming multipart/form-data directamente a disco sin doble buffer en /tmp.
- Control fail-fast de tamaño (Content-Length y conteo de bytes durante streaming).
- Encolamiento en pool de workers dedicado con ordenamiento lógico de Lamport.
- Consulta de estado con progreso atómico y Retry-After.
- Descarga segura de salidas transcodificadas con protección contra Path Traversal.
- Recuperación Fail-Clean al arrancar (marcado de TRABAJO_PERDIDO y purga de huérfanos).
- Retención automática de 24 horas y cuotas por usuario (máximo 3 activos).
"""

from datetime import datetime, timezone, timedelta
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Dict, List, Optional, Set, Tuple
import urllib.error
import urllib.request
from urllib.parse import urlparse

from app.config import Config
from app.errores import ErrorApi, TipoError
from app.imagenes import parsear as parsear_imagen
from app.pool import PoolWorkers

# Alturas estándar permitidas según contrato §2
CALIDADES_VALIDAS: Tuple[int, ...] = (1080, 720, 480, 360, 240)

# Validación estricta de identificadores hexadecimales de 32 caracteres (evita Path Traversal)
RE_ID_TRABAJO = re.compile(r"^[0-9a-f]{32}$")


class RedireccionSeguraHandler(urllib.request.HTTPRedirectHandler):
    """Handler para urllib que intercepta redirecciones (301, 302, 303, 307) y valida cada salto contra SSRF."""

    def __init__(self, validar_fn, max_redirecciones: int = 3, tipo_error=TipoError.URL_INACCESIBLE):
        super().__init__()
        self.validar_fn = validar_fn
        self.max_redirecciones = max_redirecciones
        self.tipo_error = tipo_error
        self.saltos = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.saltos += 1
        if self.saltos > self.max_redirecciones:
            raise ErrorApi(
                self.tipo_error,
                f"demasiadas redirecciones ({self.saltos}) al conectar con {newurl}"
            )
        try:
            self.validar_fn(newurl)
        except ErrorApi as e:
            raise ErrorApi(
                self.tipo_error,
                f"redirección bloqueada hacia dirección no permitida ({newurl}): {e.mensaje}",
                interno=e.interno
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def ahora_iso() -> str:
    """Devuelve la fecha y hora actual en UTC con formato estándar ISO 8601 (terminada en Z)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def calcular_expiracion_iso(fin_iso: Optional[str], horas_retencion: int = 24) -> Optional[str]:
    """Calcula la marca de tiempo de expiración sumando las horas de retención a la fecha de fin."""
    if not fin_iso:
        return None
    try:
        dt = datetime.fromisoformat(fin_iso.replace("Z", "+00:00"))
        expira_dt = dt + timedelta(hours=horas_retencion)
        return expira_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def ffprobe_default(ruta_archivo: str) -> dict:
    """Ejecuta ffprobe sobre el archivo o URL y extrae metadatos en JSON.
    
    Aislamiento de Seguridad:
    - Para URLs remotas se autoriza únicamente 'http,https,tcp,tls', excluyendo estrictamente
      'file' para impedir lecturas arbitrarias de archivos locales vía playlists HLS (CVE-2016-1897).
    - Para archivos locales en disco se restringe a 'file,crypto', bloqueando peticiones de red externas.
    """
    es_url = str(ruta_archivo).startswith("http://") or str(ruta_archivo).startswith("https://")
    whitelist = "http,https,tcp,tls" if es_url else "file,crypto"
    comando = [
        "ffprobe",
        "-v", "error",
        "-protocol_whitelist", whitelist,
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration",
        "-show_entries", "format=duration,size",
        "-of", "json",
        str(ruta_archivo),
    ]
    try:
        res = subprocess.run(comando, capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            raise ErrorApi(
                TipoError.ARCHIVO_NO_ES_VIDEO,
                "el archivo no se puede leer como video: formato inválido o dañado",
                interno=res.stderr.strip() or "ffprobe retornó código distinto de cero"
            )
        return json.loads(res.stdout)
    except subprocess.TimeoutExpired:
        raise ErrorApi(
            TipoError.ARCHIVO_NO_ES_VIDEO,
            "el análisis del video superó el tiempo límite",
            interno="timeout en ffprobe"
        )
    except json.JSONDecodeError as err:
        raise ErrorApi(
            TipoError.ARCHIVO_NO_ES_VIDEO,
            "no se pudieron leer los metadatos del video",
            interno=str(err)
        )
    except FileNotFoundError:
        raise ErrorApi(
            TipoError.ERROR_INTERNO,
            "la herramienta ffprobe no está disponible en el servidor"
        )


class VideoServicio:
    """Administra el almacenamiento, validación, encolamiento y ciclo de vida de videos."""

    def __init__(
        self,
        config: Config,
        pool: PoolWorkers,
        lanzador,
        logger=None,
        ffprobe_runner=None,
        url_opener=None,
    ):
        self.config = config
        self.pool = pool
        self.lanzador = lanzador
        self.log = logger
        self.ffprobe = ffprobe_runner or ffprobe_default
        self.url_opener = url_opener
        self.dir_trabajos = config.video_dir

        # Asegurar existencia del directorio raíz compartido con fallback si no hay permisos de root
        try:
            os.makedirs(self.dir_trabajos, exist_ok=True)
        except PermissionError:
            self.dir_trabajos = os.path.abspath("trabajos")
            os.makedirs(self.dir_trabajos, exist_ok=True)

        # Mutex para concurrencia sobre cuotas activas por usuario
        self._mutex_usuarios = threading.Lock()
        self._trabajos_activos_por_usuario: Dict[str, Set[str]] = {}

        # Hilo de recolección de basura periódica (purga de trabajos vencidos > 24 h)
        self._detener_recolector = threading.Event()
        self._hilo_recolector: Optional[threading.Thread] = None

        # Ejecutar recuperación Fail-Clean al inicializar
        self.recuperar_al_arrancar()

        # Iniciar recolector de basura periódico en segundo plano
        gc_intervalo = getattr(config, "video_gc_intervalo_s", 600.0)
        if gc_intervalo > 0:
            self.iniciar_recolector_basura(gc_intervalo)

    # --------------------------------------------------------------------------
    # Recuperación Fail-Clean y Retención (Contrato §3 y § Anexo)
    # --------------------------------------------------------------------------
    def recuperar_al_arrancar(self) -> None:
        """Examina /trabajos al arrancar y aplica la política Fail-Clean.
        
        1. Las carpetas sin estado.json son subidas incompletas/huérfanas: se borran.
        2. Los trabajos que quedaron 'en_cola' o 'procesando' se marcan como error TRABAJO_PERDIDO.
           En el Hit 3, cada nodo solo marca como perdidos los suyos (§ Anexo).
        3. Los trabajos terminados hace más de 24 horas se borran.
        """
        if not os.path.isdir(self.dir_trabajos):
            return

        ahora_ts = time.time()
        retencion_s = self.config.video_retencion_horas * 3600

        with self._mutex_usuarios:
            self._trabajos_activos_por_usuario.clear()

            try:
                entradas = os.scandir(self.dir_trabajos)
            except OSError as err:
                if self.log:
                    self.log.error("video | error al escanear directorio %s: %s", self.dir_trabajos, err)
                return

            for entrada in entradas:
                if not entrada.is_dir():
                    continue

                id_trabajo = entrada.name
                if not RE_ID_TRABAJO.fullmatch(id_trabajo):
                    continue

                ruta_estado = os.path.join(entrada.path, "estado.json")
                if not os.path.exists(ruta_estado):
                    # Huérfano sin estado (subida abortada): borrar
                    try:
                        shutil.rmtree(entrada.path)
                        if self.log:
                            self.log.info("video | purgado directorio huérfano sin estado: %s", id_trabajo)
                    except OSError as err:
                        if self.log:
                            self.log.warning("video | no se pudo borrar huérfano %s: %s", id_trabajo, err)
                    continue

                # Leer estado existente
                try:
                    with open(ruta_estado, "r", encoding="utf-8") as f:
                        estado = json.load(f)
                except Exception as err:
                    if self.log:
                        self.log.warning("video | error leyendo estado.json en %s: %s", id_trabajo, err)
                    continue

                estado_actual = estado.get("estado")
                tiempos = estado.get("tiempos", {})
                fin_iso = tiempos.get("fin")

                # Verificar si ya venció el período de retención
                if estado_actual in ("listo", "error") and fin_iso:
                    try:
                        dt = datetime.fromisoformat(fin_iso.replace("Z", "+00:00"))
                        if ahora_ts - dt.timestamp() > retencion_s:
                            shutil.rmtree(entrada.path)
                            if self.log:
                                self.log.info("video | trabajo %s vencido por retención purgado", id_trabajo)
                            continue
                    except Exception:
                        pass

                # Fail-Clean: Si el servidor cayó mientras procesaba o en cola, pasar a TRABAJO_PERDIDO.
                # En el Hit 3, cada nodo solo marca como perdidos los suyos (§ Anexo).
                if estado_actual in ("en_cola", "procesando"):
                    nodo_trabajo = estado.get("nodo")
                    nodo_actual = getattr(self.config, "nodo_id", None)
                    if nodo_actual is not None and nodo_trabajo is not None and str(nodo_trabajo) != str(nodo_actual):
                        continue  # Pertenece a otro nodo del clúster que puede seguir vivo

                    estado["estado"] = "error"
                    estado["error"] = {
                        "tipo": "TRABAJO_PERDIDO",
                        "mensaje": "el servidor se reinició mientras el trabajo estaba en curso"
                    }
                    if not tiempos.get("fin"):
                        tiempos["fin"] = ahora_iso()
                    estado["tiempos"] = tiempos
                    self._escribir_json_atomico(ruta_estado, estado)
                    if self.log:
                        self.log.warning("video | trabajo %s recuperado como TRABAJO_PERDIDO", id_trabajo)

    def purgar_vencidos(self) -> int:
        """Purga trabajos finalizados que superaron el tiempo de retención (24 h) y huérfanos.
        
        Retorna la cantidad de directorios purgados.
        """
        if not os.path.isdir(self.dir_trabajos):
            return 0
        ahora_ts = time.time()
        retencion_s = self.config.video_retencion_horas * 3600
        purgados = 0
        try:
            entradas = list(os.scandir(self.dir_trabajos))
        except OSError:
            return 0

        for entrada in entradas:
            if not entrada.is_dir() or not RE_ID_TRABAJO.fullmatch(entrada.name):
                continue
            ruta_estado = os.path.join(entrada.path, "estado.json")
            if not os.path.exists(ruta_estado):
                try:
                    # Huérfano sin estado.json con más de 30 minutos de antigüedad
                    if ahora_ts - entrada.stat().st_mtime > 1800:
                        shutil.rmtree(entrada.path, ignore_errors=True)
                        purgados += 1
                except OSError:
                    pass
                continue

            try:
                with open(ruta_estado, "r", encoding="utf-8") as f:
                    estado = json.load(f)
                estado_actual = estado.get("estado")
                fin_iso = estado.get("tiempos", {}).get("fin")
                if estado_actual in ("listo", "error") and fin_iso:
                    dt = datetime.fromisoformat(fin_iso.replace("Z", "+00:00"))
                    if ahora_ts - dt.timestamp() > retencion_s:
                        shutil.rmtree(entrada.path, ignore_errors=True)
                        purgados += 1
                        if self.log:
                            self.log.info("video | recolector de basura: purgado trabajo vencido %s", entrada.name)
            except Exception:
                continue
        return purgados

    def iniciar_recolector_basura(self, intervalo_s: float = 600.0) -> None:
        """Inicia el hilo demonio de recolección de basura cada intervalo_s segundos (por defecto 10 min)."""
        if self._hilo_recolector is not None and self._hilo_recolector.is_alive():
            return

        def _bucle_limpieza():
            while not self._detener_recolector.is_set():
                if self._detener_recolector.wait(intervalo_s):
                    break
                try:
                    self.purgar_vencidos()
                except Exception as err:
                    if self.log:
                        self.log.warning("video | error en recolector de basura: %s", err)

        self._detener_recolector.clear()
        self._hilo_recolector = threading.Thread(target=_bucle_limpieza, name="video-garbage-collector", daemon=True)
        self._hilo_recolector.start()

    def detener_recolector_basura(self) -> None:
        """Detiene el hilo demonio de recolección de basura."""
        self._detener_recolector.set()
        if self._hilo_recolector and self._hilo_recolector.is_alive():
            self._hilo_recolector.join(timeout=2.0)

    # --------------------------------------------------------------------------
    # Validaciones Previas (Contrato §2, §3 y §6)
    # --------------------------------------------------------------------------
    def validar_calidades(self, calidades_raw: any) -> List[int]:
        """Valida la lista o string de calidades solicitadas sin coerción implícita."""
        if isinstance(calidades_raw, str):
            partes = [c.strip() for c in calidades_raw.split(",") if c.strip()]
        elif isinstance(calidades_raw, list):
            partes = calidades_raw
        else:
            raise ErrorApi(TipoError.PAYLOAD_INVALIDO, "el campo calidades es obligatorio", detalles=["calidades"])

        if not partes:
            raise ErrorApi(TipoError.PAYLOAD_INVALIDO, "el campo calidades no puede estar vacío", detalles=["calidades"])

        calidades: List[int] = []
        for p in partes:
            # Rechazar estrictamente booleanos, floats y tipos no enteros
            if isinstance(p, bool) or not isinstance(p, (int, str)):
                raise ErrorApi(
                    TipoError.PAYLOAD_INVALIDO,
                    f"calidad inválida: {p}",
                    detalles=["calidades"]
                )
            if isinstance(p, str) and not p.isdigit():
                raise ErrorApi(
                    TipoError.PAYLOAD_INVALIDO,
                    f"calidad inválida: {p}",
                    detalles=["calidades"]
                )
            try:
                c_num = int(p)
            except (ValueError, TypeError):
                raise ErrorApi(
                    TipoError.PAYLOAD_INVALIDO,
                    f"calidad inválida: {p}",
                    detalles=["calidades"]
                )
            if c_num not in CALIDADES_VALIDAS:
                raise ErrorApi(
                    TipoError.PAYLOAD_INVALIDO,
                    f"calidad {c_num} no soportada; soportadas: {list(CALIDADES_VALIDAS)}",
                    detalles=["calidades"]
                )
            if c_num in calidades:
                raise ErrorApi(
                    TipoError.PAYLOAD_INVALIDO,
                    f"calidades contiene valores repetidos ({c_num})",
                    detalles=["calidades"]
                )
            calidades.append(c_num)

        return calidades

    def validar_url(self, url: str) -> None:
        """Valida que la URL sea http/https y no apunte a redes privadas (SSRF)."""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ErrorApi(TipoError.URL_INVALIDA, f"esquema no soportado: {parsed.scheme} (se requiere http o https)")

        hostname = parsed.hostname
        if not hostname:
            raise ErrorApi(TipoError.URL_INVALIDA, "la URL no contiene un nombre de host válido")

        if hostname.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "::"):
            raise ErrorApi(TipoError.URL_INVALIDA, "no se permiten URLs apuntando a localhost o direcciones no enrutables")

        try:
            # Resolución DNS para verificar rangos privados
            ips = socket.getaddrinfo(hostname, None)
            for info in ips:
                ip_str = info[4][0]
                ip_obj = ipaddress.ip_address(ip_str)
                if (
                    ip_obj.is_private
                    or ip_obj.is_loopback
                    or ip_obj.is_link_local
                    or ip_obj.is_reserved
                    or ip_obj.is_unspecified
                ):
                    raise ErrorApi(
                        TipoError.URL_INVALIDA,
                        f"la URL apunta a una dirección IP no permitida ({ip_str})"
                    )
        except socket.gaierror:
            raise ErrorApi(TipoError.URL_INACCESIBLE, f"no se pudo resolver el host {hostname}")

    def validar_metadatos_video(self, ruta_archivo: str, calidades: List[int]) -> dict:
        """Ejecuta ffprobe y valida duración, resolución y que las calidades no superen el original."""
        info = self.ffprobe(ruta_archivo)
        streams = info.get("streams", [])
        if not streams:
            raise ErrorApi(TipoError.ARCHIVO_NO_ES_VIDEO, "el archivo no contiene ninguna pista de video")

        stream_v = streams[0]
        ancho = stream_v.get("width")
        alto = stream_v.get("height")

        if not ancho or not alto:
            raise ErrorApi(TipoError.ARCHIVO_NO_ES_VIDEO, "no se pudo determinar la resolución de la pista de video")

        # Límite 4K (3840×2160)
        if ancho > 3840 or alto > 2160:
            raise ErrorApi(
                TipoError.RESOLUCION_NO_SOPORTADA,
                f"la resolución de entrada ({ancho}x{alto}) supera el máximo soportado de 3840x2160 (4K)"
            )

        # Duración en segundos
        dur_str = stream_v.get("duration") or info.get("format", {}).get("duration")
        if not dur_str:
            raise ErrorApi(TipoError.ARCHIVO_NO_ES_VIDEO, "no se pudo determinar la duración del video")

        try:
            duracion_s = float(dur_str)
        except (ValueError, TypeError):
            raise ErrorApi(TipoError.ARCHIVO_NO_ES_VIDEO, f"duración de video inválida: {dur_str}")

        if duracion_s <= 0:
            raise ErrorApi(
                TipoError.ARCHIVO_NO_ES_VIDEO,
                f"la duración del video ({duracion_s} s) debe ser mayor a 0"
            )

        if duracion_s > self.config.video_duracion_max_s:
            raise ErrorApi(
                TipoError.VIDEO_DEMASIADO_LARGO,
                f"la duración ({duracion_s:.1f} s) supera el límite de {self.config.video_duracion_max_s:.0f} s (15 min)"
            )

        # Validación: nunca se agranda (§2)
        for c in calidades:
            if c > alto:
                raise ErrorApi(
                    TipoError.CALIDAD_SUPERIOR_AL_ORIGINAL,
                    f"la calidad {c}p solicitada supera la altura del video original ({alto}p)"
                )

        return {
            "ancho": int(ancho),
            "alto": int(alto),
            "duracion_s": round(duracion_s, 2),
        }

    @staticmethod
    def sanitizar_nombre_archivo(nombre: str) -> str:
        """Sanitiza el nombre de archivo eliminando path traversal, CRLF, comillas y caracteres no seguros."""
        import posixpath
        base = os.path.basename(str(nombre)).strip()
        base = posixpath.basename(base)
        # Eliminar caracteres de control y saltos de línea (CRLF) para evitar inyecciones de cabecera HTTP
        base = re.sub(r'[\r\n\t\x00-\x1f\x7f]', '', base)
        # Eliminar comillas dobles, simples, backslashes, punto y coma y barras
        base = re.sub(r'["\';\\/]', '', base)
        # Eliminar caracteres fuera de ASCII imprimible seguro
        base = re.sub(r'[^\w\s.-]', '_', base)
        # Normalizar espacios
        base = re.sub(r'\s+', '_', base).strip('._ ')
        if not base:
            return "video.mp4"
        if len(base) > 100:
            nombre_p, ext = os.path.splitext(base)
            base = f"{nombre_p[:90]}{ext}"
        return base

    def _verificar_redirecciones_url(self, url: str) -> None:
        """Verifica de forma segura que la URL responda y que ninguna redirección apunte a IPs privadas/loopback (SSRF)."""
        if self.url_opener is not None:
            opener = self.url_opener
        else:
            opener = urllib.request.build_opener(
                RedireccionSeguraHandler(
                    self.validar_url,
                    max_redirecciones=3,
                    tipo_error=TipoError.URL_INACCESIBLE,
                )
            )

        req = urllib.request.Request(
            url,
            headers={"User-Agent": "CerberusVideo/1.0", "Range": "bytes=0-1024"},
            method="HEAD",
        )
        try:
            with opener.open(req, timeout=10) as resp:
                status = getattr(resp, "status", getattr(resp, "code", 200))
                if status >= 400:
                    raise ErrorApi(
                        TipoError.URL_INACCESIBLE,
                        f"la URL {url} devolvió un error HTTP {status}"
                    )
        except urllib.error.HTTPError as err:
            # Si el servidor remoto rechaza HEAD (405) o Range (416), reintentamos con GET
            if err.code in (405, 416, 501):
                try:
                    req_get = urllib.request.Request(
                        url,
                        headers={"User-Agent": "CerberusVideo/1.0", "Range": "bytes=0-1024"}
                    )
                    with opener.open(req_get, timeout=10) as resp_get:
                        status = getattr(resp_get, "status", getattr(resp_get, "code", 200))
                        if status >= 400:
                            raise ErrorApi(
                                TipoError.URL_INACCESIBLE,
                                f"la URL {url} devolvió un error HTTP {status}"
                            )
                except ErrorApi:
                    raise
                except Exception as ex_get:
                    raise ErrorApi(
                        TipoError.URL_INACCESIBLE,
                        f"la URL {url} no responde o devolvió un error: {ex_get}",
                        interno=str(ex_get),
                    )
            else:
                raise ErrorApi(
                    TipoError.URL_INACCESIBLE,
                    f"la URL {url} devolvió un error HTTP {err.code}",
                    interno=str(err),
                )
        except ErrorApi:
            raise
        except Exception as err:
            raise ErrorApi(
                TipoError.URL_INACCESIBLE,
                f"la URL {url} no responde o no es accesible: {err}",
                interno=str(err),
            )

    def validar_url_y_metadatos(self, url: str, calidades: List[int]) -> dict:
        """Valida que la URL sea pública, responda y contenga un video compatible (contrato §4)."""
        self.validar_url(url)
        self._verificar_redirecciones_url(url)
        try:
            info = self.ffprobe(url)
        except ErrorApi as err:
            if err.tipo == TipoError.ARCHIVO_NO_ES_VIDEO:
                raise ErrorApi(
                    TipoError.URL_INACCESIBLE,
                    f"la URL {url} no responde, devolvió un error o no hay un video accesible",
                    interno=err.interno,
                )
            raise
        except Exception as err:
            raise ErrorApi(
                TipoError.URL_INACCESIBLE,
                f"la URL {url} no responde o no hay un video accesible: {err}",
                interno=str(err),
            )

        streams = info.get("streams", [])
        if not streams:
            raise ErrorApi(
                TipoError.URL_INACCESIBLE,
                f"la URL {url} no contiene ninguna pista de video accesible"
            )

        stream_v = streams[0]
        ancho = stream_v.get("width")
        alto = stream_v.get("height")
        if not ancho or not alto:
            raise ErrorApi(
                TipoError.URL_INACCESIBLE,
                "no se pudo determinar la resolución de la pista de video en la URL"
            )

        if ancho > 3840 or alto > 2160:
            raise ErrorApi(
                TipoError.RESOLUCION_NO_SOPORTADA,
                f"la resolución de entrada ({ancho}x{alto}) supera el máximo soportado de 3840x2160 (4K)"
            )

        dur_str = stream_v.get("duration") or info.get("format", {}).get("duration")
        if not dur_str:
            raise ErrorApi(
                TipoError.URL_INACCESIBLE,
                "no se pudo determinar la duración del video en la URL"
            )

        try:
            duracion_s = float(dur_str)
        except (ValueError, TypeError):
            raise ErrorApi(TipoError.URL_INACCESIBLE, f"duración de video inválida en la URL: {dur_str}")

        if duracion_s <= 0:
            raise ErrorApi(
                TipoError.URL_INACCESIBLE,
                f"la duración del video en la URL ({duracion_s} s) debe ser mayor a 0"
            )

        if duracion_s > self.config.video_duracion_max_s:
            raise ErrorApi(
                TipoError.VIDEO_DEMASIADO_LARGO,
                f"la duración ({duracion_s:.1f} s) supera el límite de {self.config.video_duracion_max_s:.0f} s (15 min)"
            )

        for c in calidades:
            if c > alto:
                raise ErrorApi(
                    TipoError.CALIDAD_SUPERIOR_AL_ORIGINAL,
                    f"la calidad {c}p solicitada supera la altura del video original ({alto}p)"
                )

        bytes_val = 0
        size_str = info.get("format", {}).get("size")
        if size_str:
            try:
                bytes_val = int(size_str)
                if bytes_val > self.config.video_tamano_max_bytes:
                    raise ErrorApi(
                        TipoError.VIDEO_DEMASIADO_GRANDE,
                        f"el archivo en la URL supera el límite de {self.config.video_tamano_max_bytes} bytes"
                    )
            except ValueError:
                pass

        import posixpath
        from urllib.parse import urlparse
        nombre_raw = posixpath.basename(urlparse(url).path) or "video.mp4"
        nombre_archivo = self.sanitizar_nombre_archivo(nombre_raw)

        return {
            "nombre": nombre_archivo,
            "bytes": bytes_val,
            "ancho": int(ancho),
            "alto": int(alto),
            "duracion_s": round(duracion_s, 2),
        }

    # --------------------------------------------------------------------------
    # Control de Cuotas por Usuario (Atómico y anti-carreras TOCTOU)
    # --------------------------------------------------------------------------
    def reservar_cuota_usuario(self, usuario: str, id_trabajo: str) -> None:
        """Reserva atómicamente un slot de cuota antes de cualquier I/O o procesamiento.
        
        Garantiza que no existan carreras TOCTOU entre validación y registro bajo contención.
        """
        with self._mutex_usuarios:
            activos = self._trabajos_activos_por_usuario.get(usuario, set())
            if id_trabajo in activos:
                return
            if len(activos) >= self.config.video_trabajos_por_usuario_max:
                raise ErrorApi(
                    TipoError.DEMASIADOS_TRABAJOS,
                    f"el usuario {usuario} ya tiene {len(activos)} trabajos activos "
                    f"(máximo {self.config.video_trabajos_por_usuario_max})"
                )
            if usuario not in self._trabajos_activos_por_usuario:
                self._trabajos_activos_por_usuario[usuario] = set()
            self._trabajos_activos_por_usuario[usuario].add(id_trabajo)

    def liberar_cuota_usuario(self, usuario: str, id_trabajo: str) -> None:
        """Libera la reserva de cuota de un trabajo."""
        with self._mutex_usuarios:
            if usuario in self._trabajos_activos_por_usuario:
                self._trabajos_activos_por_usuario[usuario].discard(id_trabajo)
                if not self._trabajos_activos_por_usuario[usuario]:
                    del self._trabajos_activos_por_usuario[usuario]

    def trabajos_activos_de(self, usuario: str) -> int:
        """Devuelve la cantidad de trabajos activos de un usuario."""
        with self._mutex_usuarios:
            return len(self._trabajos_activos_por_usuario.get(usuario, set()))

    def _verificar_cuota_usuario(self, usuario: str) -> None:
        """Verifica que el usuario no supere el límite de trabajos activos (3)."""
        with self._mutex_usuarios:
            activos = self._trabajos_activos_por_usuario.get(usuario, set())
            if len(activos) >= self.config.video_trabajos_por_usuario_max:
                raise ErrorApi(
                    TipoError.DEMASIADOS_TRABAJOS,
                    f"el usuario {usuario} ya tiene {len(activos)} trabajos activos "
                    f"(máximo {self.config.video_trabajos_por_usuario_max})"
                )

    def _registrar_trabajo_activo(self, usuario: str, id_trabajo: str) -> None:
        self.reservar_cuota_usuario(usuario, id_trabajo)

    def _liberar_trabajo_activo(self, usuario: str, id_trabajo: str) -> None:
        self.liberar_cuota_usuario(usuario, id_trabajo)

    # --------------------------------------------------------------------------
    # Recepción por Streaming Multipart
    # --------------------------------------------------------------------------
    async def recibir_video_multipart(self, request, dir_trabajo: str) -> Tuple[str, int, str]:
        """Transmite el cuerpo multipart directamente a dir_trabajo/entrada evitando doble buffer.
        
        Aplica control fail-fast de tamaño y validación estricta de nombres de campos.
        Retorna:
          Tupla: (nombre_archivo_original, bytes_escritos, calidades_str)
        """
        from python_multipart import MultipartParser
        from python_multipart.multipart import parse_options_header
        from starlette.concurrency import run_in_threadpool

        content_type = request.headers.get("content-type", "")
        _, params = parse_options_header(content_type)
        boundary = params.get(b"boundary") or params.get("boundary")
        if not boundary:
            raise ErrorApi(TipoError.PAYLOAD_INVALIDO, "el tipo de contenido multipart no contiene boundary")
        if isinstance(boundary, str):
            boundary = boundary.encode("latin1")

        # Control fail-fast mediante Content-Length antes de leer el cuerpo.
        # Se agrega un margen para el envelope (boundary, cabeceras) para no
        # rechazar falsamente videos que pesan <= 1 GiB pero cuyo sobre multipart excede por poco.
        MARGEN_ENVELOPE_MULTIPART = 65536
        cl = request.headers.get("content-length")
        if cl:
            try:
                cl_val = int(cl)
                if cl_val > self.config.video_tamano_max_bytes + MARGEN_ENVELOPE_MULTIPART:
                    raise ErrorApi(
                        TipoError.VIDEO_DEMASIADO_GRANDE,
                        f"el tamaño del cuerpo ({cl_val} bytes) supera ampliamente el límite del video ({self.config.video_tamano_max_bytes} bytes)"
                    )
            except ValueError:
                pass

        os.makedirs(dir_trabajo, exist_ok=True)
        ruta_entrada = os.path.join(dir_trabajo, "entrada")

        file_out = None
        bytes_video = 0
        campos = {}
        nombre_archivo = "video.mp4"
        campos_inesperados = set()
        campos_permitidos = {"video", "calidades"}
        current_headers = {}
        current_field = None
        video_presente = False

        def on_header_field(data, start, end):
            key = data[start:end].decode("latin1").lower()
            last = current_headers.get("_last_field")
            if current_headers.get("_in_value", False):
                current_headers["_last_field"] = key
                current_headers["_in_value"] = False
                current_headers[key] = b""
            else:
                if last:
                    key = last + key
                current_headers["_last_field"] = key
                current_headers[key] = b""

        def on_header_value(data, start, end):
            current_headers["_in_value"] = True
            key = current_headers.get("_last_field")
            if key:
                current_headers[key] = current_headers.get(key, b"") + data[start:end]

        def on_headers_finished():
            nonlocal current_field, nombre_archivo, file_out, video_presente
            cd = current_headers.get("content-disposition", b"").decode("latin1")
            _, cd_params = parse_options_header(cd)
            name_raw = cd_params.get(b"name") or cd_params.get("name", b"")
            current_field = name_raw.decode("utf-8", errors="ignore") if isinstance(name_raw, bytes) else str(name_raw)

            if current_field not in campos_permitidos:
                campos_inesperados.add(current_field)

            fn_raw = cd_params.get(b"filename") or cd_params.get("filename")
            if fn_raw:
                fn_str = fn_raw.decode("utf-8", errors="ignore") if isinstance(fn_raw, bytes) else str(fn_raw)
                if fn_str:
                    nombre_archivo = self.sanitizar_nombre_archivo(fn_str)

            if current_field == "video":
                video_presente = True
                file_out = open(ruta_entrada, "wb")

        def on_part_data(data, start, end):
            nonlocal bytes_video
            chunk = data[start:end]
            if current_field == "video" and file_out:
                bytes_video += len(chunk)
                if bytes_video > self.config.video_tamano_max_bytes:
                    raise ErrorApi(
                        TipoError.VIDEO_DEMASIADO_GRANDE,
                        f"el archivo supera el límite de {self.config.video_tamano_max_bytes} bytes"
                    )
                file_out.write(chunk)
            elif current_field:
                campos[current_field] = campos.get(current_field, b"") + chunk

        def on_part_end():
            nonlocal file_out, current_field
            if file_out:
                file_out.close()
                file_out = None
            current_headers.clear()
            current_field = None

        parser = MultipartParser(boundary, {
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
        })

        try:
            # Procesar la escritura de chunks en threadpool para liberar el event loop de FastAPI
            async for chunk in request.stream():
                await run_in_threadpool(parser.write, chunk)
            await run_in_threadpool(parser.finalize)
        except ErrorApi:
            raise
        except Exception as err:
            raise ErrorApi(
                TipoError.PAYLOAD_INVALIDO,
                "error al procesar la transmisión multipart del video",
                interno=str(err)
            )
        finally:
            if file_out:
                try:
                    await run_in_threadpool(file_out.close)
                except Exception:
                    file_out.close()

        if campos_inesperados:
            raise ErrorApi(
                TipoError.PAYLOAD_INVALIDO,
                f"campo no permitido en el formulario: {list(campos_inesperados)[0]}",
                detalles=list(campos_inesperados)
            )

        faltantes = []
        if "calidades" not in campos or not campos["calidades"].strip():
            faltantes.append("calidades")
        if not video_presente or not os.path.exists(ruta_entrada):
            faltantes.append("video")
        if faltantes:
            raise ErrorApi(
                TipoError.PAYLOAD_INVALIDO,
                f"faltan campos obligatorios en el formulario: {', '.join(faltantes)}",
                detalles=faltantes,
            )

        if bytes_video == 0:
            raise ErrorApi(TipoError.ARCHIVO_NO_ES_VIDEO, "el archivo de video está vacío (0 bytes)")

        calidades_str = campos["calidades"].decode("utf-8", errors="ignore").strip()
        return nombre_archivo, bytes_video, calidades_str

    # --------------------------------------------------------------------------
    # Creación y Despacho del Trabajo (POST /videos)
    # --------------------------------------------------------------------------
    def crear_trabajo(
        self,
        usuario: str,
        nombre_archivo: str,
        bytes_totales: int,
        meta_video: dict,
        calidades: List[int],
        ts_solicitud: int,
        id_trabajo: Optional[str] = None,
        url: Optional[str] = None,
    ) -> dict:
        """Crea el trabajo en disco, inicializa estado.json y lo encola en el pool."""
        import uuid
        if id_trabajo is None:
            id_trabajo = uuid.uuid4().hex

        # Control y reserva atómica de cuota (idempotente si ya fue reservada en el endpoint)
        self.reservar_cuota_usuario(usuario, id_trabajo)

        dir_trabajo = os.path.join(self.dir_trabajos, id_trabajo)
        os.makedirs(dir_trabajo, exist_ok=True)
        os.makedirs(os.path.join(dir_trabajo, "salida"), exist_ok=True)

        estado_inicial = {
            "id": id_trabajo,
            "usuario": usuario,
            "nodo": getattr(self.config, "nodo_id", None),
            "estado": "en_cola",
            "calidades": calidades,
            "original": {
                "nombre": nombre_archivo,
                "bytes": bytes_totales,
                "duracion_s": meta_video["duracion_s"],
                "ancho": meta_video["ancho"],
                "alto": meta_video["alto"],
            },
            "tiempos": {
                "creado": ahora_iso(),
                "inicio": None,
                "fin": None,
                "en_cola_s": None,
                "procesamiento_s": None,
            },
            "resultados": None,
            "error": None,
        }
        if url:
            estado_inicial["url"] = url

        ruta_estado = os.path.join(dir_trabajo, "estado.json")
        try:
            self._escribir_json_atomico(ruta_estado, estado_inicial)
        except Exception:
            self.liberar_cuota_usuario(usuario, id_trabajo)
            try:
                shutil.rmtree(dir_trabajo)
            except OSError:
                pass
            raise

        # Definir callbacks de ciclo de vida para el worker pool
        def al_iniciar(id_t: str, en_cola_s: float):
            self._actualizar_estado_iniciar(id_t, en_cola_s)

        def al_terminar(id_t: str, resultado: any, error: any, en_cola_s: float):
            self._actualizar_estado_terminar(id_t, usuario, resultado, error)

        # Función ejecutora que invoca al contenedor tarea-video
        def ejecutor():
            return self._ejecutar_transcodificacion(
                id_trabajo=id_trabajo,
                calidades=calidades,
                duracion_s=meta_video["duracion_s"],
                url=url,
            )

        # Encolar en el pool de workers (no bloqueante)
        try:
            self.pool.encolar(
                id_tarea=id_trabajo,
                ejecutor_fn=ejecutor,
                lamport_ts=ts_solicitud,
                al_iniciar=al_iniciar,
                al_terminar=al_terminar,
            )
        except Exception:
            self.liberar_cuota_usuario(usuario, id_trabajo)
            try:
                shutil.rmtree(dir_trabajo)
            except OSError:
                pass
            raise

        posicion = self.pool.posicion_en_cola(id_trabajo) if hasattr(self.pool, "posicion_en_cola") else self.pool.tareas_encoladas
        return {
            "id": id_trabajo,
            "estado": "en_cola",
            "posicion_en_cola": max(1, posicion),
            "calidades": calidades,
            "original": estado_inicial["original"],
            "consultar": f"/videos/{id_trabajo}",
        }

    def _descargar_video_url(self, url: str, ruta_destino: str) -> None:
        """Descarga el video desde una URL pública a disco con verificación de límites y mitigación anti-SSRF."""
        # Revalidar la URL inmediatamente antes de iniciar la conexión (mitigación contra DNS rebinding)
        self.validar_url(url)

        if self.url_opener is not None:
            opener = self.url_opener
        else:
            opener = urllib.request.build_opener(
                RedireccionSeguraHandler(
                    self.validar_url,
                    max_redirecciones=3,
                    tipo_error=TipoError.DESCARGA_FALLIDA,
                )
            )

        try:
            import time
            req = urllib.request.Request(url, headers={"User-Agent": "CerberusVideo/1.0"})
            bytes_descargados = 0
            inicio_descarga = time.time()
            max_segundos_descarga = 300.0
            with opener.open(req, timeout=30) as resp:
                status = getattr(resp, "status", getattr(resp, "code", 200))
                if status != 200:
                    raise ErrorApi(
                        TipoError.DESCARGA_FALLIDA,
                        f"error HTTP {status} al descargar el video desde {url}"
                    )
                with open(ruta_destino, "wb") as f_out:
                    while True:
                        if time.time() - inicio_descarga > max_segundos_descarga:
                            raise ErrorApi(
                                TipoError.DESCARGA_FALLIDA,
                                f"la descarga excedió el tiempo límite de {int(max_segundos_descarga)} segundos"
                            )
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        bytes_descargados += len(chunk)
                        if bytes_descargados > self.config.video_tamano_max_bytes:
                            raise ErrorApi(
                                TipoError.VIDEO_DEMASIADO_GRANDE,
                                f"el archivo descargado supera el límite de {self.config.video_tamano_max_bytes} bytes"
                            )
                        f_out.write(chunk)
        except ErrorApi:
            if os.path.exists(ruta_destino):
                try:
                    os.remove(ruta_destino)
                except OSError:
                    pass
            raise
        except Exception as err:
            if os.path.exists(ruta_destino):
                try:
                    os.remove(ruta_destino)
                except OSError:
                    pass
            raise ErrorApi(
                TipoError.DESCARGA_FALLIDA,
                f"falló la descarga del video desde la URL: {err}",
                interno=str(err)
            )

    def _ejecutar_transcodificacion(
        self,
        id_trabajo: str,
        calidades: List[int],
        duracion_s: float,
        url: Optional[str] = None,
    ):
        """Ejecuta la tarea en el contenedor efímero tarea-video."""
        dir_trabajo = os.path.join(self.dir_trabajos, id_trabajo)
        ruta_entrada_local = os.path.join(dir_trabajo, "entrada")

        # Si el trabajo proviene de una URL y aún no se descargó a entrada, descargar ahora
        if url and not os.path.exists(ruta_entrada_local):
            self._descargar_video_url(url, ruta_entrada_local)

        imagen = parsear_imagen(self.config.video_imagen)
        calculo = "transcodificar"
        parametros = {"calidades": calidades}
        # En contenedor Docker se monta en /trabajos; en tests o ejecución local se usa self.dir_trabajos
        base_dir = "/trabajos" if os.path.exists("/trabajos") and os.access("/trabajos", os.W_OK) else self.dir_trabajos
        datos = {
            "entrada": os.path.join(base_dir, id_trabajo, "entrada"),
            "salida": os.path.join(base_dir, id_trabajo, "salida"),
            "progreso": os.path.join(base_dir, id_trabajo, "progreso.json"),
            "duracion_s": duracion_s,
        }

        # Configuración del volumen compartido (en host o volumen Docker)
        volumen_montaje = getattr(self.config, "video_volumen", "tp2-videos")
        volumenes = {volumen_montaje: {"bind": "/trabajos", "mode": "rw"}}

        return self.lanzador.ejecutarTareaRemota(
            imagen=imagen,
            calculo=calculo,
            parametros=parametros,
            datos=datos,
            volumenes=volumenes,
            mem_limit=self.config.video_mem_limit,
            cpus=self.config.video_cpus,
            timeout=self.config.video_timeout,
        )

    def _actualizar_estado_iniciar(self, id_trabajo: str, en_cola_s: float) -> None:
        ruta_estado = os.path.join(self.dir_trabajos, id_trabajo, "estado.json")
        try:
            with open(ruta_estado, "r", encoding="utf-8") as f:
                estado = json.load(f)
            estado["estado"] = "procesando"
            estado["tiempos"]["inicio"] = ahora_iso()
            estado["tiempos"]["en_cola_s"] = round(en_cola_s, 2)
            self._escribir_json_atomico(ruta_estado, estado)
        except Exception as err:
            if self.log:
                self.log.error("video | error al actualizar inicio en %s: %s", id_trabajo, err)

    def _actualizar_estado_terminar(self, id_trabajo: str, usuario: str, resultado: any, error: any) -> None:
        ruta_estado = os.path.join(self.dir_trabajos, id_trabajo, "estado.json")
        try:
            with open(ruta_estado, "r", encoding="utf-8") as f:
                estado = json.load(f)

            fin_ts = ahora_iso()
            estado["tiempos"]["fin"] = fin_ts

            # Calcular procesamiento_s si tenemos inicio
            inicio_iso = estado["tiempos"].get("inicio")
            if inicio_iso:
                try:
                    dt_ini = datetime.fromisoformat(inicio_iso.replace("Z", "+00:00"))
                    dt_fin = datetime.fromisoformat(fin_ts.replace("Z", "+00:00"))
                    estado["tiempos"]["procesamiento_s"] = round(dt_fin.timestamp() - dt_ini.timestamp(), 2)
                except Exception:
                    pass

            if error:
                estado["estado"] = "error"
                if isinstance(error, ErrorApi) and error.tipo == TipoError.TAREA_SIN_RESPUESTA:
                    estado["error"] = {
                        "tipo": "TIEMPO_AGOTADO",
                        "mensaje": "el procesamiento superó el tiempo máximo permitido",
                    }
                elif isinstance(error, ErrorApi) and error.tipo == TipoError.DESCARGA_FALLIDA:
                    estado["error"] = {
                        "tipo": "DESCARGA_FALLIDA",
                        "mensaje": error.mensaje,
                    }
                else:
                    estado["error"] = {
                        "tipo": "TRANSCODIFICACION_FALLIDA",
                        "mensaje": getattr(error, "mensaje", str(error)),
                    }
            else:
                estado["estado"] = "listo"
                # Formatear salidas de resultados
                res_dict = {}
                datos_res = resultado.resultado if hasattr(resultado, "resultado") else resultado
                if isinstance(datos_res, dict):
                    for cal_k, datos_cal in datos_res.items():
                        res_dict[str(cal_k)] = {
                            "ancho": datos_cal.get("ancho"),
                            "alto": datos_cal.get("alto"),
                            "bytes": datos_cal.get("bytes"),
                            "descargar": f"/videos/{id_trabajo}/{cal_k}",
                        }
                estado["resultados"] = res_dict

                # Liberar archivo original de entrada una vez transcodificado exitosamente
                ruta_entrada_eliminar = os.path.join(self.dir_trabajos, id_trabajo, "entrada")
                if os.path.exists(ruta_entrada_eliminar):
                    try:
                        os.remove(ruta_entrada_eliminar)
                    except OSError:
                        pass

            self._escribir_json_atomico(ruta_estado, estado)
        except Exception as err:
            if self.log:
                self.log.error("video | error al actualizar fin en %s: %s", id_trabajo, err)
        finally:
            self._liberar_trabajo_activo(usuario, id_trabajo)

    # --------------------------------------------------------------------------
    # Consulta de Estado (GET /videos/{id})
    # --------------------------------------------------------------------------
    def consultar_trabajo(self, id_trabajo: str, usuario_solicitante: str, es_admin: bool) -> Tuple[dict, Optional[str]]:
        """Consulta el estado del trabajo, verificando propiedad y expiración.
        
        Retorna:
          Tupla (contenido_diccionario, retry_after_header_si_aplica)
        """
        if not RE_ID_TRABAJO.fullmatch(id_trabajo):
            raise ErrorApi(TipoError.TRABAJO_INEXISTENTE, f"trabajo {id_trabajo} no encontrado")

        dir_trabajo = os.path.join(self.dir_trabajos, id_trabajo)
        ruta_estado = os.path.join(dir_trabajo, "estado.json")
        if not os.path.exists(ruta_estado):
            raise ErrorApi(TipoError.TRABAJO_INEXISTENTE, f"trabajo {id_trabajo} no encontrado")

        try:
            with open(ruta_estado, "r", encoding="utf-8") as f:
                estado = json.load(f)
        except Exception:
            raise ErrorApi(TipoError.TRABAJO_INEXISTENTE, f"trabajo {id_trabajo} no se puede leer")

        # Autorización de consulta: solo el dueño o un admin (§1)
        if not es_admin and estado.get("usuario") != usuario_solicitante:
            raise ErrorApi(TipoError.TRABAJO_INEXISTENTE, f"trabajo {id_trabajo} no encontrado")

        # Expiración por retención (24 h desde fin)
        fin_iso = estado.get("tiempos", {}).get("fin")
        if fin_iso:
            try:
                dt_fin = datetime.fromisoformat(fin_iso.replace("Z", "+00:00"))
                if time.time() - dt_fin.timestamp() > self.config.video_retencion_horas * 3600:
                    shutil.rmtree(dir_trabajo, ignore_errors=True)
                    raise ErrorApi(TipoError.TRABAJO_INEXISTENTE, f"el trabajo {id_trabajo} expiró y fue purgado")
            except ValueError:
                pass

        estado_str = estado.get("estado")
        progreso = None
        posicion_en_cola = None
        retry_after = None

        if estado_str == "en_cola":
            posicion_en_cola = max(1, self.pool.posicion_en_cola(id_trabajo) if hasattr(self.pool, "posicion_en_cola") else 1)
            retry_after = "5"
        elif estado_str == "procesando":
            retry_after = "5"
            ruta_progreso = os.path.join(dir_trabajo, "progreso.json")
            if os.path.exists(ruta_progreso):
                try:
                    with open(ruta_progreso, "r", encoding="utf-8") as f:
                        p_data = json.load(f)
                        progreso = p_data.get("progreso")
                except Exception:
                    progreso = None
        elif estado_str == "listo":
            progreso = 100

        expira_iso = calcular_expiracion_iso(fin_iso, self.config.video_retencion_horas)

        contenido = {
            "id": id_trabajo,
            "estado": estado_str,
            "progreso": progreso,
            "calidades": estado.get("calidades", []),
            "original": estado.get("original", {}),
            "tiempos": estado.get("tiempos", {}),
            "expira": expira_iso,
            "error": estado.get("error"),
        }

        if posicion_en_cola is not None:
            contenido["posicion_en_cola"] = posicion_en_cola
        if estado_str == "listo":
            contenido["resultados"] = estado.get("resultados", {})

        return contenido, retry_after

    # --------------------------------------------------------------------------
    # Descarga de Archivo Transcodificado (GET /videos/{id}/{calidad})
    # --------------------------------------------------------------------------
    def preparar_descarga(self, id_trabajo: str, calidad: int, usuario_solicitante: str, es_admin: bool) -> Tuple[str, str]:
        """Verifica disponibilidad y retorna la ruta del archivo y el nombre de descarga."""
        if not RE_ID_TRABAJO.fullmatch(id_trabajo):
            raise ErrorApi(TipoError.TRABAJO_INEXISTENTE, f"trabajo {id_trabajo} no encontrado")

        dir_trabajo = os.path.join(self.dir_trabajos, id_trabajo)
        ruta_estado = os.path.join(dir_trabajo, "estado.json")
        if not os.path.exists(ruta_estado):
            raise ErrorApi(TipoError.TRABAJO_INEXISTENTE, f"trabajo {id_trabajo} no encontrado")

        try:
            with open(ruta_estado, "r", encoding="utf-8") as f:
                estado = json.load(f)
        except Exception:
            raise ErrorApi(TipoError.TRABAJO_INEXISTENTE, f"trabajo {id_trabajo} no se puede leer")

        # Autorización
        if not es_admin and estado.get("usuario") != usuario_solicitante:
            raise ErrorApi(TipoError.TRABAJO_INEXISTENTE, f"trabajo {id_trabajo} no encontrado")

        # Calidad solicitada en el pedido original
        calidades = estado.get("calidades", [])
        if calidad not in calidades:
            raise ErrorApi(
                TipoError.CALIDAD_NO_SOLICITADA,
                f"la calidad {calidad}p no fue solicitada para el trabajo {id_trabajo}"
            )

        # Estado listo
        if estado.get("estado") != "listo":
            raise ErrorApi(
                TipoError.RESULTADO_NO_LISTO,
                f"el trabajo {id_trabajo} aún no está listo (estado: {estado.get('estado')})"
            )

        # Buscar archivo en salida/
        ruta_video = os.path.join(dir_trabajo, "salida", f"{calidad}.mp4")
        if not os.path.exists(ruta_video):
            raise ErrorApi(
                TipoError.ERROR_INTERNO,
                f"el archivo transcodificado para {calidad}p no se encuentra en disco"
            )

        # Formar nombre de descarga original_calidadp.mp4 sanitizado contra inyecciones de cabecera
        nombre_original = estado.get("original", {}).get("nombre", "video.mp4")
        nombre_sanitizado = self.sanitizar_nombre_archivo(nombre_original)
        nombre_base, _ = os.path.splitext(nombre_sanitizado)
        nombre_base = re.sub(r'[^a-zA-Z0-9_-]', '_', nombre_base).strip('._ ') or "video"
        nombre_descarga = f"{nombre_base}_{calidad}p.mp4"

        return ruta_video, nombre_descarga

    # --------------------------------------------------------------------------
    # Auxiliares
    # --------------------------------------------------------------------------
    @staticmethod
    def _escribir_json_atomico(ruta_destino: str, datos: dict) -> None:
        """Escribe datos serializados a JSON en un .tmp y renombra atómicamente con os.replace.
        
        Si ocurre un fallo de E/S o permisos, no se silencia el error: se limpia el temporal
        y se propaga un ErrorApi(ERROR_INTERNO) para mantener la consistencia entre memoria y disco.
        """
        directorio = os.path.dirname(ruta_destino)
        if not os.path.exists(directorio):
            raise ErrorApi(
                TipoError.ERROR_INTERNO,
                f"el directorio {directorio} no existe al persistir estado"
            )
        ruta_tmp = f"{ruta_destino}.tmp"
        try:
            with open(ruta_tmp, "w", encoding="utf-8") as f:
                json.dump(datos, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(ruta_tmp, ruta_destino)
        except OSError as err:
            try:
                if os.path.exists(ruta_tmp):
                    os.remove(ruta_tmp)
            except OSError:
                pass
            raise ErrorApi(
                TipoError.ERROR_INTERNO,
                "error al persistir el estado del video en disco",
                interno=str(err)
            )
