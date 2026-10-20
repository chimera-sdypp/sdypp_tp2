#!/usr/bin/env bash
# Banco de videos de prueba para la transcodificación (contrato-video.md): un archivo por
# caso, feliz o no, y casos.json con lo que el contrato dice que tiene que devolver cada uno.
# Lo usan la batería de pruebas y los tests del servidor.
#
#   evidencias/generar_videos_prueba.sh [carpeta]      (por defecto videos/, que no se versiona)
#   FORZAR=1 evidencias/generar_videos_prueba.sh       (vuelve a generar los que ya existen)
#
# Todo es sintético (testsrc2 de ffmpeg): no hace falta bajar nada y el banco es el mismo en
# cualquier máquina. Usa el ffmpeg del host y, si no hay, el de la imagen tarea-video.
# Los videos reales de 10 min (bbb_240p … bbb_2160p) no se generan: se usan tal cual.
set -euo pipefail
export LC_NUMERIC=C   # ffprobe devuelve punto decimal; printf con locale en español espera coma

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "${1:-$RAIZ/videos}"
DESTINO="$(cd "${1:-$RAIZ/videos}" && pwd)"
IMAGEN="cerberusdistribuido/tarea-video:1.0.0"
cd "$DESTINO"

# Con el ffmpeg de la imagen, la carpeta se monta en /v: por eso todo usa rutas relativas.
if command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null; then
    ff() { ffmpeg -hide_banner -nostdin -v error -y "$@"; }
    fp() { ffprobe "$@"; }
else
    echo "no hay ffmpeg en el host: uso el de $IMAGEN" >&2
    en_imagen() { docker run --rm --user "$(id -u):$(id -g)" -v "$DESTINO:/v" -w /v --entrypoint "$@"; }
    ff() { en_imagen ffmpeg "$IMAGEN" -hide_banner -nostdin -v error -y "$@"; }
    fp() { en_imagen ffprobe "$IMAGEN" "$@"; }
fi

# caso <archivo> <comando…>: el comando escribe en $SALIDA, un temporal que después se renombra.
# Así un Ctrl-C a mitad de camino no deja un archivo roto que parezca un caso válido. Si el
# archivo ya existe no se regenera, salvo con FORZAR=1.
caso() {
    local archivo="$1"; shift
    if [[ -e "$archivo" && -z "${FORZAR:-}" ]]; then
        echo "  ya está    $archivo"
        return
    fi
    echo "  generando  $archivo"
    SALIDA="$archivo.parcial"
    trap 'rm -f -- "$SALIDA" "$SALIDA.completo"' EXIT   # si falla, no quedan temporales
    "$@"
    mv "$SALIDA" "$archivo"
}

# sintetico <ancho>x<alto> <segundos> <formato> [opciones…]: barras de testsrc2 con un tono.
# Las opciones van al final, así pueden reemplazar a las de acá (p. ej. otro códec).
sintetico() {
    local tamano="$1" segundos="$2" formato="$3"; shift 3
    ff -f lavfi -i "testsrc2=size=$tamano:rate=25:duration=$segundos" \
       -f lavfi -i "sine=frequency=440:duration=$segundos" \
       -c:v libx264 -preset ultrafast -pix_fmt yuv420p -c:a aac -b:a 128k "$@" -f "$formato" "$SALIDA"
}

sin_audio() { ff -f lavfi -i "testsrc2=size=$1:rate=25:duration=$2" -c:v libx264 -preset ultrafast -pix_fmt yuv420p -f mp4 "$SALIDA"; }
solo_audio() { ff -f lavfi -i "sine=frequency=440:duration=5" -c:a aac -f mp4 "$SALIDA"; }

# cortado <movflags> (0 = ninguno): un mp4 de 10 s cortado a la mitad. Con +faststart el índice (moov) queda
# al principio y ffprobe lo lee entero: el corte recién aparece al transcodificar. Sin él, el
# moov va al final, se pierde con el corte, y el archivo ya no se puede leer como video.
cortado() {
    local completo="$SALIDA.completo"
    ff -f lavfi -i "testsrc2=size=640x360:rate=25:duration=10" -c:v libx264 -preset ultrafast \
       -pix_fmt yuv420p -movflags "$1" -f mp4 "$completo"
    head -c "$(( $(stat -c %s "$completo") / 2 ))" "$completo" > "$SALIDA"
    rm -f "$completo"
}

texto()     { printf 'Esto es un archivo de texto con extensión .mp4: no es un video.\n' > "$SALIDA"; }
aleatorio() { head -c 1M /dev/urandom > "$SALIDA"; }
vacio()     { : > "$SALIDA"; }
# Disperso: ocupa 0 bytes en disco pero mide 1100 MiB. El contrato lo rechaza por el
# Content-Length antes de leer el contenido, así que no hace falta que sea un video.
grande()    { truncate -s 1100M "$SALIDA"; }

echo "Banco de videos en $DESTINO"

# Felices: 202 y después "listo".
caso chico_360p.mp4          sintetico 640x360 5 mp4 -b:v 1500k                     # ~1 MB
caso mediano_1080p.mp4       sintetico 1920x1080 60 mp4 -vf noise=alls=12:allf=t -b:v 6500k  # ~50 MB
caso 4x3_480p.mp4            sintetico 640x480 10 mp4
caso vertical_720x1280.mp4   sintetico 720x1280 10 mp4
caso sin_audio_360p.mp4      sin_audio 640x360 5
caso vp9_360p.webm           sintetico 640x360 5 webm -c:v libvpx-vp9 -deadline realtime -c:a libopus
caso limite_899s.mp4         sintetico 320x240 899 mp4
# Rechazados en el POST.
caso largo_960s.mp4          sintetico 320x240 960 mp4
caso 8k_7680x4320.mp4        sintetico 7680x4320 1 mp4
caso dci4k_4096x2160.mp4     sintetico 4096x2160 1 mp4
caso solo_audio.m4a          solo_audio
caso no_es_video.mp4         texto
caso aleatorio.mp4           aleatorio
caso vacio.mp4               vacio
caso sin_moov.mp4            cortado 0
caso grande_1100MiB.mp4      grande
# Aceptado, pero el trabajo termina en error.
caso truncado_faststart.mp4  cortado +faststart

# Lo esperado sale del contrato (§3 y §6). Si cambia el contrato, cambia esto.
cat > casos.json <<'EOF'
[
  {"archivo": "chico_360p.mp4", "calidades": [360, 240], "esperado": {"codigo": 202, "estado": "listo"},
   "que_prueba": "Video chico (~1 MB): el flujo completo en segundos"},
  {"archivo": "chico_360p.mp4", "calidades": [480], "esperado": {"codigo": 422, "tipo": "CALIDAD_SUPERIOR_AL_ORIGINAL"},
   "que_prueba": "Pedir una calidad mayor que el original: no se agranda"},
  {"archivo": "mediano_1080p.mp4", "calidades": [1080, 720, 480, 360, 240], "esperado": {"codigo": 202, "estado": "listo"},
   "que_prueba": "~50 MB a las 5 calidades a la vez"},
  {"archivo": "4x3_480p.mp4", "calidades": [360], "esperado": {"codigo": 202, "estado": "listo", "resolucion": {"360": [480, 360]}},
   "que_prueba": "Se respeta la proporción 4:3"},
  {"archivo": "vertical_720x1280.mp4", "calidades": [720], "esperado": {"codigo": 202, "estado": "listo", "resolucion": {"720": [406, 720]}},
   "que_prueba": "Video vertical: la calidad es la altura"},
  {"archivo": "sin_audio_360p.mp4", "calidades": [240], "esperado": {"codigo": 202, "estado": "listo"},
   "que_prueba": "Un original sin audio no es un error"},
  {"archivo": "vp9_360p.webm", "calidades": [240], "esperado": {"codigo": 202, "estado": "listo"},
   "que_prueba": "Otro contenedor y otro códec (WebM con VP9 y Opus)"},
  {"archivo": "limite_899s.mp4", "calidades": [240], "esperado": {"codigo": 202, "estado": "listo"},
   "que_prueba": "Justo debajo del límite de duración (15 min = 900 s)"},
  {"archivo": "largo_960s.mp4", "calidades": [240], "esperado": {"codigo": 422, "tipo": "VIDEO_DEMASIADO_LARGO"},
   "que_prueba": "Más de 15 min"},
  {"archivo": "8k_7680x4320.mp4", "calidades": [240], "esperado": {"codigo": 422, "tipo": "RESOLUCION_NO_SOPORTADA"},
   "que_prueba": "Entrada mayor que 4K"},
  {"archivo": "dci4k_4096x2160.mp4", "calidades": [240], "esperado": {"codigo": 422, "tipo": "RESOLUCION_NO_SOPORTADA"},
   "que_prueba": "4K de cine (4096 de ancho): supera los 3840 del límite"},
  {"archivo": "solo_audio.m4a", "calidades": [240], "esperado": {"codigo": 422, "tipo": "ARCHIVO_NO_ES_VIDEO"},
   "que_prueba": "Un archivo multimedia sin pista de video"},
  {"archivo": "no_es_video.mp4", "calidades": [240], "esperado": {"codigo": 422, "tipo": "ARCHIVO_NO_ES_VIDEO"},
   "que_prueba": "Un texto con extensión .mp4"},
  {"archivo": "aleatorio.mp4", "calidades": [240], "esperado": {"codigo": 422, "tipo": "ARCHIVO_NO_ES_VIDEO"},
   "que_prueba": "1 MB de bytes al azar"},
  {"archivo": "vacio.mp4", "calidades": [240], "esperado": {"codigo": 422, "tipo": "ARCHIVO_NO_ES_VIDEO"},
   "que_prueba": "Archivo de 0 bytes"},
  {"archivo": "sin_moov.mp4", "calidades": [240], "esperado": {"codigo": 422, "tipo": "ARCHIVO_NO_ES_VIDEO"},
   "que_prueba": "mp4 cortado a la mitad que perdió el índice: no se puede leer"},
  {"archivo": "grande_1100MiB.mp4", "calidades": [240], "esperado": {"codigo": 413, "tipo": "VIDEO_DEMASIADO_GRANDE"},
   "que_prueba": "Más de 1 GiB: el rechazo tiene que llegar antes de terminar la subida"},
  {"archivo": "truncado_faststart.mp4", "calidades": [240], "esperado": {"codigo": 202, "estado": "error", "tipo": "TRANSCODIFICACION_FALLIDA"},
   "que_prueba": "mp4 cortado a la mitad con el índice al principio: se lee, pero falta la mitad del video"}
]
EOF

echo
echo "| Archivo | Tamaño | Video |"
echo "|---|---|---|"
for archivo in $(python3 -c 'import json; print("\n".join(dict.fromkeys(c["archivo"] for c in json.load(open("casos.json")))))'); do
    bytes=$(stat -c %s "$archivo")
    info=$(fp -v error -select_streams v:0 -show_entries stream=codec_name,width,height:format=duration \
               -of default=nw=1:nk=1 "$archivo" 2>/dev/null | paste -sd' ' || true)
    read -r codec ancho alto duracion <<< "$info"
    if [[ -n "${alto:-}" ]]; then
        video="$codec ${ancho}x${alto}, $(printf '%.1f' "$duracion") s"
    else
        video="no se puede leer como video"
    fi
    echo "| \`$archivo\` | $(numfmt --to=iec-i --suffix=B "$bytes") | $video |"
done
echo
echo "Lo esperado de cada caso está en $DESTINO/casos.json"
