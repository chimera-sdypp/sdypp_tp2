#!/usr/bin/env bash
# R4 — Servidor stateless con autenticación por request y dos roles (usuario y admin).
#
# Levanta el servidor del hit con un secreto JWT efímero y un límite de 5 pedidos por minuto,
# y prueba los casos de la práctica con curl, mostrando cada pedido y el código que vuelve:
#
#     sin token ................... 401        token de usuario en /stats ... 403
#     token de admin en /stats .... 200        token inválido ............... 401
#     token vencido ............... 401        /health sin token ............ 200 (público)
#     el usuario y el admin ejecutan una tarea (200)
#     más de 5 pedidos por minuto de un usuario ... 429 con Retry-After
#
#   evidencias/r4_autenticacion.sh [hit1|hit2|hit3]        (por defecto hit2)
#
# Contra un servidor ya desplegado, sin levantar nada (hace falta su secreto):
#   TP2_URL=http://host:8082 TP2_JWT_SECRET=... evidencias/r4_autenticacion.sh
# Sale con error si algún código no es el esperado.
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
HIT="${1:-hit2}"
PUERTO="${TP2_PUERTO:-18090}"
LIMITE=5
NODOS=1      # en el Hit 3 el límite se cuenta en cada nodo y nginx reparte entre los 3
[ "$HIT" = "hit3" ] && NODOS=3
PEDIDOS=$((LIMITE * NODOS))
IMAGEN="cerberusdistribuido/tarea:test"
TAREA='{"calculo":"suma","parametros":{"a":3,"b":4},"imagen":"'"$IMAGEN"'"}'
fallos=0

if [ -n "${TP2_URL:-}" ]; then
    URL="${TP2_URL%/}"
    : "${TP2_JWT_SECRET:?con TP2_URL hace falta TP2_JWT_SECRET, el secreto de ese servidor}"
else
    URL="http://127.0.0.1:$PUERTO"
    ENTORNO="$(mktemp)"
    COMPOSE=(docker compose -f "$RAIZ/$HIT/docker-compose.yml" --env-file "$ENTORNO")
    bajar() {
        "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
        rm -f "$ENTORNO"
    }
    trap bajar EXIT
    TP2_JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    cat > "$ENTORNO" <<FIN
DOCKER_GID=$(stat -c %g /var/run/docker.sock)
TP2_IMAGENES_PERMITIDAS=cerberusdistribuido/tarea
TP2_PUERTO=$PUERTO
TP2_JWT_SECRET=$TP2_JWT_SECRET
TP2_LIMITE_PEDIDOS_MIN=$LIMITE
TP2_REGISTRY_USUARIO=
TP2_REGISTRY_TOKEN=
FIN
    echo "==> imagen de la tarea (local, para no depender del registry)"
    docker build -q -t "$IMAGEN" "$RAIZ/$HIT/tarea" >/dev/null
    echo "==> servidor $HIT en $URL (build + up, espera a healthy)"
    "${COMPOSE[@]}" up --build -d --wait >/dev/null 2>&1
fi
export TP2_JWT_SECRET

token() { python3 "$RAIZ/evidencias/emitir_token.py" "$@"; }
USUARIO="$(token --usuario ana --rol usuario)"
OTRO_USUARIO="$(token --usuario beto --rol usuario)"
ADMIN="$(token --usuario root --rol admin)"
INVALIDO="$(token --usuario root --rol admin --secreto-erroneo)"
VENCIDO="$(token --usuario root --rol admin --segundos -5)"

# caso <descripción> <esperado> <método> <ruta> <token|-> [cuerpo]
caso() {
    local descripcion="$1" esperado="$2" metodo="$3" ruta="$4" token="$5" cuerpo="${6:-}"
    local args=(-s -o /tmp/r4_cuerpo.$$ -D /tmp/r4_cabeceras.$$ -w '%{http_code}' -X "$metodo" "$URL$ruta")
    [ "$token" != "-" ] && args+=(-H "Authorization: Bearer $token")
    [ -n "$cuerpo" ] && args+=(-H 'Content-Type: application/json' -d "$cuerpo")
    obtenido="$(curl "${args[@]}")"
    local marca="OK "
    if [ "$obtenido" != "$esperado" ]; then marca="MAL"; fallos=$((fallos + 1)); fi
    printf '[%s] %-52s esperado %s, obtenido %s\n' "$marca" "$descripcion" "$esperado" "$obtenido"
    printf '      %s\n' "$(head -c 230 /tmp/r4_cuerpo.$$)"
}

echo
echo "== Autenticación y roles (R4) =="
caso "GET /stats sin token"                         401 GET  /stats -
caso "GET /stats con token de usuario"              403 GET  /stats "$USUARIO"
caso "GET /stats con token de admin"                200 GET  /stats "$ADMIN"
caso "GET /stats con token firmado con otro secreto" 401 GET  /stats "$INVALIDO"
caso "GET /stats con token vencido"                 401 GET  /stats "$VENCIDO"
caso "GET /stats con un token que no es un JWT"     401 GET  /stats "basura"
caso "POST /getRemoteTask sin token"                401 POST /getRemoteTask - "$TAREA"
caso "POST /getRemoteTask con token vencido"        401 POST /getRemoteTask "$VENCIDO" "$TAREA"
caso "POST /getRemoteTask con token de usuario"     200 POST /getRemoteTask "$USUARIO" "$TAREA"
caso "POST /getRemoteTask con token de admin"       200 POST /getRemoteTask "$ADMIN" "$TAREA"
caso "GET /health sin token (público)"              200 GET  /health -

echo
echo "== Límite por usuario: $LIMITE pedidos por minuto (por nodo, si hay varios) (429) =="
# Pedidos que pasan la autenticación y se rechazan enseguida por el cuerpo ({}), así no
# hace falta esperar a ningún contenedor: lo que se prueba es el contador del usuario.
# Con 3 nodos y reparto round-robin, el pedido 3*L+1 es el primero que supera el límite de algún nodo.
for i in $(seq 1 "$PEDIDOS"); do
    codigo="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$URL/getRemoteTask" \
        -H "Authorization: Bearer $OTRO_USUARIO" -H 'Content-Type: application/json' -d '{}')"
    printf '      pedido %d de beto: %s\n' "$i" "$codigo"
    [ "$codigo" = "429" ] && { echo "[MAL] beto recibió 429 antes del límite"; fallos=$((fallos + 1)); }
done
caso "pedido $((PEDIDOS + 1)) de beto en el mismo minuto" 429 POST /getRemoteTask "$OTRO_USUARIO" '{}'
echo "      Retry-After: $(grep -i '^retry-after' /tmp/r4_cabeceras.$$ | tr -d '\r' | cut -d' ' -f2) s"
caso "ana no se ve afectada por el límite de beto"  422 POST /getRemoteTask "$USUARIO" '{}'
caso "un pedido sin token sigue siendo 401, no 429" 401 POST /getRemoteTask - '{}'

echo
echo "== /stats (admin) =="
curl -s -H "Authorization: Bearer $ADMIN" "$URL/stats" | python3 -m json.tool
rm -f /tmp/r4_cuerpo.$$ /tmp/r4_cabeceras.$$

echo
if [ "$fallos" -eq 0 ]; then echo "R4: todos los casos dieron lo esperado."; else echo "R4: $fallos caso(s) con un código inesperado."; exit 1; fi
