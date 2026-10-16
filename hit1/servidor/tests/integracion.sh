#!/usr/bin/env bash
# Levanta el servidor del Hit 1 desde cero con la tarea de prueba, corre los
# tests de integración contra Docker real, prueba el cliente con el servicio
# tarea real y baja todo, salga bien o mal.
#
#   hit1/servidor/tests/integracion.sh            (desde cualquier directorio)
#   PYTHON=.venv/bin/python hit1/servidor/tests/integracion.sh
set -euo pipefail

AQUI="$(cd "$(dirname "$0")" && pwd)"
HIT1="$(cd "$AQUI/../.." && pwd)"
PUERTO="${TP2_PUERTO:-8080}"
PYTHON="${PYTHON:-python3}"
ENTORNO="$(mktemp)"
COMPOSE=(docker compose -f "$HIT1/docker-compose.yml" --env-file "$ENTORNO")

bajar() {
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -f "$ENTORNO"
}
trap bajar EXIT

# Secreto efímero para los JWT (R4) y un token de usuario y uno de admin para los tests.
export TP2_JWT_SECRET="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(48))')"
export TP2_TOKEN="$("$PYTHON" "$HIT1/../evidencias/emitir_token.py" --usuario ana --rol usuario)"
export TP2_TOKEN_ADMIN="$("$PYTHON" "$HIT1/../evidencias/emitir_token.py" --usuario root --rol admin)"

cat > "$ENTORNO" <<FIN
DOCKER_GID=$(stat -c %g /var/run/docker.sock)
TP2_IMAGENES_PERMITIDAS=cerberusdistribuido/tarea-prueba,cerberusdistribuido/tarea,cerberusdistribuido/no-es-tarea,cerberusdistribuido/tarea-fijada@sha256:$(printf '0%.0s' {1..64})
TP2_PUERTO=$PUERTO
TP2_TIMEOUT_EJECUCION=4
TP2_JWT_SECRET=$TP2_JWT_SECRET
TP2_REGISTRY_USUARIO=
TP2_REGISTRY_TOKEN=
FIN

echo "==> imágenes de la tarea de prueba y de la tarea real"
docker build -q -t cerberusdistribuido/tarea-prueba:test "$AQUI/tarea_prueba" >/dev/null
docker build -q -t cerberusdistribuido/tarea:test "$HIT1/tarea" >/dev/null
echo "==> imágenes para la validación (R2): una que no es servicio tarea y una con otro digest"
docker build -q -t cerberusdistribuido/no-es-tarea:test - >/dev/null <<'FIN'
FROM python:3.14-slim
CMD ["python", "-c", "raise SystemExit(3)"]
FIN
docker tag cerberusdistribuido/tarea-prueba:test cerberusdistribuido/tarea-fijada:test
echo "==> servidor (build + up, espera a healthy)"
"${COMPOSE[@]}" up --build -d --wait
echo "==> tests de integración"
cd "$AQUI/.."
TP2_URL="http://127.0.0.1:$PUERTO" "$PYTHON" -m pytest -m integracion -v -p no:cacheprovider
echo "==> cliente contra el servicio tarea real"
"$PYTHON" "$HIT1/cliente/cliente.py" division '{"a": 7, "b": 2}' --imagen cerberusdistribuido/tarea:test \
    --servidor "http://127.0.0.1:$PUERTO" --token "$TP2_TOKEN"
