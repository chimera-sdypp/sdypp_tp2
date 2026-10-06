# Contrato público — TP2 (Hits 1, 2 y 3)

Qué recibe y qué devuelve cada servidor. El diseño y el porqué de cada decisión están en el
`contrato.md` y el README de cada hit.

Los tres hits están desplegados y exponen las mismas dos rutas:

| Hit | URL | Qué podés hacer |
|---|---|---|
| 1 | `http://18.231.127.74:8081` | Mandar un cálculo y que se ejecute en un contenedor Docker que se crea y se borra para esa tarea |
| 2 | `http://18.231.127.74:8082` | Lo mismo, con varias tareas a la vez: hasta 4 corren en paralelo y el resto espera en una cola ordenada por reloj de Lamport |
| 3 | `http://18.231.127.74:8083` | Lo mismo, contra 3 servidores detrás de nginx: si matás uno, incluso el coordinador, las tareas siguen saliendo |

## Lo común a los tres

**`POST /getRemoteTask`** ejecuta una tarea. Cabecera `Content-Type: application/json` y este cuerpo:

```json
{"calculo": "suma", "parametros": {"a": 3, "b": 4}, "datos": {}, "imagen": "cerberusdistribuido/tarea:1.0.0"}
```

| Campo | Obligatorio | Qué es |
|---|---|---|
| `calculo` | sí | `suma`, `resta`, `multiplicacion` o `division` |
| `parametros` | sí | `{"a": <número>, "b": <número>}` |
| `datos` | no | Datos adicionales para la tarea (`{}` por defecto) |
| `imagen` | sí | La imagen que resuelve la tarea, con versión fija. La única permitida es `cerberusdistribuido/tarea` |

El cliente no manda credenciales: el servidor baja la imagen privada con su propio token de sólo
lectura. La validación es estricta: un tipo equivocado, un campo de más o un JSON inválido se
rechazan y la respuesta dice qué falló. El cuerpo puede tener hasta 64 KiB.

**`GET /health`** dice si el servidor puede ejecutar tareas: `200` si sí, `503` si su Docker no responde.

**Toda respuesta tiene el mismo sobre**, salga bien o mal:

```json
{"codigo": 200, "contenido": {"calculo": "suma", "resultado": 7}}
{"codigo": 422, "contenido": {"error": {"tipo": "TAREA_RECHAZADA", "mensaje": "división por cero", "detalles": []}}}
```

| Código | `tipo` | Cuándo |
|---|---|---|
| 400 | `CUERPO_VACIO`, `JSON_INVALIDO` | No hay cuerpo, o el JSON está mal formado, tiene claves duplicadas o `NaN` |
| 403 | `IMAGEN_NO_PERMITIDA` | La imagen no está en la lista blanca |
| 404 / 405 | `RUTA_INEXISTENTE`, `METODO_NO_PERMITIDO` | La ruta no existe, o no acepta ese método |
| 413 | `CUERPO_DEMASIADO_GRANDE` | Más de 64 KiB |
| 415 | `TIPO_DE_CONTENIDO` | Falta `Content-Type: application/json` |
| 422 | `PAYLOAD_INVALIDO`, `IMAGEN_INEXISTENTE`, `TAREA_RECHAZADA` | El pedido no es válido (`detalles` dice qué campo), la imagen no existe, o la tarea rechazó los parámetros (p. ej. división por cero) |
| 500 | `ERROR_INTERNO` | Error inesperado del servidor |
| 502 | `TAREA_FALLIDA`, `REGISTRY_NO_DISPONIBLE` | La tarea falló o no se pudo bajar la imagen |
| 503 | `SERVICIO_NO_DISPONIBLE` | Docker no responde |
| 504 | `TAREA_SIN_RESPUESTA` | La tarea no arrancó en 30 s o no contestó en 60 s |

Los errores 5xx se pueden reintentar sin riesgo: las tareas son cálculos sin efectos secundarios.
No hay autenticación ni HTTPS.

## Hit 1 — una tarea en un contenedor efímero

Cada pedido levanta un contenedor de la imagen, le pasa el cálculo, espera el resultado y lo borra.

```bash
curl -s -H 'Content-Type: application/json' \
  -d '{"calculo":"multiplicacion","parametros":{"a":6,"b":7},"imagen":"cerberusdistribuido/tarea:1.0.0"}' \
  http://18.231.127.74:8081/getRemoteTask
# {"codigo":200,"contenido":{"calculo":"multiplicacion","resultado":42}}
```

## Hit 2 — varias tareas a la vez, ordenadas por Lamport

Hasta 4 tareas corren en paralelo, cada una en su contenedor; las demás esperan con el pedido
abierto. La cola sale en orden del reloj de Lamport que manda cada cliente.

- **Mandá tu reloj** en la cabecera `X-Lamport-Clock` (opcional; si falta o no es un entero, vale 0).
- **El servidor devuelve el suyo** en `X-Lamport-Clock` en toda respuesta, y además en
  `contenido.lamport_ts` en el `200` y en `/health`.
- **`/health` muestra el pool:** `workers_activos`, `workers_max` y `tareas_encoladas`.

```bash
curl -s -i -H 'Content-Type: application/json' -H 'X-Lamport-Clock: 41' \
  -d '{"calculo":"suma","parametros":{"a":3,"b":4},"imagen":"cerberusdistribuido/tarea:1.0.0"}' \
  http://18.231.127.74:8082/getRemoteTask
# x-lamport-clock: 12820
# {"codigo":200,"contenido":{"calculo":"suma","resultado":7,"lamport_ts":12820}}
```

## Hit 3 — cluster de 3 nodos con elección de líder

nginx reparte los pedidos entre 3 nodos. Uno de ellos, el coordinador (elegido con el algoritmo
Bully), decide qué nodo ejecuta cada tarea. Si se cae el nodo que ejecuta, la tarea pasa a otro;
si se cae el coordinador, se elige otro en menos de un segundo.

- **La respuesta dice qué nodo ejecutó la tarea**, en `contenido.nodo`.
- **`/health` muestra el cluster** desde el nodo que contestó: quién es el coordinador (`null`
  durante una elección) y, si contestó el coordinador, el estado de los 3 nodos.
- **Dos errores más:** `502 NODO_NO_DISPONIBLE` (lo devuelve nginx si ningún nodo responde o se
  cayó el que atendía tu pedido: reintentá) y `503 CLUSTER_NO_DISPONIBLE` (no hay coordinador ni
  nodos vivos).
- Los mensajes entre nodos (`/cluster/*`) no se publican: desde afuera dan `404`.

```bash
curl -s http://18.231.127.74:8083/health
# {"codigo":200,"contenido":{"servidor":"ok","docker":"ok",
#   "cluster":{"nodo":1,"coordinador":3,"rol":"worker","tareas_en_curso":0}}}
```

## Verlo en Swagger

[`openapi.yaml`](openapi.yaml) tiene este mismo contrato en OpenAPI 3.1, con los tres hits como
servidores. Se puede pegar en [editor.swagger.io](https://editor.swagger.io/), o abrir en local
desde la raíz del repo:

```bash
docker run --rm -p 8088:8080 -v "$PWD/openapi.yaml:/usr/share/nginx/html/openapi.yaml:ro" -e URL=openapi.yaml swaggerapi/swagger-ui
# http://localhost:8088
```

"Try it out" no funciona desde el navegador (los servidores no mandan cabeceras CORS): usar el
`curl` que muestra Swagger.
