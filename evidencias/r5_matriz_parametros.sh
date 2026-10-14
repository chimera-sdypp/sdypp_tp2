#!/usr/bin/env bash
# R5 — Pruebas con tareas diversas y matriz de parámetros:
#   1. Al menos 3 imágenes/tareas distintas (CPU-bound, procesamiento de archivos, fallas/timeout).
#   2. Cada tarea se prueba con parámetros válidos, límite e inválidos.
#   3. Matriz obligatoria V1 (archivos: 1, 10, 100) x V2 (acciones: 1, 3, 5) x V3 (concurrencia: 1, 10, 50).
#   4. Peticiones autenticadas (R4) con verificación de rechazo de bots sin token.
#   5. Métricas de latencia p50/p95, throughput y cuellos de botella detectados.
#
#   evidencias/r5_matriz_parametros.sh [hit2]
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
HIT="${1:-hit2}"
PUERTO="${TP2_PUERTO:-18090}"
URL="http://127.0.0.1:$PUERTO"
ENTORNO="$(mktemp)"
COMPOSE=(docker compose -f "$RAIZ/$HIT/docker-compose.yml" --env-file "$ENTORNO")
TOKEN="cerberus-token-2026"

bajar() {
    echo "==> Limpiando contenedores..."
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$ENTORNO"
}
trap bajar EXIT

ARCHIVO="${TP2_ENV:-$RAIZ/$HIT/.env}"
leer() { grep -m1 "^$1=" "$ARCHIVO" 2>/dev/null | cut -d= -f2- || true; }
USUARIO="$(leer TP2_REGISTRY_USUARIO)"
REG_TOKEN="$(leer TP2_REGISTRY_TOKEN)"

echo "======================================================================"
echo "== R5 — Pruebas con Tareas Diversas y Matriz de Parámetros ($HIT) =="
echo "======================================================================"

echo "==> 1. Construyendo las 3 imágenes de tareas especializadas..."
docker build -q -t cerberusdistribuido/tarea-cpu:1.0.0 "$RAIZ/$HIT/tareas/cpu" >/dev/null
docker build -q -t cerberusdistribuido/tarea-archivos:1.0.0 "$RAIZ/$HIT/tareas/archivos" >/dev/null
docker build -q -t cerberusdistribuido/tarea-falla:1.0.0 "$RAIZ/$HIT/tareas/falla" >/dev/null
echo "    ✓ cerberusdistribuido/tarea-cpu:1.0.0"
echo "    ✓ cerberusdistribuido/tarea-archivos:1.0.0"
echo "    ✓ cerberusdistribuido/tarea-falla:1.0.0"

PERMITIDAS="cerberusdistribuido/tarea,cerberusdistribuido/tarea-cpu,cerberusdistribuido/tarea-archivos,cerberusdistribuido/tarea-falla"

cat > "$ENTORNO" <<FIN
DOCKER_GID=$(stat -c %g /var/run/docker.sock)
TP2_IMAGENES_PERMITIDAS=$PERMITIDAS
TP2_PUERTO=$PUERTO
TP2_WORKERS_MAX=4
TP2_TIMEOUT_EJECUCION=4
TP2_API_TOKEN=$TOKEN
TP2_REGISTRY_USUARIO=$USUARIO
TP2_REGISTRY_TOKEN=$REG_TOKEN
FIN

echo "==> 2. Iniciando servidor $HIT en puerto $PUERTO..."
"${COMPOSE[@]}" up --build -d --wait >/dev/null 2>&1

echo "==> 3. Ejecutando batería de carga y mediciones (Python)..."
PYTHON="${PYTHON:-$RAIZ/.venv/bin/python3}"
if [ ! -f "$PYTHON" ]; then
    PYTHON="python3"
fi

"$PYTHON" "$RAIZ/evidencias/r5_generador_carga.py" \
    --servidor "$URL" \
    --token "$TOKEN" \
    --salida-json "$RAIZ/hit2/mediciones/matriz_r5_resultados.json" \
    --salida-md "$RAIZ/evidencias/R5_INFORME.md"

echo "======================================================================"
echo "== Resumen del Informe R5 =="
echo "======================================================================"
cat "$RAIZ/evidencias/R5_INFORME.md"
