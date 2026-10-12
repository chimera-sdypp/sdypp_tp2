#!/usr/bin/env bash
# R2 — La imagen llega por parámetro y el servidor la valida antes de ejecutarla: bien
# formada, en la lista blanca, existente y con el digest fijado. Cada caso inválido
# devuelve su código y un mensaje claro, sin 500 genéricos ni contenedores colgados.
#
#   evidencias/r2_imagenes.sh [hit1|hit2|hit3]        (por defecto hit1)
#
# Levanta el hit con esta lista blanca: la tarea real fijada a su digest de Docker Hub y
# una imagen que no es servicio tarea (termina enseguida). Imprime una tabla en Markdown.
# El usuario y el token de sólo lectura salen de hitN/.env, o del archivo que diga TP2_ENV.
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
HIT="${1:-hit1}"
PUERTO="${TP2_PUERTO:-18090}"
URL="http://127.0.0.1:$PUERTO"
REPO="cerberusdistribuido/tarea"
ENTORNO="$(mktemp)"
CONFIG="$(mktemp -d)"
RESPUESTA="$(mktemp)"
COMPOSE=(docker compose -f "$RAIZ/$HIT/docker-compose.yml" --env-file "$ENTORNO")

bajar() {
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    docker image rm "$REPO:otra" cerberusdistribuido/no-es-tarea:1.0 >/dev/null 2>&1 || true
    rm -rf "$ENTORNO" "$CONFIG" "$RESPUESTA"
}
trap bajar EXIT

ARCHIVO="${TP2_ENV:-$RAIZ/$HIT/.env}"
leer() { grep -m1 "^$1=" "$ARCHIVO" 2>/dev/null | cut -d= -f2- || true; }
USUARIO="$(leer TP2_REGISTRY_USUARIO)"
TOKEN="$(leer TP2_REGISTRY_TOKEN)"
if [ -z "$USUARIO" ] || [ -z "$TOKEN" ]; then
    echo "Falta TP2_REGISTRY_USUARIO o TP2_REGISTRY_TOKEN en $ARCHIVO (otro archivo: TP2_ENV=...)" >&2
    exit 1
fi

# El digest real de la tarea publicada: se baja con el token, en un DOCKER_CONFIG aparte.
echo "$TOKEN" | DOCKER_CONFIG="$CONFIG" docker login -u "$USUARIO" --password-stdin >/dev/null 2>&1
DOCKER_CONFIG="$CONFIG" docker pull -q "$REPO:1.0.0" >/dev/null
DIGEST="$(docker image inspect "$REPO:1.0.0" --format '{{range .RepoDigests}}{{println .}}{{end}}' \
    | grep -m1 "^$REPO@" | cut -d@ -f2)"

# Otro contenido con el mismo nombre de repositorio, y una imagen que no es servicio tarea.
docker build -q -t "$REPO:otra" "$RAIZ/$HIT/servidor/tests/tarea_prueba" >/dev/null 2>&1
docker build -q -t cerberusdistribuido/no-es-tarea:1.0 - >/dev/null 2>&1 <<'FIN'
FROM python:3.14-slim
CMD ["python", "-c", "raise SystemExit(3)"]
FIN

PERMITIDAS="$REPO@$DIGEST,cerberusdistribuido/no-es-tarea"
cat > "$ENTORNO" <<FIN
DOCKER_GID=$(stat -c %g /var/run/docker.sock)
TP2_IMAGENES_PERMITIDAS=$PERMITIDAS
TP2_PUERTO=$PUERTO
TP2_REGISTRY_USUARIO=$USUARIO
TP2_REGISTRY_TOKEN=$TOKEN
FIN
"${COMPOSE[@]}" up --build -d --wait >/dev/null 2>&1

caso() {  # $1 = descripción, $2 = imagen
    local cuerpo medicion
    cuerpo="$(python3 -I -c 'import json, sys
print(json.dumps({"calculo": "suma", "parametros": {"a": 3, "b": 4}, "imagen": sys.argv[1]}))' "$2")"
    medicion="$(curl -s -o "$RESPUESTA" -w '%{http_code} %{time_total}' -X POST "$URL/getRemoteTask" \
        -H 'Content-Type: application/json' -d "$cuerpo")"
    python3 -I - "$1" "$2" $medicion "$RESPUESTA" <<'PY'
import json, re, sys
caso, imagen, codigo, tiempo, ruta = sys.argv[1:]
corto = lambda texto: re.sub(r"sha256:([0-9a-f]{12})[0-9a-f]{52}", r"sha256:\1…", texto)
contenido = json.load(open(ruta, encoding="utf-8"))["contenido"]
error = contenido.get("error")
if error:
    tipo, mensaje = error["tipo"], error["mensaje"]
    if error["detalles"]:
        mensaje += ": " + "; ".join(d["problema"] for d in error["detalles"])
else:
    tipo, mensaje = "—", f"resultado {contenido['resultado']}"
print(f"| {caso} | `{corto(imagen)}` | {codigo} | `{tipo}` | {corto(mensaje)} | {float(tiempo):.2f} s |")
PY
}

echo "Servidor: $HIT. TP2_IMAGENES_PERMITIDAS=${PERMITIDAS//$DIGEST/${DIGEST:0:19}…}"
echo
echo "| Caso | \`imagen\` | HTTP | \`tipo\` | Mensaje | Tiempo |"
echo "|---|---|---|---|---|---|"
caso "Válida, por tag" "$REPO:1.0.0"
caso "Válida, por digest" "$REPO@$DIGEST"
caso "Mal formada: mayúsculas" "Cerberus/Tarea:1.0.0"
caso "Mal formada: sin versión" "$REPO"
caso "Mal formada: \`latest\`" "$REPO:latest"
caso "Mal formada: otro comando" "$REPO:1.0.0; rm -rf /"
caso "No permitida" "alpine:3.20"
caso "Inexistente: tag" "$REPO:9.9.9"
caso "Inexistente: digest" "$REPO@sha256:$(printf '0%.0s' {1..64})"
caso "Otro contenido que el digest fijado" "$REPO:otra"
caso "No es un servicio tarea" "cerberusdistribuido/no-es-tarea:1.0"
echo
quedaron="$(docker ps -aq --filter ancestor="$REPO:1.0.0" --filter ancestor=cerberusdistribuido/no-es-tarea:1.0 | wc -l)"
echo "Contenedores tarea que quedaron después de los 11 pedidos: $quedaron"
