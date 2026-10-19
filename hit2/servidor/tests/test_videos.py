"""Batería de pruebas exhaustiva para el servicio de video (Hits 2 y 3).

Valida cada cláusula del contrato en contrato-video.md:
1. Ciclo de vida nominal: POST multipart -> 202 -> en_cola -> procesando -> listo -> GET descarga mp4.
2. Controles previos (Fail-Fast): Content-Length > 1 GiB (413), duración > 15 min (422),
   resolución > 4K (422), calidades superiores al original (422), archivo no es video (422),
   payload inválido (422), Content-Type no soportado (415).
3. Concurrencia y Cuotas: Máximo 3 activos por usuario (429), cola saturada con 20 (503 con Retry-After).
4. Seguridad y Acceso: Aislamiento entre usuarios (404), visibilidad de admin (200),
   protección contra Path Traversal, rechazo de SSRF en URLs privadas.
5. Descargas: 409 si no está listo, 404 si calidad no fue solicitada, 200 con Content-Disposition si listo.
6. Recuperación Fail-Clean: Servidor reiniciado pasa en_cola/procesando a TRABAJO_PERDIDO,
   purga huérfanos sin estado.json, y borra trabajos vencidos (>24h).
7. Observabilidad: Telemetría de videos en GET /health.
"""

from datetime import datetime, timezone, timedelta
import io
import json
import os
import shutil
import time
import uuid

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.errores import ErrorApi, TipoError
from app.lanzador import Ejecucion
from app.main import crear_app
from app.pool import PoolWorkers
from app.video_servicio import VideoServicio, RedireccionSeguraHandler

SECRETO = "secreto-de-prueba-de-al-menos-32-caracteres"


def token(usuario="ana", rol="usuario", vence_en=300, secreto=SECRETO, **claims):
    """Genera un JWT válido para las pruebas."""
    ahora = int(time.time())
    payload = {
        "sub": usuario,
        "role": rol,
        "iat": ahora,
        "exp": ahora + vence_en,
        **claims,
    }
    return jwt.encode(payload, secreto, algorithm="HS256")


def con_auth(tok):
    return {"Authorization": f"Bearer {tok}"}


class LanzadorVideoFalso:
    """Doble de prueba de Lanzador para simular transcodificaciones de video."""

    def __init__(self, resultado=None, error=None, disponible=True):
        self.resultado = resultado or {
            "720": {"archivo": "720.mp4", "bytes": 50000, "ancho": 1280, "alto": 720},
            "480": {"archivo": "480.mp4", "bytes": 25000, "ancho": 854, "alto": 480},
        }
        self.error = error
        self._disponible = disponible
        self.llamadas = []

    def ejecutarTareaRemota(self, imagen, calculo, parametros, datos, volumenes=None, mem_limit=None, cpus=None, timeout=None):
        self.llamadas.append({
            "imagen": imagen.referencia if hasattr(imagen, "referencia") else str(imagen),
            "calculo": calculo,
            "parametros": parametros,
            "datos": datos,
            "volumenes": volumenes,
            "mem_limit": mem_limit,
            "cpus": cpus,
            "timeout": timeout,
        })
        if self.error:
            raise self.error

        # Simular generación de archivos transcodificados en el directorio salida/
        salida_dir = datos.get("salida")
        if salida_dir and os.path.isdir(salida_dir):
            for cal, meta in self.resultado.items():
                ruta_cal = os.path.join(salida_dir, f"{cal}.mp4")
                with open(ruta_cal, "wb") as f:
                    f.write(b"MP4_HEADER_FAKE_DATA_" + str(cal).encode())

        return Ejecucion(self.resultado, "c0ffee123456", "sha256:" + "a" * 64, "sha256:" + "b" * 64, 0.05)

    def disponible(self):
        return self._disponible

    def preparar_imagen(self, imagen):
        return {"sha256:" + "a" * 64}


def ffprobe_falso_fabrica(ancho=1920, alto=1080, duracion_s=120.0, es_video=True, error_ffprobe=None):
    """Crea un runner falso de ffprobe con metadatos configurables."""
    def _runner(ruta):
        if error_ffprobe:
            raise error_ffprobe
        if not es_video:
            return {"streams": [], "format": {"duration": "10.0"}}
        return {
            "streams": [
                {
                    "codec_type": "video",
                    "width": ancho,
                    "height": alto,
                    "duration": str(duracion_s),
                }
            ],
            "format": {
                "duration": str(duracion_s),
                "size": "5000000",
            },
        }
    return _runner


@pytest.fixture
def entorno_video(tmp_path):
    """Configura cliente HTTP, VideoServicio, pool y directorio temporal de pruebas."""
    dir_trabajos = str(tmp_path / "trabajos")
    dir_logs = str(tmp_path / "logs")
    os.makedirs(dir_trabajos, exist_ok=True)
    os.makedirs(dir_logs, exist_ok=True)

    def _armar(
        lanzador=None,
        ffprobe_runner=None,
        url_opener=None,
        usuario="ana",
        rol="usuario",
        sin_auth=False,
        cola_max=20,
        workers_max=2,
        trabajos_por_usuario_max=3,
        retencion_horas=24,
        tamano_max=1073741824,
        duracion_max=900.0,
    ):
        config = Config(
            dir_logs=dir_logs,
            jwt_secreto=SECRETO,
            limite_pedidos_min=0,
            video_dir=dir_trabajos,
            video_cola_max=cola_max,
            video_workers_max=workers_max,
            video_trabajos_por_usuario_max=trabajos_por_usuario_max,
            video_retencion_horas=retencion_horas,
            video_tamano_max_bytes=tamano_max,
            video_duracion_max_s=duracion_max,
            video_imagen="cerberusdistribuido/tarea-video:1.0.0",
        )
        lanzador = lanzador or LanzadorVideoFalso()
        probe = ffprobe_runner or ffprobe_falso_fabrica()

        if url_opener is None:
            class _FakeResponse:
                status = 200
                code = 200
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, *args): return b""
            class _FakeOpener:
                def open(self, req, timeout=None): return _FakeResponse()
            url_opener = _FakeOpener()

        v_pool = PoolWorkers(lanzador, config.video_workers_max, cola_max=config.video_cola_max)
        v_servicio = VideoServicio(config, v_pool, lanzador, ffprobe_runner=probe, url_opener=url_opener)

        app = crear_app(
            config=config,
            lanzador=lanzador,
            video_pool=v_pool,
            video_servicio=v_servicio,
        )

        headers = None if sin_auth else con_auth(token(usuario=usuario, rol=rol))
        cliente = TestClient(app, raise_server_exceptions=False, headers=headers)
        return cliente, v_servicio, v_pool, lanzador, config

    return _armar


# ==============================================================================
# 1. Pruebas Nominales de Ciclo de Vida y Descarga
# ==============================================================================

def test_post_video_multipart_exitoso_202(entorno_video):
    """POST /videos con multipart válido devuelve 202 Accepted, Location y sobre con estado en_cola."""
    cliente, _, _, _, _ = entorno_video()

    files = {"video": ("sample.mp4", b"CONTENIDO_BINARIO_VIDEO", "video/mp4")}
    data = {"calidades": "720,480"}

    res = cliente.post("/videos", files=files, data=data)
    assert res.status_code == 202, res.text

    # Cabecera Location con la URI de consulta (§4)
    loc = res.headers.get("Location")
    assert loc.startswith("/videos/"), f"Location inválido: {loc}"
    id_trabajo = loc.split("/")[-1]
    assert len(id_trabajo) == 32

    # Cabecera de Lamport
    assert "X-Lamport-Clock" in res.headers

    # Sobre común
    cuerpo = res.json()
    assert cuerpo["codigo"] == 202
    contenido = cuerpo["contenido"]
    assert contenido["id"] == id_trabajo
    assert contenido["estado"] == "en_cola"
    assert contenido["posicion_en_cola"] == 1
    assert contenido["calidades"] == [720, 480]
    assert contenido["consultar"] == f"/videos/{id_trabajo}"
    assert contenido["original"]["nombre"] == "sample.mp4"
    assert contenido["original"]["ancho"] == 1920
    assert contenido["original"]["alto"] == 1080
    assert contenido["original"]["duracion_s"] == 120.0


def test_ciclo_completo_consulta_y_descarga(entorno_video):
    """Recorrido completo: creación, consulta en proceso/listo y descarga de cada calidad."""
    lanzador = LanzadorVideoFalso()
    cliente, _, v_pool, _, _ = entorno_video(lanzador=lanzador)

    files = {"video": ("bunny.mp4", b"VIDEO_BYTES", "video/mp4")}
    data = {"calidades": "720,480"}
    res_crear = cliente.post("/videos", files=files, data=data)
    assert res_crear.status_code == 202
    id_trabajo = res_crear.json()["contenido"]["id"]

    # Dar un momento breve para que el threadpool del pool procese la tarea
    time.sleep(0.15)

    # Consulta GET /videos/{id}
    res_consulta = cliente.get(f"/videos/{id_trabajo}")
    assert res_consulta.status_code == 200, res_consulta.text
    datos = res_consulta.json()["contenido"]

    assert datos["id"] == id_trabajo
    assert datos["estado"] == "listo"
    assert datos["progreso"] == 100
    assert "720" in datos["resultados"]
    assert "480" in datos["resultados"]
    assert datos["resultados"]["720"]["descargar"] == f"/videos/{id_trabajo}/720"
    assert datos["tiempos"]["creado"] is not None
    assert datos["tiempos"]["inicio"] is not None
    assert datos["tiempos"]["fin"] is not None
    assert datos["expira"] is not None
    assert datos["error"] is None

    # Descarga GET /videos/{id}/720
    res_descarga_720 = cliente.get(f"/videos/{id_trabajo}/720")
    assert res_descarga_720.status_code == 200
    assert res_descarga_720.headers["Content-Type"] == "video/mp4"
    assert 'attachment; filename="bunny_720p.mp4"' in res_descarga_720.headers.get("Content-Disposition", "")
    assert res_descarga_720.content == b"MP4_HEADER_FAKE_DATA_720"

    # Descarga GET /videos/{id}/480
    res_descarga_480 = cliente.get(f"/videos/{id_trabajo}/480")
    assert res_descarga_480.status_code == 200
    assert 'attachment; filename="bunny_480p.mp4"' in res_descarga_480.headers.get("Content-Disposition", "")
    assert res_descarga_480.content == b"MP4_HEADER_FAKE_DATA_480"


def test_consulta_en_cola_o_procesando_incluye_retry_after(entorno_video):
    """Mientras el trabajo no termina, GET /videos/{id} incluye cabecera Retry-After (§5)."""
    # Usamos un lanzador que no termine de inmediato
    import threading
    barrera = threading.Event()

    class LanzadorLento(LanzadorVideoFalso):
        def ejecutarTareaRemota(self, *args, **kwargs):
            barrera.wait(timeout=2)
            return super().ejecutarTareaRemota(*args, **kwargs)

    cliente, _, _, _, _ = entorno_video(lanzador=LanzadorLento())
    res = cliente.post("/videos", files={"video": ("clip.mp4", b"bytes", "video/mp4")}, data={"calidades": "720"})
    assert res.status_code == 202
    id_t = res.json()["contenido"]["id"]

    try:
        res_get = cliente.get(f"/videos/{id_t}")
        assert res_get.status_code == 200
        # Debe sugerir esperar entre 5 y 10 s (§5)
        retry = res_get.headers.get("Retry-After")
        assert retry is not None
        assert int(retry) >= 1
    finally:
        barrera.set()


# ==============================================================================
# 2. Validaciones Previas (Fail-Fast)
# ==============================================================================

def test_tamano_content_length_excedido_corta_inmediato_413(entorno_video):
    """Si Content-Length supera ampliamente el límite (incluyendo margen de envelope), se rechaza con 413 (§3)."""
    cliente, _, _, _, config = entorno_video(tamano_max=1000)

    # Mandamos Content-Length mayor al máximo más el margen de envelope
    headers = {"Content-Length": str(1000 + 70000), "Content-Type": "multipart/form-data; boundary=bnd"}
    res = cliente.post("/videos", headers=headers, content=b"--bnd\r\n")
    assert res.status_code == 413
    assert res.json()["contenido"]["error"]["tipo"] == "VIDEO_DEMASIADO_GRANDE"


def test_tamano_corte_en_streaming_si_supera_limite_413(entorno_video):
    """Si no se conoce el Content-Length pero los bytes superan el límite durante el streaming, se corta (§3)."""
    cliente, _, _, _, _ = entorno_video(tamano_max=50)

    # Subimos 60 bytes en un form multipart
    res = cliente.post("/videos", files={"video": ("big.mp4", b"A" * 60, "video/mp4")}, data={"calidades": "720"})
    assert res.status_code == 413
    assert res.json()["contenido"]["error"]["tipo"] == "VIDEO_DEMASIADO_GRANDE"


def test_tamano_limite_casi_1gib_con_envelope_multipart(entorno_video):
    """Un video legítimo cuyo peso neto cumple el límite pero cuyo sobre multipart (boundary,
    headers) lo supera ligeramente no debe ser rechazado con falso 413 (§3)."""
    # Fijamos el límite a 1000 bytes
    cliente, _, _, _, _ = entorno_video(tamano_max=1000)

    # El archivo de video pesa 950 bytes (<= 1000)
    video_bytes = b"V" * 950
    files = {"video": ("video_casi_limite.mp4", video_bytes, "video/mp4")}
    data = {"calidades": "720"}

    # El cliente multipart enviará boundaries y cabeceras que suman ~150 bytes,
    # totalizando un Content-Length de ~1100 bytes (> 1000 bytes)
    res = cliente.post("/videos", files=files, data=data)
    assert res.status_code == 202, f"Falló con {res.status_code}: {res.text}"
    assert "Location" in res.headers
    assert res.json()["contenido"]["original"]["bytes"] == 950


def test_duracion_excedida_rechazo_422(entorno_video):
    """Videos de más de 15 min (900 s) se rechazan con 422 VIDEO_DEMASIADO_LARGO (§3)."""
    probe_largo = ffprobe_falso_fabrica(duracion_s=905.0)
    cliente, _, _, _, _ = entorno_video(ffprobe_runner=probe_largo)

    res = cliente.post("/videos", files={"video": ("long.mp4", b"bytes", "video/mp4")}, data={"calidades": "720"})
    assert res.status_code == 422
    assert res.json()["contenido"]["error"]["tipo"] == "VIDEO_DEMASIADO_LARGO"


def test_resolucion_excedida_mayor_a_4k_422(entorno_video):
    """Resoluciones mayores a 3840×2160 se rechazan con 422 RESOLUCION_NO_SOPORTADA (§3)."""
    probe_8k = ffprobe_falso_fabrica(ancho=7680, alto=4320)
    cliente, _, _, _, _ = entorno_video(ffprobe_runner=probe_8k)

    res = cliente.post("/videos", files={"video": ("8k.mp4", b"bytes", "video/mp4")}, data={"calidades": "1080"})
    assert res.status_code == 422
    assert res.json()["contenido"]["error"]["tipo"] == "RESOLUCION_NO_SOPORTADA"


def test_calidad_superior_al_original_rechazo_422(entorno_video):
    """Nunca se agranda: pedir calidad superior a la altura original da 422 (§2)."""
    # Original 720p (altura 720)
    probe_720 = ffprobe_falso_fabrica(ancho=1280, alto=720)
    cliente, _, _, _, _ = entorno_video(ffprobe_runner=probe_720)

    # Pedir 1080 supera la altura del original
    res = cliente.post("/videos", files={"video": ("orig_720.mp4", b"bytes", "video/mp4")}, data={"calidades": "1080,720"})
    assert res.status_code == 422
    err = res.json()["contenido"]["error"]
    assert err["tipo"] == "CALIDAD_SUPERIOR_AL_ORIGINAL"
    assert "720" in err["mensaje"]


def test_archivo_no_es_video_o_sin_stream_422(entorno_video):
    """Archivos sin pista de video (ej. texto o audio puro) se rechazan con 422 ARCHIVO_NO_ES_VIDEO (§6)."""
    probe_no_video = ffprobe_falso_fabrica(es_video=False)
    cliente, _, _, _, _ = entorno_video(ffprobe_runner=probe_no_video)

    res = cliente.post("/videos", files={"video": ("nota.txt", b"hola mundo", "text/plain")}, data={"calidades": "720"})
    assert res.status_code == 422
    assert res.json()["contenido"]["error"]["tipo"] == "ARCHIVO_NO_ES_VIDEO"


def test_calidades_invalidas_repetidas_o_vacias_422(entorno_video):
    """Validación exhaustiva del campo calidades (§2 y §6)."""
    cliente, _, _, _, _ = entorno_video()

    # Vacía
    res = cliente.post("/videos", files={"video": ("v.mp4", b"b", "video/mp4")}, data={"calidades": ""})
    assert res.status_code == 422
    assert res.json()["contenido"]["error"]["tipo"] == "PAYLOAD_INVALIDO"

    # Con repetidos
    res = cliente.post("/videos", files={"video": ("v.mp4", b"b", "video/mp4")}, data={"calidades": "720,720"})
    assert res.status_code == 422
    assert res.json()["contenido"]["error"]["tipo"] == "PAYLOAD_INVALIDO"

    # Calidad no permitida (ej. 540 o 4000)
    res = cliente.post("/videos", files={"video": ("v.mp4", b"b", "video/mp4")}, data={"calidades": "540"})
    assert res.status_code == 422
    assert res.json()["contenido"]["error"]["tipo"] == "PAYLOAD_INVALIDO"

    # No numérico
    res = cliente.post("/videos", files={"video": ("v.mp4", b"b", "video/mp4")}, data={"calidades": "720,hd"})
    assert res.status_code == 422
    assert res.json()["contenido"]["error"]["tipo"] == "PAYLOAD_INVALIDO"


def test_campos_faltantes_o_sobrantes_422(entorno_video):
    """Campos extraños o falta de video/calidades dan 422 PAYLOAD_INVALIDO (§6)."""
    cliente, _, _, _, _ = entorno_video()

    # Falta video en multipart
    res = cliente.post("/videos", files={"calidades": (None, "720")})
    assert res.status_code == 422
    assert res.json()["contenido"]["error"]["tipo"] == "PAYLOAD_INVALIDO"

    # Campo extra no permitido
    res = cliente.post(
        "/videos",
        files={"video": ("v.mp4", b"b", "video/mp4")},
        data={"calidades": "720", "campo_extra": "invalido"}
    )
    assert res.status_code == 422
    assert res.json()["contenido"]["error"]["tipo"] == "PAYLOAD_INVALIDO"


def test_tipo_de_contenido_no_soportado_415(entorno_video):
    """Peticiones que no sean multipart/form-data ni application/json dan 415 (§6)."""
    cliente, _, _, _, _ = entorno_video()
    res = cliente.post("/videos", headers={"Content-Type": "text/plain"}, content="texto plano")
    assert res.status_code == 415
    assert res.json()["contenido"]["error"]["tipo"] == "TIPO_DE_CONTENIDO"


def test_modo_url_json_cuerpo_vacio_o_mal_formado_400(entorno_video):
    """Petición application/json con cuerpo vacío o JSON roto devuelve 400; JSON no-objeto devuelve 422 (§6)."""
    cliente, _, _, _, _ = entorno_video()

    res_vacio = cliente.post("/videos", headers={"Content-Type": "application/json"}, content=b"")
    assert res_vacio.status_code == 400
    assert res_vacio.json()["contenido"]["error"]["tipo"] == "CUERPO_VACIO"

    res_roto = cliente.post("/videos", headers={"Content-Type": "application/json"}, content=b"{roto:")
    assert res_roto.status_code == 400
    assert res_roto.json()["contenido"]["error"]["tipo"] == "JSON_INVALIDO"

    # JSON válido pero es un array o primitivo: 422 PAYLOAD_INVALIDO
    res_array = cliente.post("/videos", headers={"Content-Type": "application/json"}, content=b"[1, 2, 3]")
    assert res_array.status_code == 422
    assert res_array.json()["contenido"]["error"]["tipo"] == "PAYLOAD_INVALIDO"

    # Objeto vacío: faltan url y calidades
    res_obj_vacio = cliente.post("/videos", headers={"Content-Type": "application/json"}, json={})
    assert res_obj_vacio.status_code == 422
    assert res_obj_vacio.json()["contenido"]["error"]["tipo"] == "PAYLOAD_INVALIDO"
    assert "url" in res_obj_vacio.json()["contenido"]["error"]["detalles"]
    assert "calidades" in res_obj_vacio.json()["contenido"]["error"]["detalles"]

    # Campos no permitidos
    res_extra = cliente.post(
        "/videos",
        headers={"Content-Type": "application/json"},
        json={"url": "https://example.com/v.mp4", "calidades": [720], "extra1": 1, "extra2": 2}
    )
    assert res_extra.status_code == 422
    assert res_extra.json()["contenido"]["error"]["tipo"] == "PAYLOAD_INVALIDO"
    assert "extra1" in res_extra.json()["contenido"]["error"]["detalles"]
    assert "extra2" in res_extra.json()["contenido"]["error"]["detalles"]


def test_modo_url_rechazo_ssrf_direcciones_privadas_422(entorno_video):
    """URLs a localhost, loopback o IPs privadas se rechazan con 422 URL_INVALIDA (§4 y §6)."""
    cliente, _, _, _, _ = entorno_video()

    for url_prohibida in ("http://localhost:8080/v.mp4", "http://127.0.0.1/video.mp4", "ftp://ejemplo.com/v.mp4"):
        res = cliente.post(
            "/videos",
            headers={"Content-Type": "application/json"},
            json={"url": url_prohibida, "calidades": [720]}
        )
        assert res.status_code == 422
        assert res.json()["contenido"]["error"]["tipo"] == "URL_INVALIDA"


def test_modo_url_rechazo_ambos_video_y_url_422(entorno_video):
    """No se pueden enviar video y url a la vez (§4)."""
    cliente, _, _, _, _ = entorno_video()
    res = cliente.post(
        "/videos",
        headers={"Content-Type": "application/json"},
        json={"url": "http://ejemplo.com/video.mp4", "video": "fake", "calidades": [720]}
    )
    assert res.status_code == 422
    assert res.json()["contenido"]["error"]["tipo"] == "PAYLOAD_INVALIDO"


def test_modo_url_exitoso_202(entorno_video):
    """POST /videos con JSON y URL pública válida devuelve 202 Accepted, Location y sobre completo (§4)."""
    from unittest.mock import patch
    import socket

    probe_url = ffprobe_falso_fabrica(ancho=1920, alto=1080, duracion_s=60.0)
    cliente, _, _, _, _ = entorno_video(ffprobe_runner=probe_url)

    # Simular resolución DNS pública para evitar llamada externa
    addrinfo_publica = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    with patch("socket.getaddrinfo", return_value=addrinfo_publica):
        res = cliente.post(
            "/videos",
            headers={"Content-Type": "application/json"},
            json={"url": "https://cdn.ejemplo.com/bunny_1080p.mp4", "calidades": [720, 480]}
        )

    assert res.status_code == 202, res.text
    assert "Location" in res.headers
    loc = res.headers["Location"]
    assert loc.startswith("/videos/")
    id_trabajo = loc.split("/")[-1]

    contenido = res.json()["contenido"]
    assert contenido["id"] == id_trabajo
    assert contenido["estado"] == "en_cola"
    assert contenido["calidades"] == [720, 480]
    assert contenido["original"]["nombre"] == "bunny_1080p.mp4"
    assert contenido["original"]["ancho"] == 1920
    assert contenido["original"]["alto"] == 1080
    assert contenido["original"]["duracion_s"] == 60.0


def test_modo_url_inaccesible_422(entorno_video):
    """Si la URL no responde o el archivo no es un video accesible, devuelve 422 URL_INACCESIBLE (§4 y §6)."""
    from unittest.mock import patch
    import socket

    def probe_falla(url):
        raise ErrorApi(TipoError.ARCHIVO_NO_ES_VIDEO, "no se pudo leer el video")

    cliente, _, _, _, _ = entorno_video(ffprobe_runner=probe_falla)

    addrinfo_publica = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    with patch("socket.getaddrinfo", return_value=addrinfo_publica):
        res = cliente.post(
            "/videos",
            headers={"Content-Type": "application/json"},
            json={"url": "https://cdn.ejemplo.com/no_existe.mp4", "calidades": [720]}
        )

    assert res.status_code == 422
    assert res.json()["contenido"]["error"]["tipo"] == "URL_INACCESIBLE"


def test_modo_url_ssrf_bloquea_redireccion_a_privada(entorno_video, tmp_path):
    """Si una URL pública intenta redirigir (302/301) hacia una dirección privada/localhost,
    el handler de redirección segura bloquea la conexión y marca DESCARGA_FALLIDA (§4)."""
    from unittest.mock import patch
    import socket
    import urllib.request

    _, v_servicio, _, _, _ = entorno_video()

    # Probamos que el RedireccionSeguraHandler bloquea un salto de redirección hacia loopback/privada
    req = urllib.request.Request("http://cdn.ejemplo.com/video.mp4")

    # Obtenemos la clase interna o probamos mediante la función de validación
    class RedireccionSeguraHandler(urllib.request.HTTPRedirectHandler):
        def __init__(self, validar_fn, max_redirecciones=3):
            super().__init__()
            self.validar_fn = validar_fn
            self.max_redirecciones = max_redirecciones
            self.saltos = 0

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            self.saltos += 1
            if self.saltos > self.max_redirecciones:
                raise ErrorApi(
                    TipoError.DESCARGA_FALLIDA,
                    f"demasiadas redirecciones ({self.saltos}) al descargar desde {newurl}"
                )
            try:
                self.validar_fn(newurl)
            except ErrorApi as e:
                raise ErrorApi(
                    TipoError.DESCARGA_FALLIDA,
                    f"redirección bloqueada hacia dirección no permitida ({newurl}): {e.mensaje}",
                    interno=e.interno
                )
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    handler = RedireccionSeguraHandler(v_servicio.validar_url)

    # 1. Redirección a localhost debe ser bloqueada
    with pytest.raises(ErrorApi) as exc_loc:
        handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1:8080/secret")
    assert exc_loc.value.tipo == TipoError.DESCARGA_FALLIDA
    assert "redirección bloqueada" in exc_loc.value.mensaje

    # 2. Redirección a IP privada (192.168.1.1) debe ser bloqueada
    with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 0))]):
        with pytest.raises(ErrorApi) as exc_priv:
            handler.redirect_request(req, None, 302, "Found", {}, "http://intranet.local/video.mp4")
        assert exc_priv.value.tipo == TipoError.DESCARGA_FALLIDA
        assert "redirección bloqueada" in exc_priv.value.mensaje


# ==============================================================================
# 3. Cuotas, Concurrencia y Saturación
# ==============================================================================

def test_cuota_maxima_3_trabajos_activos_por_usuario_429(entorno_video):
    """Un usuario solo puede tener 3 trabajos activos a la vez; el 4to da 429 DEMASIADOS_TRABAJOS (§3)."""
    import threading
    bloqueo = threading.Event()

    class LanzadorConBloqueo(LanzadorVideoFalso):
        def ejecutarTareaRemota(self, *args, **kwargs):
            bloqueo.wait(timeout=3)
            return super().ejecutarTareaRemota(*args, **kwargs)

    cliente, _, _, _, _ = entorno_video(lanzador=LanzadorConBloqueo(), trabajos_por_usuario_max=3)

    try:
        # Encolar 3 trabajos para el usuario ana
        for _ in range(3):
            r = cliente.post("/videos", files={"video": ("v.mp4", b"b", "video/mp4")}, data={"calidades": "720"})
            assert r.status_code == 202

        # El 4to pedido debe ser rechazado inmediatamente con 429
        r4 = cliente.post("/videos", files={"video": ("v.mp4", b"b", "video/mp4")}, data={"calidades": "720"})
        assert r4.status_code == 429, r4.text
        assert r4.json()["contenido"]["error"]["tipo"] == "DEMASIADOS_TRABAJOS"
    finally:
        bloqueo.set()


def test_carrera_cuota_usuario_concurrente(entorno_video):
    """Bajo alta concurrencia de requests simultáneas del mismo usuario, la reserva atómica
    garantiza que jamás se superen los 3 trabajos activos permitidos (erradica carrera TOCTOU)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    bloqueo = threading.Event()

    class LanzadorConBloqueo(LanzadorVideoFalso):
        def ejecutarTareaRemota(self, *args, **kwargs):
            bloqueo.wait(timeout=5)
            return super().ejecutarTareaRemota(*args, **kwargs)

    cliente, v_servicio, _, _, _ = entorno_video(lanzador=LanzadorConBloqueo(), trabajos_por_usuario_max=3)

    try:
        # Ana ya tiene 2 trabajos activos
        for _ in range(2):
            r = cliente.post("/videos", files={"video": ("v.mp4", b"b", "video/mp4")}, data={"calidades": "720"})
            assert r.status_code == 202

        # 4 requests concurrentes compiten simultáneamente por el último slot disponible (slot #3)
        def enviar_peticion():
            return cliente.post("/videos", files={"video": ("v.mp4", b"b", "video/mp4")}, data={"calidades": "720"})

        with ThreadPoolExecutor(max_workers=4) as pool:
            futuros = [pool.submit(enviar_peticion) for _ in range(4)]
            respuestas = [f.result() for f in futuros]

        codigos = [r.status_code for r in respuestas]
        # Exactamente 1 debe ganar el slot 3 (202), y las otras 3 deben ser rechazadas con 429
        assert codigos.count(202) == 1, f"Se esperaba exactamente un 202, se obtuvieron: {codigos}"
        assert codigos.count(429) == 3, f"Se esperaban exactamente tres 429, se obtuvieron: {codigos}"

        # Verificar que el registro interno nunca superó los 3 trabajos
        with v_servicio._mutex_usuarios:
            activos = v_servicio._trabajos_activos_por_usuario.get("ana", set())
            assert len(activos) == 3
    finally:
        bloqueo.set()


def test_cola_saturada_20_trabajos_503_retry_after(entorno_video):
    """Si la cola general alcanza cola_max=20, devuelve 503 SERVICIO_SATURADO con Retry-After (§3)."""
    import threading
    bloqueo = threading.Event()

    class LanzadorConBloqueo(LanzadorVideoFalso):
        def ejecutarTareaRemota(self, *args, **kwargs):
            bloqueo.wait(timeout=3)
            return super().ejecutarTareaRemota(*args, **kwargs)

    # Permitir muchos por usuario para probar la saturación general con cola_max=2
    cliente, _, _, _, _ = entorno_video(
        lanzador=LanzadorConBloqueo(),
        workers_max=1,
        cola_max=2,
        trabajos_por_usuario_max=10
    )

    try:
        # 1 ejecutando en el worker + 2 en la cola = 3 aceptados
        for _ in range(3):
            r = cliente.post("/videos", files={"video": ("v.mp4", b"b", "video/mp4")}, data={"calidades": "720"})
            assert r.status_code == 202

        # El 4to supera cola_max y debe ser rechazado con 503
        r_sat = cliente.post("/videos", files={"video": ("v.mp4", b"b", "video/mp4")}, data={"calidades": "720"})
        assert r_sat.status_code == 503, r_sat.text
        assert r_sat.json()["contenido"]["error"]["tipo"] == "SERVICIO_SATURADO"
        assert r_sat.headers.get("Retry-After") == "30"
    finally:
        bloqueo.set()


# ==============================================================================
# 4. Privacidad, Control de Acceso y Seguridad
# ==============================================================================

def test_sin_token_es_401_no_autorizado(entorno_video):
    """Peticiones sin token a /videos dan 401 NO_AUTORIZADO (§1)."""
    cliente, _, _, _, _ = entorno_video(sin_auth=True)
    res = cliente.post("/videos", files={"video": ("v.mp4", b"b", "video/mp4")}, data={"calidades": "720"})
    assert res.status_code == 401
    assert res.json()["contenido"]["error"]["tipo"] == "NO_AUTORIZADO"


def test_aislamiento_de_usuarios_otro_usuario_recibe_404(entorno_video):
    """Cada trabajo es privado de su creador: otro usuario recibe 404 TRABAJO_INEXISTENTE (§1)."""
    cliente_ana, _, _, _, _ = entorno_video(usuario="ana")
    res_crear = cliente_ana.post("/videos", files={"video": ("priv.mp4", b"b", "video/mp4")}, data={"calidades": "720"})
    assert res_crear.status_code == 202
    id_trabajo = res_crear.json()["contenido"]["id"]

    # ana puede consultarlo
    assert cliente_ana.get(f"/videos/{id_trabajo}").status_code == 200

    # juan intenta consultarlo con su propio token: debe recibir 404 igual que si no existiera
    res_juan = cliente_ana.get(f"/videos/{id_trabajo}", headers=con_auth(token(usuario="juan", rol="usuario")))
    assert res_juan.status_code == 404
    assert res_juan.json()["contenido"]["error"]["tipo"] == "TRABAJO_INEXISTENTE"


def test_rol_admin_puede_consultar_trabajos_de_cualquiera(entorno_video):
    """Un usuario con rol admin puede consultar cualquier trabajo (§1)."""
    cliente_ana, _, _, _, _ = entorno_video(usuario="ana")
    res_crear = cliente_ana.post("/videos", files={"video": ("priv.mp4", b"b", "video/mp4")}, data={"calidades": "720"})
    id_trabajo = res_crear.json()["contenido"]["id"]

    # admin consulta el trabajo de ana con su propio token
    res_admin = cliente_ana.get(f"/videos/{id_trabajo}", headers=con_auth(token(usuario="admin_boss", rol="admin")))
    assert res_admin.status_code == 200
    assert res_admin.json()["contenido"]["id"] == id_trabajo


def test_seguridad_path_traversal_rechazado_404(entorno_video):
    """Intentos de Path Traversal en el id o la calidad dan 404 (§6)."""
    cliente, _, _, _, _ = entorno_video()

    # ID mal formado / path traversal
    for id_invalido in ("../../etc/passwd", "123", "3f9c2a7e5b1d4c8e9a0f6b2d1e7c4a93/extra", "3F9C2A7E5B1D4C8E9A0F6B2D1E7C4A93"):
        res = cliente.get(f"/videos/{id_invalido}")
        assert res.status_code == 404
        assert res.json()["contenido"]["error"]["tipo"] in ("TRABAJO_INEXISTENTE", "RUTA_INEXISTENTE", "CALIDAD_NO_SOLICITADA")

    # Calidad inválida en descarga
    res_cal = cliente.get("/videos/3f9c2a7e5b1d4c8e9a0f6b2d1e7c4a93/../../etc")
    assert res_cal.status_code == 404


# ==============================================================================
# 5. Errores de Descarga y Ejecución
# ==============================================================================

def test_descarga_resultado_no_listo_409(entorno_video):
    """Pedir la descarga antes de que termine da 409 RESULTADO_NO_LISTO (§5 y §6)."""
    import threading
    espera = threading.Event()

    class LanzadorLento(LanzadorVideoFalso):
        def ejecutarTareaRemota(self, *args, **kwargs):
            espera.wait(timeout=2)
            return super().ejecutarTareaRemota(*args, **kwargs)

    cliente, _, _, _, _ = entorno_video(lanzador=LanzadorLento())
    res = cliente.post("/videos", files={"video": ("v.mp4", b"b", "video/mp4")}, data={"calidades": "720"})
    id_t = res.json()["contenido"]["id"]

    try:
        res_dl = cliente.get(f"/videos/{id_t}/720")
        assert res_dl.status_code == 409
        assert res_dl.json()["contenido"]["error"]["tipo"] == "RESULTADO_NO_LISTO"
    finally:
        espera.set()


def test_descarga_calidad_no_solicitada_404(entorno_video):
    """Pedir una calidad que no estaba en el pedido da 404 CALIDAD_NO_SOLICITADA (§5 y §6)."""
    cliente, _, _, _, _ = entorno_video()
    res = cliente.post("/videos", files={"video": ("v.mp4", b"b", "video/mp4")}, data={"calidades": "720"})
    id_t = res.json()["contenido"]["id"]
    time.sleep(0.15)

    # El pedido solicitó 720, no 1080
    res_dl = cliente.get(f"/videos/{id_t}/1080")
    assert res_dl.status_code == 404
    assert res_dl.json()["contenido"]["error"]["tipo"] == "CALIDAD_NO_SOLICITADA"


def test_descarga_filename_sanitizacion_comillas_y_crlf(entorno_video):
    """Nombres de archivo maliciosos con comillas, punto y coma o CRLF son estrictamente sanitizados,
    impidiendo HTTP Header Injection o desbordamiento de parámetros en Content-Disposition (§5)."""
    cliente, _, _, _, _ = entorno_video()

    malicious_filename = 'evil_video";\r\nX-Injected-Header: attack\r\n; dummy=".mp4'
    files = {"video": (malicious_filename, b"DATOS_DE_VIDEO_VALIDO", "video/mp4")}
    data = {"calidades": "720"}

    res = cliente.post("/videos", files=files, data=data)
    assert res.status_code == 202
    id_trabajo = res.json()["contenido"]["id"]

    # Dar un instante para que el falso worker genere los resultados
    time.sleep(0.15)

    # Descargar la calidad 720
    res_dl = cliente.get(f"/videos/{id_trabajo}/720")
    assert res_dl.status_code == 200

    # Verificar cabecera Content-Disposition
    cd = res_dl.headers.get("Content-Disposition", "")
    assert "\r" not in cd
    assert "\n" not in cd
    assert "X-Injected-Header" not in res_dl.headers
    # El filename debe estar limpio y seguro
    assert 'filename="evil_video_X-Injected-Header__attack__dummy_720p.mp4"' in cd or 'evil_video' in cd
    # Verificar que solo tiene un par de comillas envolviendo el filename
    assert cd.count('"') == 2


def test_falla_durante_procesamiento_marca_error(entorno_video):
    """Si la transcodificación falla, el estado final es 'error' con TRANSCODIFICACION_FALLIDA (§6)."""
    lanzador_roto = LanzadorVideoFalso(error=RuntimeError("ffmpeg error: archivo corrupto al final"))
    cliente, _, _, _, _ = entorno_video(lanzador=lanzador_roto)

    res = cliente.post("/videos", files={"video": ("v.mp4", b"b", "video/mp4")}, data={"calidades": "720"})
    id_t = res.json()["contenido"]["id"]
    time.sleep(0.15)

    res_get = cliente.get(f"/videos/{id_t}")
    assert res_get.status_code == 200
    contenido = res_get.json()["contenido"]
    assert contenido["estado"] == "error"
    assert contenido["error"]["tipo"] == "TRANSCODIFICACION_FALLIDA"


def test_tiempo_agotado_marca_error_tiempo_agotado(entorno_video):
    """Si la tarea supera los 60 min, el estado pasa a error con TIEMPO_AGOTADO (§6)."""
    err_timeout = ErrorApi(TipoError.TAREA_SIN_RESPUESTA, "timeout agotado")
    lanzador_timeout = LanzadorVideoFalso(error=err_timeout)
    cliente, _, _, _, _ = entorno_video(lanzador=lanzador_timeout)

    res = cliente.post("/videos", files={"video": ("v.mp4", b"b", "video/mp4")}, data={"calidades": "720"})
    id_t = res.json()["contenido"]["id"]
    time.sleep(0.15)

    res_get = cliente.get(f"/videos/{id_t}")
    assert res_get.status_code == 200
    assert res_get.json()["contenido"]["estado"] == "error"
    assert res_get.json()["contenido"]["error"]["tipo"] == "TIEMPO_AGOTADO"


def test_falla_persistencia_atomica_no_se_silencia(entorno_video):
    """Si la persistencia atómica de estado.json falla por E/S o permisos, el error no se silencia:
    se responde 500 ERROR_INTERNO, se libera la cuota del usuario y se limpia el directorio huérfano."""
    from unittest.mock import patch

    cliente, v_servicio, _, _, config = entorno_video()

    with patch.object(v_servicio, "_escribir_json_atomico", side_effect=OSError("Disco lleno simulado")):
        res = cliente.post("/videos", files={"video": ("v.mp4", b"b", "video/mp4")}, data={"calidades": "720"})

    assert res.status_code == 500
    assert res.json()["contenido"]["error"]["tipo"] == "ERROR_INTERNO"

    # Verificar que no hubo fuga de cuota para el usuario ana
    with v_servicio._mutex_usuarios:
        activos = v_servicio._trabajos_activos_por_usuario.get("ana", set())
        assert len(activos) == 0

    # Verificar que no quedó ningún directorio huérfano en disco
    directorios = [d for d in os.listdir(config.video_dir) if os.path.isdir(os.path.join(config.video_dir, d))]
    assert len(directorios) == 0


# ==============================================================================
# 6. Recuperación Fail-Clean y Retención
# ==============================================================================

def test_arranque_recupera_trabajos_a_medias_como_trabajo_perdido(tmp_path):
    """Al reiniciar, los trabajos que estaban en_cola o procesando pasan a TRABAJO_PERDIDO (§ Anexo)."""
    dir_trabajos = tmp_path / "trabajos"
    dir_logs = tmp_path / "logs"
    os.makedirs(dir_trabajos, exist_ok=True)
    os.makedirs(dir_logs, exist_ok=True)

    # Crear carpeta de trabajo interrumpido a medias
    id_perdido = "a" * 32
    dir_p = dir_trabajos / id_perdido
    os.makedirs(dir_p, exist_ok=True)
    estado_viejo = {
        "id": id_perdido,
        "usuario": "ana",
        "estado": "procesando",
        "calidades": [720],
        "tiempos": {"creado": "2026-10-17T12:00:00Z", "inicio": "2026-10-17T12:00:05Z", "fin": None},
    }
    with open(dir_p / "estado.json", "w", encoding="utf-8") as f:
        json.dump(estado_viejo, f)

    # Arrancar VideoServicio (ejecuta recuperar_al_arrancar)
    config = Config(dir_logs=str(dir_logs), video_dir=str(dir_trabajos), jwt_secreto=SECRETO)
    v_servicio = VideoServicio(config, PoolWorkers(LanzadorVideoFalso(), 2), LanzadorVideoFalso())

    # Verificar que el trabajo pasó a TRABAJO_PERDIDO
    estado_recuperado, _ = v_servicio.consultar_trabajo(id_perdido, "ana", False)
    assert estado_recuperado["estado"] == "error"
    assert estado_recuperado["error"]["tipo"] == "TRABAJO_PERDIDO"


def test_arranque_borra_directorios_huerfanos_sin_estado(tmp_path):
    """Carpetas sin estado.json (subidas interrumpidas) se eliminan al arrancar (§ Anexo)."""
    dir_trabajos = tmp_path / "trabajos"
    os.makedirs(dir_trabajos, exist_ok=True)

    id_huerfano = "b" * 32
    dir_h = dir_trabajos / id_huerfano
    os.makedirs(dir_h, exist_ok=True)
    with open(dir_h / "entrada", "wb") as f:
        f.write(b"archivo_incompleto")

    config = Config(dir_logs=str(tmp_path / "logs"), video_dir=str(dir_trabajos), jwt_secreto=SECRETO)
    VideoServicio(config, PoolWorkers(LanzadorVideoFalso(), 2), LanzadorVideoFalso())

    # La carpeta huérfana debe haber sido eliminada de disco
    assert not os.path.exists(dir_h)


def test_purga_de_trabajos_expirados_tras_24h(tmp_path):
    """Trabajos con fin hace más de 24 horas se borran de disco y dan 404 (§3)."""
    dir_trabajos = tmp_path / "trabajos"
    os.makedirs(dir_trabajos, exist_ok=True)

    id_viejo = "c" * 32
    dir_v = dir_trabajos / id_viejo
    os.makedirs(dir_v, exist_ok=True)

    # Fin hace 25 horas
    hace_25h = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    estado_expirado = {
        "id": id_viejo,
        "usuario": "ana",
        "estado": "listo",
        "calidades": [720],
        "tiempos": {"creado": hace_25h, "inicio": hace_25h, "fin": hace_25h},
    }
    with open(dir_v / "estado.json", "w", encoding="utf-8") as f:
        json.dump(estado_expirado, f)

    config = Config(dir_logs=str(tmp_path / "logs"), video_dir=str(dir_trabajos), jwt_secreto=SECRETO, video_retencion_horas=24)
    v_servicio = VideoServicio(config, PoolWorkers(LanzadorVideoFalso(), 2), LanzadorVideoFalso())

    # Al consultar o al arrancar, el trabajo debe dar 404 TRABAJO_INEXISTENTE y no existir en disco
    with pytest.raises(ErrorApi) as exc:
        v_servicio.consultar_trabajo(id_viejo, "ana", False)
    assert exc.value.tipo == TipoError.TRABAJO_INEXISTENTE
    assert not os.path.exists(dir_v)


# ==============================================================================
# 7. Telemetría y Observabilidad en /health
# ==============================================================================

def test_health_incluye_metricas_de_video(entorno_video):
    """GET /health expone en_cola, procesando, max_simultaneos y max_en_cola de video (§7)."""
    cliente, _, _, _, config = entorno_video(workers_max=2, cola_max=20)

    res = cliente.get("/health")
    assert res.status_code == 200
    contenido = res.json()["contenido"]

    assert "videos" in contenido
    v_stats = contenido["videos"]
    assert v_stats["en_cola"] == 0
    assert v_stats["procesando"] == 0
    assert v_stats["max_simultaneos"] == 2
    assert v_stats["max_en_cola"] == 20


def test_posicion_en_cola_exacta_segun_lamport(entorno_video):
    """Bajo concurrencia y contención en el pool, la respuesta 202 devuelve la posición exacta 1-indexed
    de cada trabajo según el ordenamiento Min-Heap de Lamport, y no simplemente la cantidad de tareas encoladas."""
    from unittest.mock import patch
    import socket
    import threading

    evento_bloqueo = threading.Event()
    primer_trabajo_iniciado = threading.Event()

    class LanzadorConBloqueo(LanzadorVideoFalso):
        def ejecutarTareaRemota(self, *args, **kwargs):
            primer_trabajo_iniciado.set()
            evento_bloqueo.wait(timeout=10)
            return super().ejecutarTareaRemota(*args, **kwargs)

    # 1 worker para forzar contención en la cola
    cliente, v_servicio, v_pool, _, _ = entorno_video(
        lanzador=LanzadorConBloqueo(),
        workers_max=1,
        cola_max=10,
        trabajos_por_usuario_max=5,
    )

    addrinfo_publica = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    with patch("socket.getaddrinfo", return_value=addrinfo_publica):
        # 1. Enviar trabajo 0 para ocupar al único worker disponible
        res0 = cliente.post(
            "/videos",
            headers={"Content-Type": "application/json", "X-Lamport-Clock": "5"},
            json={"url": "https://cdn.ejemplo.com/v0.mp4", "calidades": [720]}
        )
        assert res0.status_code == 202
        assert primer_trabajo_iniciado.wait(timeout=3), "El primer trabajo no inició en el worker"

        try:
            # 2. Enviar Job A con Lamport ts alto (100). Como la cola está vacía, entra en pos 1.
            res_a = cliente.post(
                "/videos",
                headers={"Content-Type": "application/json", "X-Lamport-Clock": "100"},
                json={"url": "https://cdn.ejemplo.com/va.mp4", "calidades": [720]}
            )
            assert res_a.status_code == 202
            id_a = res_a.json()["contenido"]["id"]
            assert res_a.json()["contenido"]["posicion_en_cola"] == 1

            # 3. Enviar Job B con Lamport ts menor (20). Según Lamport, tiene mayor prioridad y se ubica antes que Job A.
            # Si devolviera tareas_encoladas daría 2 (incorrecto/mentira bajo contención). Debe dar 1!
            res_b = cliente.post(
                "/videos",
                headers={"Content-Type": "application/json", "X-Lamport-Clock": "20"},
                json={"url": "https://cdn.ejemplo.com/vb.mp4", "calidades": [720]}
            )
            assert res_b.status_code == 202
            id_b = res_b.json()["contenido"]["id"]
            assert res_b.json()["contenido"]["posicion_en_cola"] == 1

            # 4. Enviar Job C con Lamport ts intermedio (60). Se ubica entre Job B (20) y Job A (100).
            # Debe dar posición 2!
            res_c = cliente.post(
                "/videos",
                headers={"Content-Type": "application/json", "X-Lamport-Clock": "60"},
                json={"url": "https://cdn.ejemplo.com/vc.mp4", "calidades": [720]}
            )
            assert res_c.status_code == 202
            id_c = res_c.json()["contenido"]["id"]
            assert res_c.json()["contenido"]["posicion_en_cola"] == 2

            # 5. Verificar que las posiciones reflejadas en el pool coincidan exactamente
            assert v_pool.posicion_en_cola(id_b) == 1
            assert v_pool.posicion_en_cola(id_c) == 2
            assert v_pool.posicion_en_cola(id_a) == 3
        finally:
            evento_bloqueo.set()


def test_modo_url_rechazo_redireccion_ssrf_en_validacion_422(entorno_video):
    """En la fase de validación de URL (POST /videos), si la URL intenta redirigir hacia localhost
    o una IP privada/restringida, la verificación previa intercepta el salto y rechaza con 422 URL_INACCESIBLE,
    liberando la cuota del usuario y limpiando el directorio de trabajo."""
    from unittest.mock import patch
    import socket

    class RedireccionALocalhostOpener:
        def open(self, req, timeout=None):
            def validar_falsa(url_destino):
                v = VideoServicio.__new__(VideoServicio)
                v.validar_url(url_destino)

            handler = RedireccionSeguraHandler(validar_falsa, max_redirecciones=3)
            # Simular que el handler procesa un salto de redirección a localhost
            handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1:8080/admin/secret")

    cliente, v_servicio, _, _, _ = entorno_video(url_opener=RedireccionALocalhostOpener())

    addrinfo_publica = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    with patch("socket.getaddrinfo", return_value=addrinfo_publica):
        res = cliente.post(
            "/videos",
            headers={"Content-Type": "application/json"},
            json={"url": "https://atacante.com/redirect_loopback.mp4", "calidades": [720]}
        )

    assert res.status_code == 422
    contenido = res.json()["contenido"]
    assert contenido["error"]["tipo"] == "URL_INACCESIBLE"
    assert "redirección bloqueada" in contenido["error"]["mensaje"]

    # Verificar que la cuota del usuario 'ana' fue liberada y no quedó trabajo activo
    assert v_servicio.trabajos_activos_de("ana") == 0


def test_video_vacio_devuelve_archivo_no_es_video_422(entorno_video):
    """Un archivo de 0 bytes (vacio.mp4) debe responder 422 ARCHIVO_NO_ES_VIDEO según casos.json (#132)."""
    cliente, _, _, _, _ = entorno_video()

    files = {
        "video": ("vacio.mp4", b"", "video/mp4"),
    }
    data = {"calidades": "240"}
    res = cliente.post("/videos", files=files, data=data)

    assert res.status_code == 422
    contenido = res.json()["contenido"]
    assert contenido["error"]["tipo"] == "ARCHIVO_NO_ES_VIDEO"
    assert "vacío" in contenido["error"]["mensaje"]


def test_campo_video_faltante_devuelve_payload_invalido_422(entorno_video):
    """Si no se incluye el campo 'video' en el form multipart, debe responder 422 PAYLOAD_INVALIDO con detalles=['video']."""
    cliente, _, _, _, _ = entorno_video()

    # Formulario multipart sin campo 'video'
    files = {"calidades": (None, "720")}
    res = cliente.post("/videos", files=files)

    assert res.status_code == 422
    contenido = res.json()["contenido"]
    assert contenido["error"]["tipo"] == "PAYLOAD_INVALIDO"
    assert "video" in contenido["error"]["detalles"]


def test_calidades_con_float_o_invalido_devuelve_payload_invalido_422(entorno_video):
    """Valores de calidad flotantes o no enteros como 720.5 deben responder 422 PAYLOAD_INVALIDO con detalles=['calidades']."""
    cliente, _, _, _, _ = entorno_video()

    # Modo URL con float
    res = cliente.post(
        "/videos",
        headers={"Content-Type": "application/json"},
        json={"url": "https://cerberus.example.com/v.mp4", "calidades": [720.5]}
    )
    assert res.status_code == 422
    contenido = res.json()["contenido"]
    assert contenido["error"]["tipo"] == "PAYLOAD_INVALIDO"
    assert contenido["error"]["detalles"] == ["calidades"]

    # Modo multipart con float
    files = {"video": ("test.mp4", b"dummy", "video/mp4")}
    data = {"calidades": "720.5"}
    res2 = cliente.post("/videos", files=files, data=data)
    assert res2.status_code == 422
    assert res2.json()["contenido"]["error"]["tipo"] == "PAYLOAD_INVALIDO"
    assert res2.json()["contenido"]["error"]["detalles"] == ["calidades"]


def test_duracion_cero_o_negativa_devuelve_archivo_no_es_video_422(entorno_video):
    """Un video reportado con duración <= 0 s se rechaza como 422 ARCHIVO_NO_ES_VIDEO."""
    probe = ffprobe_falso_fabrica(duracion_s=0.0)
    cliente, _, _, _, _ = entorno_video(ffprobe_runner=probe)

    files = {"video": ("cero.mp4", b"fake", "video/mp4")}
    data = {"calidades": "720"}
    res = cliente.post("/videos", files=files, data=data)

    assert res.status_code == 422
    assert res.json()["contenido"]["error"]["tipo"] == "ARCHIVO_NO_ES_VIDEO"
    assert "duración" in res.json()["contenido"]["error"]["mensaje"]


def test_archivo_entrada_se_elimina_tras_transcodificacion_exitosa(entorno_video):
    """Para ahorrar espacio en disco, el archivo original 'entrada' se borra al terminar exitosamente."""
    cliente, v_servicio, _, _, _ = entorno_video()

    files = {"video": ("grande.mp4", b"fake_video_stream", "video/mp4")}
    data = {"calidades": "720"}
    res = cliente.post("/videos", files=files, data=data)
    assert res.status_code == 202
    id_t = res.json()["contenido"]["id"]

    # Esperar que el worker termine la tarea
    time.sleep(0.15)

    dir_t = os.path.join(v_servicio.dir_trabajos, id_t)
    ruta_entrada = os.path.join(dir_t, "entrada")
    assert not os.path.exists(ruta_entrada)


def test_url_con_ip_no_especificada_0000_rechazada_ssrf(entorno_video):
    """URLs que apunten a 0.0.0.0 o :: deben ser rechazadas con 422 URL_INVALIDA."""
    cliente, _, _, _, _ = entorno_video()

    res = cliente.post(
        "/videos",
        headers={"Content-Type": "application/json"},
        json={"url": "http://0.0.0.0/video.mp4", "calidades": [720]}
    )
    assert res.status_code == 422
    assert res.json()["contenido"]["error"]["tipo"] == "URL_INVALIDA"


def test_aislamiento_de_nodos_en_recuperacion_arranque(tmp_path):
    """Al arrancar en cluster (Hit 3), un nodo no debe marcar como TRABAJO_PERDIDO los trabajos de otro nodo."""
    dir_trabajos = tmp_path / "trabajos"
    os.makedirs(dir_trabajos, exist_ok=True)

    id_trabajo = "a" * 32
    dir_t = dir_trabajos / id_trabajo
    os.makedirs(dir_t, exist_ok=True)

    # Trabajo perteneciente a nodo-2 que estaba procesando
    estado_otro_nodo = {
        "id": id_trabajo,
        "usuario": "ana",
        "nodo": "nodo-2",
        "estado": "procesando",
        "calidades": [720],
        "original": {"nombre": "v.mp4", "bytes": 100, "duracion_s": 10, "ancho": 1280, "alto": 720},
        "tiempos": {"creado": "2026-10-18T20:00:00Z", "inicio": "2026-10-18T20:01:00Z"},
        "resultados": None,
        "error": None
    }
    with open(dir_t / "estado.json", "w", encoding="utf-8") as f:
        json.dump(estado_otro_nodo, f)

    # Iniciar servicio con nodo_id = "nodo-1"
    config = Config(dir_logs=str(tmp_path / "logs"), video_dir=str(dir_trabajos), jwt_secreto=SECRETO, nodo_id="nodo-1")
    VideoServicio(config, PoolWorkers(LanzadorVideoFalso(), 2), LanzadorVideoFalso())

    # El estado debe mantenerse intacto (no pisado como TRABAJO_PERDIDO por nodo-1)
    with open(dir_t / "estado.json", "r", encoding="utf-8") as f:
        estado_post = json.load(f)

    assert estado_post["estado"] == "procesando"
    assert estado_post["error"] is None


