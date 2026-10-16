#!/usr/bin/env bash
# R5 — Pruebas con tareas diversas y matriz de parámetros:
#   1. Al menos 3 imágenes/tareas distintas (CPU-bound, procesamiento de archivos, fallas/timeout).
#   2. Cada tarea se prueba con parámetros válidos, límite e inválidos.
#   3. Matriz obligatoria V1 (archivos: 1, 10, 100) x V2 (acciones: 1, 3, 5) x V3 (concurrencia: 1, 10, 50).
#   4. Peticiones autenticadas con mezcla de múltiples usuarios y rechazo estricto de bots sin token.
#   5. Métricas de latencia p50/p95, throughput y cuellos de botella detectados en /health.
#
# Uso:
#   evidencias/r5_matriz_parametros.sh [hit2]
#   TP2_SERVIDOR="http://18.231.127.74:8082" TP2_JWT_SECRET=... evidencias/r5_matriz_parametros.sh
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
HIT="${1:-hit2}"
SERVIDOR_REMOTO="${TP2_SERVIDOR:-}"
PUERTO="${TP2_PUERTO:-18090}"
URL="${SERVIDOR_REMOTO:-http://127.0.0.1:$PUERTO}"
TIMEOUT_EJECUCION="${TP2_TIMEOUT_EJECUCION:-60}"
ENTORNO="$(mktemp)"
COMPOSE=(docker compose -f "$RAIZ/$HIT/docker-compose.yml" --env-file "$ENTORNO")

bajar() {
    if [ -z "$SERVIDOR_REMOTO" ]; then
        echo "==> Limpiando contenedores locales..."
        "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    fi
    rm -rf "$ENTORNO"
}
trap bajar EXIT

ARCHIVO="${TP2_ENV:-$RAIZ/$HIT/.env}"
leer() { grep -m1 "^$1=" "$ARCHIVO" 2>/dev/null | cut -d= -f2- || true; }
USUARIO="$(leer TP2_REGISTRY_USUARIO)"
REG_TOKEN="$(leer TP2_REGISTRY_TOKEN)"

# Autenticación (R4): los usuarios de la prueba son tres JWT distintos, firmados con el secreto
# del servidor. Contra un servidor local se genera un secreto efímero; contra uno remoto hace falta
# su TP2_JWT_SECRET (o TP2_TOKENS con los JWT ya emitidos, separados por comas).
if [ -z "${TP2_TOKENS:-}" ]; then
    if [ -z "$SERVIDOR_REMOTO" ]; then
        TP2_JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    fi
    : "${TP2_JWT_SECRET:?con TP2_SERVIDOR hace falta TP2_JWT_SECRET o TP2_TOKENS}"
    export TP2_JWT_SECRET
    TOKENS="$(for u in ana beto carla; do python3 "$RAIZ/evidencias/emitir_token.py" --usuario "$u" --rol usuario --horas 4; done | paste -sd,)"
else
    TOKENS="$TP2_TOKENS"
fi

echo "======================================================================"
echo "== R5 — Pruebas con Tareas Diversas y Matriz de Parámetros ($HIT) =="
echo "======================================================================"
echo "Objetivo: $URL (timeout=$TIMEOUT_EJECUCION s)"

if [ -z "$SERVIDOR_REMOTO" ]; then
    # Las imágenes especializadas (tags cpu-1.0.0, archivos-1.0.0, falla-1.0.0) se descargan
    # directamente desde el repositorio de Docker Hub usando el token configurado.
    PERMITIDAS="cerberusdistribuido/tarea"

    cat > "$ENTORNO" <<FIN
DOCKER_GID=$(stat -c %g /var/run/docker.sock 2>/dev/null || echo 0)
TP2_IMAGENES_PERMITIDAS=$PERMITIDAS
TP2_PUERTO=$PUERTO
TP2_WORKERS_MAX=4
TP2_TIMEOUT_EJECUCION=$TIMEOUT_EJECUCION
TP2_JWT_SECRET=$TP2_JWT_SECRET
TP2_LIMITE_PEDIDOS_MIN=0
TP2_REGISTRY_USUARIO=$USUARIO
TP2_REGISTRY_TOKEN=$REG_TOKEN
FIN

    echo "==> 1. Iniciando servidor $HIT en puerto local $PUERTO..."
    "${COMPOSE[@]}" up --build -d --wait >/dev/null 2>&1
else
    echo "==> 1. Evaluando contra servidor remoto configurado: $URL"
fi

echo "==> 2. Ejecutando batería de carga y mediciones (Python)..."
PYTHON="${PYTHON:-$RAIZ/.venv/bin/python3}"
if [ ! -f "$PYTHON" ]; then
    PYTHON="python3"
fi

"$PYTHON" "$RAIZ/evidencias/r5_generador_carga.py" \
    --servidor "$URL" \
    --tokens "$TOKENS" \
    --timeout-ejecucion "$TIMEOUT_EJECUCION" \
    --salida-json "$RAIZ/hit2/mediciones/matriz_r5_resultados.json" \
    --salida-md "$RAIZ/evidencias/R5_INFORME.md"

echo "======================================================================"
echo "== Resumen del Informe R5 =="
echo "======================================================================"
cat "$RAIZ/evidencias/R5_INFORME.md"
