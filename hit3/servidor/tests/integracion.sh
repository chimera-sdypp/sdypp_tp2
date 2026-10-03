#!/usr/bin/env bash
# Levanta el cluster del Hit 3 (3 nodos + nginx) desde cero con la tarea de prueba,
# corre los tests de integración contra Docker real (incluidas las caídas del
# coordinador y de un ejecutor), prueba el cliente con el servicio tarea real y
# baja todo, salga bien o mal.
#
#   hit3/servidor/tests/integracion.sh            (desde cualquier directorio)
#   PYTHON=.venv/bin/python hit3/servidor/tests/integracion.sh
set -euo pipefail

AQUI="$(cd "$(dirname "$0")" && pwd)"
HIT3="$(cd "$AQUI/../.." && pwd)"
PUERTO="${TP2_PUERTO:-8080}"
PUERTO_NODO1="${TP2_PUERTO_NODO1:-18081}"
PYTHON="${PYTHON:-python3}"
ENTORNO="$(mktemp)"
COMPOSE=(docker compose -f "$HIT3/docker-compose.yml" -f "$AQUI/docker-compose.test.yml" --env-file "$ENTORNO")

bajar() {
    if [ "${1:-}" = "fallo" ]; then
        "${COMPOSE[@]}" logs --no-color --tail 60 || true
    fi
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -f "$ENTORNO"
}
trap 'bajar fallo' ERR
trap bajar EXIT

cat > "$ENTORNO" <<FIN
DOCKER_GID=$(stat -c %g /var/run/docker.sock)
TP2_IMAGENES_PERMITIDAS=cerberusdistribuido/tarea-prueba,cerberusdistribuido/tarea
TP2_PUERTO=$PUERTO
TP2_PUERTO_NODO1=$PUERTO_NODO1
TP2_TIMEOUT_EJECUCION=4
TP2_REGISTRY_USUARIO=
TP2_REGISTRY_TOKEN=
FIN

echo "==> imágenes de la tarea de prueba y de la tarea real"
docker build -q -t cerberusdistribuido/tarea-prueba:test "$AQUI/tarea_prueba" >/dev/null
docker build -q -t cerberusdistribuido/tarea:test "$HIT3/tarea" >/dev/null
echo "==> cluster (build + up, espera a healthy)"
"${COMPOSE[@]}" up --build -d --wait
echo "==> tests de integración"
cd "$AQUI/.."
TP2_URL="http://127.0.0.1:$PUERTO" TP2_URL_NODO1="http://127.0.0.1:$PUERTO_NODO1" \
    "$PYTHON" -m pytest -m integracion -v -s -p no:cacheprovider
echo "==> cliente contra el servicio tarea real"
"$PYTHON" "$HIT3/cliente/cliente.py" division '{"a": 7, "b": 2}' --imagen cerberusdistribuido/tarea:test \
    --servidor "http://127.0.0.1:$PUERTO"
