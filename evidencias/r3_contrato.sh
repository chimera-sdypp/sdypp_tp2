#!/usr/bin/env bash
# R3 — Contrato de ida y vuelta: dispara, contra un servidor real, cada respuesta de la tabla de
# errores del contrato (contrato-publico.md) y la respuesta OK con su metadata, y comprueba el
# código HTTP de cada una. Imprime el pedido, el código y el cuerpo.
#
#   evidencias/r3_contrato.sh [hit1|hit2|hit3]        (por defecto hit2)
#
# Levanta el hit con un secreto JWT efímero, la tarea de prueba de hit1/servidor/tests y un
# límite de 6 pedidos por minuto, y lo baja al terminar. Los dos códigos que no se pueden
# provocar sin romper el entorno (500 y 503) los cubren los tests unitarios: se indica cuáles.
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
HIT="${1:-hit2}"
PUERTO="${TP2_PUERTO:-18090}"
URL="http://127.0.0.1:$PUERTO"
LIMITE=6
NODOS=1
[ "$HIT" = "hit3" ] && NODOS=3
ENTORNO="$(mktemp)"
COMPOSE=(docker compose -f "$RAIZ/$HIT/docker-compose.yml" --env-file "$ENTORNO")
fallos=0

bajar() {
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -f "$ENTORNO" /tmp/r3_cuerpo.$$ /tmp/r3_cabeceras.$$
}
trap bajar EXIT

export TP2_JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
token() { python3 "$RAIZ/evidencias/emitir_token.py" "$@"; }
# Un usuario nuevo por caso: el límite por minuto es por usuario y no debe tapar los demás códigos.
nuevo() { token --usuario "usuario$RANDOM$RANDOM" --rol usuario; }
LIMITADO="$(token --usuario limitado --rol usuario)"

cat > "$ENTORNO" <<FIN
DOCKER_GID=$(stat -c %g /var/run/docker.sock)
TP2_IMAGENES_PERMITIDAS=cerberusdistribuido/tarea-prueba
TP2_PUERTO=$PUERTO
TP2_TIMEOUT_EJECUCION=4
TP2_JWT_SECRET=$TP2_JWT_SECRET
TP2_LIMITE_PEDIDOS_MIN=$LIMITE
TP2_REGISTRY_USUARIO=
TP2_REGISTRY_TOKEN=
FIN
IMAGEN="cerberusdistribuido/tarea-prueba:test"
docker build -q -t "$IMAGEN" "$RAIZ/hit1/servidor/tests/tarea_prueba" >/dev/null
echo "==> $HIT en $URL (build + up, espera a healthy)"
"${COMPOSE[@]}" up --build -d --wait >/dev/null 2>&1

# caso <descripción> <código esperado> <método> <ruta> <token|-> [cuerpo] [content-type|-] [cabecera extra]
caso() {
    local descripcion="$1" esperado="$2" metodo="$3" ruta="$4" token="$5" cuerpo="${6:-}" tipo="${7:-application/json}" extra="${8:-}"
    local args=(-s -o /tmp/r3_cuerpo.$$ -D /tmp/r3_cabeceras.$$ -w '%{http_code}' -X "$metodo" "$URL$ruta")
    [ "$token" != "-" ] && args+=(-H "Authorization: Bearer $token")
    [ "$tipo" != "-" ] && args+=(-H "Content-Type: $tipo")
    [ -n "$extra" ] && args+=(-H "$extra")
    [ -n "$cuerpo" ] && args+=(--data-binary "$cuerpo")
    obtenido="$(curl "${args[@]}")"
    local marca="OK "
    if [ "$obtenido" != "$esperado" ]; then marca="MAL"; fallos=$((fallos + 1)); fi
    printf '[%s] %-58s %s -> %s\n' "$marca" "$descripcion" "$esperado" "$obtenido"
    printf '      %s\n' "$(head -c 1200 /tmp/r3_cuerpo.$$)"
}
tarea() { printf '{"calculo":"%s","parametros":%s,"imagen":"%s"}' "$1" "$2" "$IMAGEN"; }

echo; echo "== 200: respuesta OK con su metadata =="
caso "suma 3 + 4" 200 POST /getRemoteTask "$(nuevo)" "$(tarea suma '{"a":3,"b":4}')"

echo; echo "== Errores =="
caso "400 cuerpo vacío" 400 POST /getRemoteTask "$(nuevo)" " "
caso "400 JSON mal formado" 400 POST /getRemoteTask "$(nuevo)" '{"calculo":'
caso "400 claves duplicadas" 400 POST /getRemoteTask "$(nuevo)" '{"calculo":"suma","calculo":"resta"}'
caso "401 sin token" 401 POST /getRemoteTask - "$(tarea suma '{"a":1,"b":1}')"
caso "401 token vencido" 401 POST /getRemoteTask "$(token --usuario vencido --segundos -5)" "$(tarea suma '{"a":1,"b":1}')"
caso "403 rol insuficiente (usuario en /stats)" 403 GET /stats "$(nuevo)"
caso "403 imagen fuera de la lista blanca" 403 POST /getRemoteTask "$(nuevo)" '{"calculo":"suma","parametros":{"a":1,"b":1},"imagen":"alguien/minero:1.0"}'
caso "404 ruta inexistente" 404 GET /no-existe "$(nuevo)" "" -
caso "405 método no permitido" 405 GET /getRemoteTask "$(nuevo)" "" -
caso "413 cuerpo de más de 64 KiB" 413 POST /getRemoteTask "$(nuevo)" "$(python3 -c 'import json; print(json.dumps({"calculo":"suma","parametros":{"x":"a"*70000},"imagen":"x/y:1"}))')"
caso "415 sin Content-Type: application/json" 415 POST /getRemoteTask "$(nuevo)" "$(tarea suma '{"a":1,"b":1}')" text/plain
caso "422 payload inválido (tipos y campos de más)" 422 POST /getRemoteTask "$(nuevo)" '{"calculo":"suma","parametros":"no","imagen":"x/y:1","extra":1}'
caso "422 imagen inexistente (tag que no existe)" 422 POST /getRemoteTask "$(nuevo)" '{"calculo":"suma","parametros":{"a":1,"b":1},"imagen":"cerberusdistribuido/tarea-prueba:no-existe"}'
caso "422 la tarea rechaza los parámetros (división por cero)" 422 POST /getRemoteTask "$(nuevo)" "$(tarea division '{"a":1,"b":0}')"
caso "502 la tarea falla" 502 POST /getRemoteTask "$(nuevo)" "$(tarea explotar '{}')"
caso "504 la tarea no contesta a tiempo (timeout de 4 s)" 504 POST /getRemoteTask "$(nuevo)" "$(tarea dormir '{"segundos":10}')"

echo
echo "-- 429: $LIMITE pedidos por minuto por usuario (por nodo, en el Hit 3) --"
for i in $(seq 1 $((LIMITE * NODOS))); do
    curl -s -o /dev/null -X POST "$URL/getRemoteTask" -H "Authorization: Bearer $LIMITADO" -H 'Content-Type: application/json' -d '{}'
done
caso "429 pedido $((LIMITE * NODOS + 1)) del mismo usuario en el minuto" 429 POST /getRemoteTask "$LIMITADO" '{}'
echo "      Retry-After: $(grep -i '^retry-after' /tmp/r3_cabeceras.$$ | tr -d '\r' | cut -d' ' -f2) s"

echo
echo "-- No se pueden provocar acá (se cubren con tests unitarios) --"
echo "   500 ERROR_INTERNO ........ hit*/servidor/tests/test_api.py::test_error_inesperado_es_500_generico"
echo "   503 SERVICIO_NO_DISPONIBLE  hit*/servidor/tests/test_api.py (Docker caído) y test_lanzador.py"
[ "$HIT" = "hit3" ] && echo "   502 NODO_NO_DISPONIBLE y 503 CLUSTER_NO_DISPONIBLE: caídas del cluster, ver hit3/servidor/tests/test_integracion.py"

echo
if [ "$fallos" -eq 0 ]; then echo "R3: todos los códigos dieron lo esperado."; else echo "R3: $fallos caso(s) con un código inesperado."; exit 1; fi
