# Contrato público — TP2 (Hits 1, 2 y 3)

Qué recibe y qué devuelve cada servidor: el request, la respuesta OK con su metadata, todos los
errores posibles y lo que el contrato **no** devuelve. El diseño y el porqué de cada decisión están en el
`contrato.md` y el README de cada hit. Cada ejemplo de este documento salió de un servidor real
(`evidencias/r3_contrato.sh`, ver [Cómo se comprobó](#6-cómo-se-comprobó)).

| Hit | URL | Qué podés hacer |
|---|---|---|
| 1 | `http://54.207.243.253:8081` | Mandar un cálculo y que se ejecute en un contenedor Docker que se crea y se borra para esa tarea |
| 2 | `http://54.207.243.253:8082` | Lo mismo, con varias tareas a la vez: hasta 4 corren en paralelo y el resto espera en una cola ordenada por reloj de Lamport |
| 3 | `http://54.207.243.253:8083` | Lo mismo, contra 3 servidores detrás de nginx: si matás uno, incluso el coordinador, las tareas siguen saliendo |

Los tres exponen `POST /getRemoteTask`, `GET /stats` y `GET /health`. Los mensajes entre nodos del Hit 3
(`/cluster/*`) y el servicio tarea no son públicos.

## 0. Autenticación y roles

El servidor **no guarda sesión**: cada pedido trae su identidad y su rol y se valida en cada llamada.

- **Cabecera:** `Authorization: Bearer <token>`. El token es un JWT HS256 con `sub` (el usuario), `role` y `exp` (vencimiento, obligatorio). Lo firma quien lo emite con el secreto del servidor (`evidencias/emitir_token.py`); el cliente nunca manda credenciales del registry.
- **Roles:** `usuario` (ejecuta tareas) y `admin` (ejecuta tareas y además ve `/stats`).

| Ruta | Sin token | `usuario` | `admin` |
|---|---|---|---|
| `POST /getRemoteTask` | 401 | ejecuta | ejecuta |
| `GET /stats` | 401 | 403 | 200 |
| `GET /health` | 200 (pública) | 200 | 200 |
| cualquier otra ruta | 401 | 404 / 405 | 404 / 405 |

- **Límite por usuario:** 300 pedidos por minuto por `sub` (configurable); pasado el límite, `429` con `Retry-After`. En el Hit 3 el límite se cuenta en cada nodo y nginx reparte entre los tres, así que el efecto en el cluster es de hasta 3 veces ese número.
- **Sin HTTPS ni CORS:** HTTP plano. Los tokens viajan en claro; es un TP, no hay TLS.

## 1. Request

`POST /getRemoteTask`

**Cabeceras**

| Cabecera | Obligatoria | Valor |
|---|---|---|
| `Authorization` | sí | `Bearer <JWT>` |
| `Content-Type` | sí | `application/json` (otro → 415) |
| `X-Lamport-Clock` | no (sólo Hit 2) | Entero, el reloj de Lamport del cliente. Si falta o no es un entero, vale 0 |

**Cuerpo** (JSON, hasta 64 KiB y 32 niveles de anidamiento; claves duplicadas, `NaN` y campos de más se rechazan)

| Campo | Tipo | Obligatorio | Por defecto | Reglas |
|---|---|---|---|---|
| `calculo` | string | sí | — | 1 a 64 caracteres: minúsculas, dígitos y `_`, empezando por una letra. Lo interpreta la imagen (la tarea de ejemplo conoce `suma`, `resta`, `multiplicacion`, `division`) |
| `parametros` | objeto | sí | — | Los interpreta la tarea; el servidor sólo exige que sea un objeto |
| `datos` | objeto | no | `{}` | Datos adicionales para la tarea |
| `imagen` | string | sí | — | `repositorio:tag` o `repositorio@sha256:…`, hasta 255 caracteres. El tag es obligatorio y no puede ser `latest`. Tiene que estar en la lista blanca del servidor (hoy, `cerberusdistribuido/tarea`) |

La validación es estricta: no convierte tipos (`"3"` no es `3`), no acepta campos de más y no
corrige nada; el error dice qué campo falló. La imagen se comprueba antes de ejecutar: bien formada,
en la lista blanca, existente y, si la lista fija un digest, con ese digest.

```json
{"calculo": "suma", "parametros": {"a": 3, "b": 4}, "datos": {}, "imagen": "cerberusdistribuido/tarea:1.0.0"}
```

```bash
curl -s -X POST http://54.207.243.253:8082/getRemoteTask \
  -H "Authorization: Bearer $TP2_TOKEN" -H 'Content-Type: application/json' \
  -d '{"calculo":"suma","parametros":{"a":3,"b":4},"imagen":"cerberusdistribuido/tarea:1.0.0"}'
```

## 2. Response OK

`200` con el sobre `{"codigo", "contenido"}`. Todas las respuestas, buenas o malas, usan este sobre.

| Campo de `contenido` | Tipo | Hits | Qué es |
|---|---|---|---|
| `calculo` | string | 1, 2, 3 | El cálculo pedido |
| `resultado` | cualquier JSON | 1, 2, 3 | Lo que devolvió la tarea |
| `task_id` | string (UUID v4) | 1, 2, 3 | Identificador de esta tarea; también está en el log del servidor |
| `imagen` | string | 1, 2, 3 | Imagen ejecutada, normalizada: `docker.io/cerberusdistribuido/tarea:1.0.0` |
| `digest` | string o `null` | 1, 2, 3 | Digest del registry (`sha256:…`) de la imagen que se ejecutó. `null` si la imagen no vino de un registry (p. ej. se construyó en el host) |
| `imagen_id` | string | 1, 2, 3 | ID local de la imagen (`sha256:…`): siempre existe, identifica el contenido exacto que corrió |
| `contenedor` | string | 1, 2, 3 | ID corto (12 caracteres) del contenedor que atendió la tarea. Se borra al terminar |
| `nodo` | entero | 3 | El nodo del cluster que ejecutó la tarea (1, 2 o 3) |
| `tiempos.en_cola_s` | número | 1, 2, 3 | Segundos de espera antes de empezar. Hit 2: en la cola esperando un worker libre. Hit 3: pidiéndole nodo al coordinador (más, si hubo que elegir coordinador o reasignar). Hit 1: siempre `0.0`, no hay cola |
| `tiempos.ejecucion_s` | número | 1, 2, 3 | Desde crear el contenedor hasta tener la respuesta de la tarea (no incluye borrarlo) |
| `tiempos.total_s` | número | 1, 2, 3 | Desde que el servidor empezó a atender el pedido hasta tener la respuesta: validación, descarga de la imagen si hizo falta, cola y ejecución |
| `lamport_ts` | entero | 2 | Reloj de Lamport del servidor al responder. También en la cabecera `X-Lamport-Clock` de toda respuesta |

**Hit 1**

```json
{"codigo":200,"contenido":{"calculo":"suma","resultado":7,
 "task_id":"5c4c664a-7bb9-4c17-a2f0-0315091e9944",
 "imagen":"docker.io/cerberusdistribuido/tarea-prueba:test",
 "digest":"sha256:70f35747b244103904729a3de71a1c677a3d1e42119bdb6a76a4b47760a440b4",
 "imagen_id":"sha256:70f35747b244103904729a3de71a1c677a3d1e42119bdb6a76a4b47760a440b4",
 "contenedor":"1dc16e0a537b",
 "tiempos":{"en_cola_s":0.0,"ejecucion_s":0.5529,"total_s":0.6799}}}
```

**Hit 2** (agrega `lamport_ts`; este cliente no mandó `X-Lamport-Clock`, así que valía 0)

```json
{"codigo":200,"contenido":{"calculo":"suma","resultado":7,
 "task_id":"ae3eb90e-c773-4217-a332-76ee96c70229",
 "imagen":"docker.io/cerberusdistribuido/tarea-prueba:test",
 "digest":"sha256:70f35747b244103904729a3de71a1c677a3d1e42119bdb6a76a4b47760a440b4",
 "imagen_id":"sha256:70f35747b244103904729a3de71a1c677a3d1e42119bdb6a76a4b47760a440b4",
 "contenedor":"8fd786f5c50b",
 "tiempos":{"en_cola_s":0.0001,"ejecucion_s":0.3432,"total_s":0.4634},
 "lamport_ts":4}}
```

**Hit 3** (agrega `nodo`)

```json
{"codigo":200,"contenido":{"calculo":"suma","resultado":7,
 "task_id":"f99e3b04-dbc4-42bf-b161-e82de0b281c4",
 "imagen":"docker.io/cerberusdistribuido/tarea-prueba:test",
 "digest":"sha256:70f35747b244103904729a3de71a1c677a3d1e42119bdb6a76a4b47760a440b4",
 "imagen_id":"sha256:70f35747b244103904729a3de71a1c677a3d1e42119bdb6a76a4b47760a440b4",
 "nodo":2,"contenedor":"a4fbfbda587d",
 "tiempos":{"en_cola_s":0.0024,"ejecucion_s":0.3418,"total_s":0.483}}}
```

Los tiempos están en segundos, redondeados a décimas de milisegundo. `resultado` depende de la tarea: el de
`suma` es un número; las tareas de la matriz de R5 (`procesar_archivos`, `primos`…) devuelven objetos.

### Otras rutas

**`GET /health`** (pública) dice si el servidor puede ejecutar tareas: `200` si sí, `503` si su Docker no responde.
Hit 2 agrega `pool` (`workers_activos`, `workers_max`, `tareas_encoladas`) y `lamport_ts`; el Hit 3 agrega `cluster`
(`nodo`, `coordinador`, `rol`, `tareas_en_curso` y, si contestó el coordinador, el estado de los 3 nodos).

**`GET /stats`** (sólo `admin`) devuelve, en el mismo sobre:

| Campo | Qué es |
|---|---|
| `tareas_ejecutadas` / `tareas_fallidas` | Tareas que terminaron bien / que llegaron a ejecutarse y terminaron mal (la tarea rechazó los parámetros, falló, no contestó, no se pudo bajar la imagen) |
| `tareas_por_imagen` | `{imagen: {ejecutadas, fallidas}}` |
| `latencia_s` | `{promedio, p95, muestras}`: tiempo total de la tarea, cola incluida, sobre las últimas 1000 tareas |
| `tareas_en_cola`, `workers_ocupados`, `workers_max` | Hit 2: la cola y el pool. Hit 1 y 3: `tareas_en_cola` es siempre 0 y `workers_ocupados` son las tareas en curso (`workers_max` es `null`) |
| `pedidos_por_codigo` | Cuántos pedidos a `/getRemoteTask` terminaron con cada código HTTP, incluidos los rechazados antes de ejecutar |
| `desde`, `uptime_s` | Desde cuándo cuenta (todo vive en memoria: se reinicia con el servidor) |
| `lamport_ts` | Sólo Hit 2 |
| `nodos`, `nodos_sin_respuesta`, `atendido_por` | Sólo Hit 3. Los números son del cluster entero: el nodo que atiende le pide los suyos a los otros dos. `nodos` desglosa por nodo; si alguno no contestó, figura en `nodos_sin_respuesta` y los totales son parciales |

Ejemplo (Hit 2):

```json
{"codigo":200,"contenido":{"tareas_ejecutadas":2,"tareas_fallidas":0,
 "tareas_por_imagen":{"docker.io/cerberusdistribuido/tarea:test":{"ejecutadas":2,"fallidas":0}},
 "latencia_s":{"promedio":0.45,"p95":0.463,"muestras":2},
 "tareas_en_cola":0,"workers_ocupados":0,"workers_max":4,
 "pedidos_por_codigo":{"200":2,"401":3,"422":5,"429":1},
 "desde":"2026-10-16T02:55:15+00:00","uptime_s":6,"lamport_ts":42}}
```

## 3. Errores

Toda respuesta de error usa el mismo sobre; `contenido` es sólo `{"error": {"tipo", "mensaje", "detalles"}}`.
**Los clientes tienen que decidir por `tipo`**; `mensaje` es para personas y puede cambiar. `detalles` es
una lista (vacía salvo en `PAYLOAD_INVALIDO`, donde cada elemento es `{"campo", "problema"}`).

```json
{"codigo": 422, "contenido": {"error": {"tipo": "TAREA_RECHAZADA", "mensaje": "división por cero", "detalles": []}}}
```

| Código | `tipo` | Cuándo ocurre | Cuerpo (`contenido.error`) |
|---|---|---|---|
| **400** | `CUERPO_VACIO` | El cuerpo está vacío | `"el cuerpo está vacío: se esperaba un objeto JSON"` |
| 400 | `JSON_INVALIDO` | JSON mal formado, claves duplicadas, `NaN`, más de 32 niveles o no es UTF-8 | `"JSON mal formado: Expecting value (línea 1, columna 12)"`, `"claves duplicadas en el JSON: calculo"` |
| **401** | `NO_AUTORIZADO` | Falta el token, no es un JWT, está firmado con otro secreto, no tiene `exp` o venció. Trae `WWW-Authenticate: Bearer` | `"falta el token: Authorization: Bearer <token>"`, `"token inválido"`, `"el token venció"` |
| **403** | `ROL_INSUFICIENTE` | Token válido, pero el rol no alcanza (`usuario` en `/stats`) o no es `usuario` ni `admin` | `"/stats es sólo para admin (tu rol: usuario)"` |
| 403 | `IMAGEN_NO_PERMITIDA` | La imagen no está en la lista blanca, o no coincide con el digest que la lista fija | `"la imagen docker.io/alguien/minero no está en la lista de imágenes permitidas"` |
| **404** | `RUTA_INEXISTENTE` | La ruta no existe (con token válido; sin token es 401) | `"no existe la ruta /no-existe"` |
| 405 | `METODO_NO_PERMITIDO` | La ruta existe pero no acepta ese método. Trae `Allow` | `"GET no está permitido en /getRemoteTask"` |
| **408** | — | **No se usa:** el servidor no emite 408. Una tarea que no contesta a tiempo es `504 TAREA_SIN_RESPUESTA` | — |
| **409** | `NO_ES_COORDINADOR`, `COORDINADOR_RECHAZADO` | **Sólo entre nodos del Hit 3** (`/cluster/*`): un nodo le pide algo al coordinador y ya no lo es, o anuncia un coordinador y el receptor es mayor. nginx no publica esas rutas: desde afuera son `404`, así que **un cliente nunca recibe un 409**. Una tarea no puede chocar con otra (no hay estado compartido ni recursos exclusivos) | `"el nodo 2 no es el coordinador"` |
| 413 | `CUERPO_DEMASIADO_GRANDE` | Más de 64 KiB (en el Hit 3 también lo contesta nginx, con el mismo sobre) | `"el cuerpo supera los 65536 bytes"` |
| 415 | `TIPO_DE_CONTENIDO` | `Content-Type` distinto de `application/json`. Trae `Accept: application/json` | `"el cuerpo tiene que ser application/json (llegó: text/plain)"` |
| **422** | `PAYLOAD_INVALIDO` | El JSON está bien pero no cumple el contrato; `detalles` dice qué campo falló | `"el payload no es válido"`, `detalles: [{"campo": "parametros", "problema": "tiene que ser un objeto, llegó un string"}, {"campo": "extra", "problema": "campo no permitido"}]` |
| 422 | `IMAGEN_INEXISTENTE` | La imagen está bien formada y permitida, pero no existe (o el servidor no puede acceder a ella) | `"la imagen docker.io/cerberusdistribuido/tarea-prueba:no-existe no existe o no es accesible"` |
| 422 | `TAREA_RECHAZADA` | La tarea rechazó los parámetros (p. ej. división por cero, un límite fuera de rango) | `"división por cero"` |
| **429** | `DEMASIADOS_PEDIDOS` | El usuario superó su límite de pedidos por minuto. Trae `Retry-After: <segundos>` | `"más de 6 pedidos por minuto para limitado: reintentá en 10 s"` (Hit 3: `"… para limitado en este nodo: …"`) |
| **500** | `ERROR_INTERNO` | Error inesperado del servidor. El detalle va al log, nunca al cliente | `"error interno del servidor"` |
| **502** | `TAREA_FALLIDA` | La tarea falló, devolvió una respuesta inválida, no se pudo levantar su contenedor, o la imagen no es un servicio tarea (el contenedor termina antes de quedar listo) | `"la tarea falló o devolvió una respuesta inválida"` |
| 502 | `REGISTRY_NO_DISPONIBLE` | No se pudo bajar la imagen del registry | `"no se pudo descargar la imagen"` |
| 502 | `NODO_NO_DISPONIBLE` | **Sólo Hit 3.** Lo contesta nginx: ningún nodo respondió, o se cayó el que atendía el pedido ya enviado. Se puede reintentar | `"ningún nodo pudo atender el pedido"` |
| **503** | `SERVICIO_NO_DISPONIBLE` | El Docker del servidor no responde | `"el servicio no puede ejecutar tareas en este momento"` |
| 503 | `CLUSTER_NO_DISPONIBLE` | **Sólo Hit 3.** No hay coordinador ni nodos vivos a tiempo | `"no hay coordinador ni nodos disponibles para ejecutar la tarea"` |
| **504** | `TAREA_SIN_RESPUESTA` | La tarea no arrancó en 30 s o no contestó en 60 s | `"la tarea no respondió en 60 s"` |

Qué reintentar: los `5xx` y `429` se pueden reintentar sin riesgo (las tareas son cálculos sin efectos secundarios; para el `429`,
esperando lo que diga `Retry-After`). Los `4xx` no mejoran sin cambiar el pedido.

## 4. Lo que falta

Datos que el servidor **no** devuelve pero podría, y por qué quedan afuera:

| Dato | Por qué no se devuelve |
|---|---|
| Logs del contenedor de la tarea | El contenedor se borra al terminar y no se captura su salida. Devolverlos obligaría a retenerlos (privacidad: pueden traer datos del usuario) y a limitar su tamaño |
| Uso de CPU y memoria de la tarea | Los contenedores viven ~0,3 s: una muestra de `docker stats` en ese tiempo no dice nada. Medirlo bien exige un cgroup por tarea y retener métricas |
| Intentos | En el Hit 3 una tarea se reasigna si se cae su nodo, pero el cliente sólo ve el resultado. No es una propiedad de los Hits 1 y 2 y el contrato es común; se podría agregar `intentos` y `nodos_excluidos` al Hit 3 |
| Posición en la cola al encolar (Hit 2) | Cambia mientras la tarea espera, y como la respuesta sólo sale al terminar, cuando llegaría ya no sirve. `tiempos.en_cola_s` dice cuánto esperó de verdad |
| Worker que atendió la tarea (Hit 2) | Los workers no tienen identidad: es un hilo por tarea con un tope. Lo que identifica quién la ejecutó es el `contenedor` |
| Reloj de Lamport en los Hits 1 y 3 | Es del Hit 2 (la cola ordena por él). Los otros hits no lo usan |
| Hora del servidor / timestamp de la respuesta | La cabecera `Date` de HTTP ya la trae; repetirla en el cuerpo no agrega nada. Sí falta, para auditoría, la hora de **encolado** y de **inicio** de cada tarea |
| `task_id` en las respuestas de error | Un pedido rechazado por validación no llega a ser una tarea. Para las que fallan después de empezar (502, 504) convendría devolverlo; hoy sólo está en el log |
| Cuántos pedidos le quedan al usuario (`X-RateLimit-*`) | El `429` ya dice cuándo reintentar con `Retry-After`; exponer el contador de cada usuario en todas las respuestas es más superficie por poco valor |
| Tamaño del resultado y del pedido | El cliente los conoce (`Content-Length`) |
| Versión del servidor y de la imagen del servidor | El `imagen_id` y el `digest` identifican la imagen de la **tarea**, no el código del servidor. No se expone la versión del servidor |

## 5. Verlo en Swagger

[`openapi.yaml`](openapi.yaml) tiene este mismo contrato en OpenAPI 3.1, con los tres hits como
servidores y el esquema de seguridad `Bearer`. Se puede pegar en [editor.swagger.io](https://editor.swagger.io/), o abrir en local
desde la raíz del repo:

```bash
docker run --rm -p 8088:8080 -v "$PWD/openapi.yaml:/usr/share/nginx/html/openapi.yaml:ro" -e URL=openapi.yaml swaggerapi/swagger-ui
# http://localhost:8088
```

"Try it out" no funciona desde el navegador (los servidores no mandan cabeceras CORS): usar el
`curl` que muestra Swagger.

## 6. Cómo se comprobó

```bash
evidencias/r3_contrato.sh hit2        # o hit1 / hit3
```

Levanta el hit con la tarea de prueba, dispara contra el servidor real cada fila de la tabla de errores y la respuesta OK,
y comprueba el código de cada una. Los dos códigos que no se pueden provocar sin romper el entorno están cubiertos por los tests unitarios:
`500` (`test_error_inesperado_es_500_generico`) y `503` (Docker caído, `test_api.py` y `test_lanzador.py`).
Las caídas del cluster (`502 NODO_NO_DISPONIBLE`, `503 CLUSTER_NO_DISPONIBLE`) las ejercita `hit3/servidor/tests/test_integracion.py`.
Los casos de autenticación y de roles, `evidencias/r4_autenticacion.sh`.
