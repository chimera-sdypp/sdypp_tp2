#!/usr/bin/env bash
# Levanta el servidor del Hit 2 desde cero con la tarea de prueba, corre los
# tests de integración contra Docker real, prueba el cliente con el servicio
# tarea real y baja todo, salga bien o mal.
#
#   hit2/servidor/tests/integracion.sh            (desde cualquier directorio)
#   PYTHON=.venv/bin/python hit2/servidor/tests/integracion.sh
set -euo pipefail

AQUI="$(cd "$(dirname "$0")" && pwd)"
HIT2="$(cd "$AQUI/../.." && pwd)"
PUERTO="${TP2_PUERTO:-8082}"
PYTHON="${PYTHON:-python3}"
ENTORNO="$(mktemp)"
COMPOSE=(docker compose -f "$HIT2/docker-compose.yml" --env-file "$ENTORNO")

bajar() {
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -f "$ENTORNO"
}
trap bajar EXIT

cat > "$ENTORNO" <<FIN
DOCKER_GID=$(stat -c %g /var/run/docker.sock)
TP2_IMAGENES_PERMITIDAS=cerberusdistribuido/tarea-prueba,cerberusdistribuido/tarea
TP2_PUERTO=$PUERTO
TP2_TIMEOUT_EJECUCION=4
TP2_WORKERS_MAX=4
TP2_REGISTRY_USUARIO=
TP2_REGISTRY_TOKEN=
FIN

echo "==> imágenes de la tarea de prueba y de la tarea real"
docker build -q -t cerberusdistribuido/tarea-prueba:test "$AQUI/tarea_prueba" >/dev/null
docker build -q -t cerberusdistribuido/tarea:test "$HIT2/tarea" >/dev/null
echo "==> servidor (build + up, espera a healthy)"
"${COMPOSE[@]}" up --build -d --wait
echo "==> tests de integración"
cd "$AQUI/.."
TP2_URL="http://127.0.0.1:$PUERTO" "$PYTHON" -m pytest -m integracion -v -p no:cacheprovider
echo "==> cliente contra el servicio tarea real"
"$PYTHON" "$HIT2/cliente/cliente.py" division '{"a": 7, "b": 2}' --imagen cerberusdistribuido/tarea:test \
    --servidor "http://127.0.0.1:$PUERTO"

