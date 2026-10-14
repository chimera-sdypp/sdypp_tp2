#!/usr/bin/env bash
# R1 — La imagen del servicio tarea es privada: el pull falla sin credencial y anda con la
# credencial configurada en el servidor.
#
#   1. Pull directo con Docker, sin credencial y con el token de sólo lectura. Usa un
#      DOCKER_CONFIG temporal: no usa ni toca el `docker login` del host.
#   2. El mismo pull hecho por el servidor del hit: se borra la imagen del host y se le
#      manda una tarea, primero sin token y después con el token en su secreto.
#
#   evidencias/r1_pull_privado.sh [hit1|hit2|hit3]        (por defecto hit1)
#
# El usuario y el token salen de hitN/.env (TP2_REGISTRY_USUARIO, TP2_REGISTRY_TOKEN), o del
# archivo que diga TP2_ENV.
# Levanta y baja el compose del hit, como su integracion.sh.
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
HIT="${1:-hit1}"
IMAGEN="cerberusdistribuido/tarea:1.0.0"
PUERTO="${TP2_PUERTO:-18090}"
ENTORNO="$(mktemp)"
CONFIG="$(mktemp -d)"
COMPOSE=(docker compose -f "$RAIZ/$HIT/docker-compose.yml" --env-file "$ENTORNO")

bajar() {
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$ENTORNO" "$CONFIG"
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

comando() { printf '\n$ %s\n' "$*"; }

echo "== 1. Pull directo de $IMAGEN"
comando "docker pull $IMAGEN                      # sin credencial"
if DOCKER_CONFIG="$CONFIG" docker pull -q "$IMAGEN" 2>&1; then
    echo "ERROR: el pull anónimo funcionó: la imagen no es privada" >&2
    exit 1
fi
comando "docker login -u $USUARIO --password-stdin   # token de sólo lectura"
echo "$TOKEN" | DOCKER_CONFIG="$CONFIG" docker login -u "$USUARIO" --password-stdin 2>&1 | grep 'Login Succeeded'
comando "docker pull $IMAGEN                      # con credencial"
DOCKER_CONFIG="$CONFIG" docker pull -q "$IMAGEN" 2>&1

# La tarea de prueba: el servidor tiene que bajar la imagen (no está en el host).
CUERPO="{\"calculo\": \"suma\", \"parametros\": {\"a\": 3, \"b\": 4}, \"imagen\": \"$IMAGEN\"}"

por_el_servidor() {  # $1 = token que se le configura al servidor ("" = ninguno)
    cat > "$ENTORNO" <<FIN
DOCKER_GID=$(stat -c %g /var/run/docker.sock)
TP2_IMAGENES_PERMITIDAS=cerberusdistribuido/tarea
TP2_PUERTO=$PUERTO
TP2_REGISTRY_USUARIO=$USUARIO
TP2_REGISTRY_TOKEN=$1
FIN
    "${COMPOSE[@]}" up --build -d --wait >/dev/null 2>&1
    comando "docker image rm $IMAGEN                  # que el servidor la tenga que bajar"
    docker image rm "$IMAGEN" 2>/dev/null || echo "(no estaba en el host)"
    comando "curl -s -X POST localhost:$PUERTO/getRemoteTask -H 'Content-Type: application/json' -d '$CUERPO'"
    curl -s -w '\nHTTP %{http_code}\n' -X POST "http://127.0.0.1:$PUERTO/getRemoteTask" \
        -H 'Content-Type: application/json' -d "$CUERPO"
    comando "docker compose logs | grep -E 'descargando|IMAGEN_INEXISTENTE'      # log del servidor"
    "${COMPOSE[@]}" logs --no-log-prefix 2>&1 | grep -E 'descargando|IMAGEN_INEXISTENTE' | cut -d'|' -f2-
    "${COMPOSE[@]}" down -v >/dev/null 2>&1
}

echo
echo "== 2. El mismo pull, hecho por el servidor del $HIT"
echo
echo "-- 2a. Servidor sin token (TP2_REGISTRY_TOKEN vacío)"
por_el_servidor ""
echo
echo "-- 2b. Servidor con el token de sólo lectura en su secreto (/run/secrets/registry_token)"
por_el_servidor "$TOKEN"
