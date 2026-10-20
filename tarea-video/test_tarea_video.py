import json
import os
import shutil
import subprocess

import pytest

import tarea_video
from tarea_video import (ParametrosInvalidos, TranscodificacionFallida, _seguir_progreso, comando,
                         escribir_progreso, sondear, transcodificar, validar)

hay_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                reason="hace falta ffmpeg")


def generar(ruta, ancho, alto, segundos=2, audio=True, faststart=False):
    """Un video sintético (barras de testsrc y un tono), en el momento: el repo no lleva binarios."""
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
            "-i", f"testsrc=duration={segundos}:size={ancho}x{alto}:rate=25"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=duration={segundos}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if faststart:
        args += ["-movflags", "+faststart"]
    subprocess.run(args + ["-f", "mp4", str(ruta)], check=True)
    return ruta


def pedido(trabajo, calidades, **extra):
    return ({"calidades": calidades},
            {"entrada": str(trabajo / "entrada"), "salida": str(trabajo / "salida"),
             "progreso": str(trabajo / "progreso.json"), **extra})


@pytest.fixture
def trabajo(tmp_path):
    carpeta = tmp_path / "trabajos" / "t1"
    carpeta.mkdir(parents=True)
    return carpeta


@pytest.fixture
def raiz(trabajo):
    return trabajo.parent


# --- Validación (sin ffmpeg) ---

@pytest.mark.parametrize("calidades, mensaje", [
    (None, "lista no vacía"),
    ([], "lista no vacía"),
    ("720", "lista no vacía"),
    ([999], "calidad inválida"),
    (["720"], "calidad inválida"),
    ([True], "calidad inválida"),
    ([720, 720], "repetidos"),
])
def test_calidades_invalidas(trabajo, raiz, calidades, mensaje):
    (trabajo / "entrada").write_bytes(b"x")
    parametros, datos = pedido(trabajo, calidades)
    with pytest.raises(ParametrosInvalidos, match=mensaje):
        validar(parametros, datos, raiz)


@pytest.mark.parametrize("clave, valor", [
    ("entrada", "/etc/passwd"),
    ("salida", "/tmp"),
    ("progreso", "{raiz}/../fuera.json"),
])
def test_rutas_fuera_del_volumen(trabajo, raiz, clave, valor):
    (trabajo / "entrada").write_bytes(b"x")
    parametros, datos = pedido(trabajo, [720])
    datos[clave] = valor.format(raiz=raiz)
    with pytest.raises(ParametrosInvalidos, match="dentro de"):
        validar(parametros, datos, raiz)


def test_entrada_inexistente(trabajo, raiz):
    with pytest.raises(ParametrosInvalidos, match="no existe la entrada"):
        validar(*pedido(trabajo, [720]), raiz)


@pytest.mark.parametrize("duracion", [0, -3, "10", True])
def test_duracion_invalida(trabajo, raiz, duracion):
    (trabajo / "entrada").write_bytes(b"x")
    with pytest.raises(ParametrosInvalidos, match="duracion_s"):
        validar(*pedido(trabajo, [720], duracion_s=duracion), raiz)


def test_un_solo_ffmpeg_con_una_salida_por_calidad(tmp_path):
    args = comando(tmp_path / "entrada", tmp_path / "salida", [720, 240])
    assert args.count("ffmpeg") == 1 and args.count("-i") == 1
    assert args[-1].endswith("240.mp4") and str(tmp_path / "salida" / "720.mp4") in args
    assert "scale=-2:720" in args and "scale=-2:240" in args
    assert args.count("0:a:0?") == 2


def test_progreso_como_mucho_una_vez_por_segundo(tmp_path, monkeypatch):
    escritos = []
    monkeypatch.setattr(tarea_video, "escribir_progreso", lambda _, p: escritos.append(p))
    reloj = iter([0, 0.5, 1.0, 2.0])
    lineas = ["out_time_us=1000000\n", "out_time_us=2000000\n", "out_time_us=5000000\n",
              "out_time_us=N/A\n", "out_time_us=12000000\n", "progress=end\n"]
    _seguir_progreso(iter(lineas), tmp_path / "progreso.json", 10, reloj=lambda: next(reloj))
    # 0 s → 10 %, 0,5 s se saltea, 1 s → 50 %, N/A se ignora, 2 s → tope de 99,9 %, end → 100.
    assert escritos == [10.0, 50.0, 99.9, 100]


def test_progreso_se_escribe_entero(tmp_path):
    ruta = tmp_path / "progreso.json"
    escribir_progreso(ruta, 42.5)
    assert json.loads(ruta.read_text()) == {"progreso": 42.5}
    assert not (tmp_path / "progreso.json.tmp").exists()


def test_progreso_no_pasa_de_99_9_antes_de_terminar(tmp_path):
    ruta = tmp_path / "progreso.json"
    _seguir_progreso(iter(["out_time_us=30000000\n"]), ruta, 10)
    assert json.loads(ruta.read_text()) == {"progreso": 99.9}


# --- Con ffmpeg real ---

@hay_ffmpeg
def test_una_calidad(trabajo, raiz):
    generar(trabajo / "entrada", 1280, 720)
    resultado = transcodificar(*pedido(trabajo, [360]), raiz)
    assert resultado == {"360": {"archivo": "360.mp4", "bytes": (trabajo / "salida" / "360.mp4").stat().st_size,
                                 "ancho": 640, "alto": 360}}
    assert json.loads((trabajo / "progreso.json").read_text()) == {"progreso": 100}


@hay_ffmpeg
def test_dos_calidades_a_la_vez(trabajo, raiz):
    generar(trabajo / "entrada", 1280, 720)
    resultado = transcodificar(*pedido(trabajo, [720, 240], duracion_s=2), raiz)
    assert {c: (r["ancho"], r["alto"]) for c, r in resultado.items()} == {"720": (1280, 720),
                                                                         "240": (426, 240)}
    assert all(r["bytes"] > 0 for r in resultado.values())


@hay_ffmpeg
def test_respeta_la_proporcion_4_3(trabajo, raiz):
    generar(trabajo / "entrada", 640, 480)
    resultado = transcodificar(*pedido(trabajo, [360]), raiz)
    assert (resultado["360"]["ancho"], resultado["360"]["alto"]) == (480, 360)


@hay_ffmpeg
def test_sin_audio(trabajo, raiz):
    generar(trabajo / "entrada", 640, 360, audio=False)
    resultado = transcodificar(*pedido(trabajo, [240]), raiz)
    assert resultado["240"]["alto"] == 240


@hay_ffmpeg
def test_no_agranda(trabajo, raiz):
    generar(trabajo / "entrada", 640, 360)
    with pytest.raises(ParametrosInvalidos, match="mayor que el original"):
        transcodificar(*pedido(trabajo, [720, 240]), raiz)
    assert not (trabajo / "salida").exists()


@hay_ffmpeg
def test_no_es_video(trabajo, raiz):
    (trabajo / "entrada").write_text("esto no es un video")
    with pytest.raises(ParametrosInvalidos, match="no se puede leer como video"):
        transcodificar(*pedido(trabajo, [240]), raiz)


@hay_ffmpeg
def test_video_truncado(trabajo, raiz, tmp_path):
    """Un mp4 con el moov al principio cortado a la mitad: ffprobe lo lee entero y ffmpeg
    termina con 0, pero la salida dura la mitad. Tiene que ser un error, no un `listo`."""
    completo = generar(tmp_path / "completo.mp4", 640, 360, segundos=4, faststart=True)
    datos = completo.read_bytes()
    (trabajo / "entrada").write_bytes(datos[: len(datos) // 2])
    with pytest.raises(TranscodificacionFallida, match="el original está dañado"):
        transcodificar(*pedido(trabajo, [240, 360]), raiz)
    assert list((trabajo / "salida").iterdir()) == []


@pytest.mark.parametrize("obtenida, esperada, incompleta", [
    (634.5, 634.57, False),   # redondeo de frames
    (1.76, 4.0, True),
    (9.5, 10.0, False),       # menos de 1 s
    (590.0, 634.57, True),
    (None, 10.0, False),      # sin duración no se puede decidir: se acepta
])
def test_incompleta(obtenida, esperada, incompleta):
    assert tarea_video._incompleta(obtenida, esperada) is incompleta


@pytest.mark.parametrize("contenido, esperado", [
    ("200000 100000\n", 2),
    ("150000 100000\n", 2),     # 1,5 CPU → 2 hilos
    ("50000 100000\n", 1),
])
def test_hilos_segun_el_limite_de_docker(tmp_path, contenido, esperado):
    (tmp_path / "cpu.max").write_text(contenido)
    assert tarea_video.hilos(tmp_path / "cpu.max") == esperado


def test_hilos_sin_limite_usa_las_cpu_del_proceso(tmp_path):
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert tarea_video.hilos(tmp_path / "cpu.max") == os.process_cpu_count()
    assert tarea_video.hilos(tmp_path / "no-existe") == os.process_cpu_count()


def test_el_comando_lleva_los_hilos(tmp_path):
    args = comando(tmp_path / "entrada", tmp_path / "salida", [720, 240], n_hilos=2)
    assert args.count("-threads") == 3 and args[args.index("-threads") + 1] == "2"
