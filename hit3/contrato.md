# Contrato — Hit 3 (cluster de tareas remotas)

Es el contrato del Hit 1, visto desde el cliente a través de nginx, con lo que agrega el cluster:
el nodo que ejecutó la tarea en la respuesta (§2), el estado del cluster en `/health`, los errores
del cluster (§4) y los mensajes entre nodos (§6), que no se publican.

## 1. El sobre

**Toda** respuesta es JSON y tiene **el mismo sobre**, salga bien o mal, en cualquier ruta y con
cualquier método:

```json
{"codigo": 200, "contenido": {"calculo": "suma", "resultado": 7, "nodo": 2}}
{"codigo": 403, "contenido": {"error": {"tipo": "IMAGEN_NO_PERMITIDA", "mensaje": "…", "detalles": []}}}
```

| Clave | Qué es |
|---|---|
| `codigo` | Repite el código HTTP |
| `contenido` | Siempre un objeto. Si salió bien, el resultado. Si no, **sólo** `{"error": {tipo, mensaje, detalles}}`, con `tipo` del enum de §4 |

Las respuestas **nunca** incluyen IPs, nombres de contenedores ni trazas: el detalle interno de un
error queda en el log del servidor.

## 2. Operaciones

| Método y ruta | Qué hace |
|---|---|
| `POST /getRemoteTask` | `ejecutarTareaRemota()` en el nodo que asigne el coordinador; devuelve el resultado |
| `GET /health` | Estado del nodo que contesta y del cluster |

Es lo único que publica nginx. Cualquier otra ruta (también `/cluster/*`) → `404 RUTA_INEXISTENTE`. Un método que la ruta no soporta →
`405 METODO_NO_PERMITIDO` con la cabecera `Allow`. Siempre en el sobre.

### `POST /getRemoteTask`

Cabecera obligatoria: `Content-Type: application/json`.

```json
{
  "calculo": "suma",
  "parametros": {"a": 3, "b": 4},
  "datos": {},
  "imagen": "cerberusdistribuido/tarea:1.0.0"
}
```

| Campo | Tipo | Obligatorio | Regla |
|---|---|---|---|
| `calculo` | string | sí | `^[a-z][a-z0-9_]*$`, 1 a 64 caracteres |
| `parametros` | objeto | sí | Lo interpreta la tarea; el servidor sólo exige que sea un objeto |
| `datos` | objeto | no (`{}`) | Datos adicionales para la tarea |
| `imagen` | string | sí | Referencia Docker con tag explícito o digest; `latest` (explícito o implícito) se rechaza. Tiene que estar en la lista blanca (§3) |

Reglas de validación — **se rechaza, nunca se "arregla"**:

- Tipos estrictos: `"calculo": 123`, `"parametros": "a=1"` o `"parametros": null` son `422`. No
  hay conversiones implícitas.
- Campos de más → `422` (así un typo como `"parametro"`, o un `"password"`, no pasa en silencio).
- Claves duplicadas en el JSON (`{"imagen": "a", "imagen": "b"}`) → `400`: si no, cada parser se
  queda con una distinta.
- `NaN`, `Infinity` → `400`: no son JSON válido.
- Más de 32 niveles de anidamiento → `400`. El límite es explícito: el parser de JSON es recursivo
  y, sin límite, aceptaría o rechazaría el mismo cuerpo según el stack de la máquina.
- El cuerpo tiene que ser un objeto JSON (un array → `422`), de 64 KiB como máximo (`413`).

Respuestas:

| Código | Cuándo | `contenido` / `contenido.error.tipo` |
|---|---|---|
| `200` | La tarea terminó | `{calculo, resultado, nodo}`: `nodo` es el ID del nodo que la ejecutó |
| `400` | Cuerpo vacío, JSON mal formado, claves duplicadas, `NaN`, demasiado anidado | `CUERPO_VACIO`, `JSON_INVALIDO` |
| `403` | La imagen no está en la lista blanca | `IMAGEN_NO_PERMITIDA` |
| `413` | Cuerpo de más de 64 KiB | `CUERPO_DEMASIADO_GRANDE` |
| `415` | `Content-Type` distinto de `application/json` | `TIPO_DE_CONTENIDO` |
| `422` | Falla la validación; la imagen no existe; la tarea rechazó los parámetros | `PAYLOAD_INVALIDO`, `IMAGEN_INEXISTENTE`, `TAREA_RECHAZADA` |
| `500` | Error inesperado del servidor | `ERROR_INTERNO` |
| `502` | La tarea falló o contestó algo inválido; el registry no respondió; se cayó el nodo que atendía el pedido (lo contesta nginx) | `TAREA_FALLIDA`, `REGISTRY_NO_DISPONIBLE`, `NODO_NO_DISPONIBLE` |
| `503` | Docker no responde; no hay coordinador ni nodos vivos a tiempo (`TP2_TIMEOUT_ASIGNACION`) | `SERVICIO_NO_DISPONIBLE`, `CLUSTER_NO_DISPONIBLE` |
| `504` | El contenedor no quedó listo o no contestó a tiempo | `TAREA_SIN_RESPUESTA` |

Por qué esos códigos:
- **`403` y no `400`** para la imagen fuera de la lista: el pedido está bien formado, pero el
  servidor se niega a ejecutarlo.
- **`422` y no `404`** para una imagen que no existe: `/getRemoteTask` sí existe; lo que no sirve es
  el contenido del pedido.
- **`502` / `504`** cuando falla el contenedor tarea: para ese tramo el servidor actúa como
  *gateway* (RFC 9110 §15.6.3 y §15.6.5).

### `GET /health`

```json
{"codigo": 200, "contenido": {"servidor": "ok", "docker": "ok",
  "cluster": {"nodo": 3, "coordinador": 3, "rol": "coordinador", "tareas_en_curso": 0,
              "nodos": {"1": {"estado": "vivo", "tareas_en_curso": 0, "ultimo_heartbeat_hace": 0.4},
                        "2": {"estado": "vivo", "tareas_en_curso": 1, "ultimo_heartbeat_hace": 0.9},
                        "3": {"estado": "vivo", "tareas_en_curso": 0, "ultimo_heartbeat_hace": 0.1}}}}}
```

`200` si puede atender. `503` si Docker no responde, con `"docker": "caido"`: el proceso vive, pero
no puede ejecutar tareas. `cluster` es la vista del nodo que contestó (nginx elige cuál):
`coordinador` es `null` durante una elección, y `nodos` (el registro) sólo aparece si contestó el
coordinador.

## 3. Lista blanca de imágenes

`TP2_IMAGENES_PERMITIDAS` es una lista de repositorios separados por comas
(`cerberusdistribuido/tarea`). Los nombres se normalizan antes de comparar:
`cerberusdistribuido/tarea` ≡ `docker.io/cerberusdistribuido/tarea`. Si la variable está vacía no
se permite **ninguna** imagen.

## 4. Tipos de error

`CUERPO_VACIO` · `JSON_INVALIDO` · `TIPO_DE_CONTENIDO` · `CUERPO_DEMASIADO_GRANDE` ·
`PAYLOAD_INVALIDO` · `IMAGEN_NO_PERMITIDA` · `IMAGEN_INEXISTENTE` · `TAREA_RECHAZADA` ·
`TAREA_FALLIDA` · `TAREA_SIN_RESPUESTA` · `REGISTRY_NO_DISPONIBLE` · `SERVICIO_NO_DISPONIBLE` ·
`RUTA_INEXISTENTE` · `METODO_NO_PERMITIDO` · `ERROR_INTERNO` · `CLUSTER_NO_DISPONIBLE` ·
`NODO_NO_DISPONIBLE` (sólo de nginx) · `NO_ES_COORDINADOR` y `COORDINADOR_RECHAZADO` (sólo entre
nodos, §6)

En `PAYLOAD_INVALIDO`, `contenido.error.detalles` lista cada problema:
`[{"campo": "parametros", "problema": "no puede ser null"}]`.

## 5. Lo que el servidor espera del servicio tarea

Lo implementa [`tarea/tarea.py`](tarea/tarea.py), igual que en el Hit 1. Usa **el mismo sobre** que el servidor (§1):
`{"codigo", "contenido"}`, salga bien o mal.

- Escucha HTTP en el puerto `8080` del contenedor.
- `GET /health` → `200` cuando está listo. El servidor lo consulta antes de mandarle el trabajo.
- `POST /ejecutarTarea` con `{"calculo", "parametros", "datos"}`:
  - `200` → `{"codigo": 200, "contenido": {"resultado": <cualquier JSON>}}`
  - `4xx` → los parámetros no sirven (p. ej. división por cero):
    `{"codigo": 422, "contenido": {"error": {"tipo": "…", "mensaje": "división por cero"}}}`.
    El `mensaje` le llega al cliente como `TAREA_RECHAZADA`.
  - `5xx` → falla de la tarea; el cliente recibe `TAREA_FALLIDA`.
  - Un `200` sin el sobre o sin `contenido.resultado` es una respuesta inválida: `TAREA_FALLIDA`.

Cálculos de `tarea/tarea.py`: `suma`, `resta`, `multiplicacion` y `division`, sobre
`parametros = {"a": <número>, "b": <número>}`. Un cálculo desconocido, parámetros que no son números,
la división por cero o un resultado fuera de rango son un `422`.

## 6. Mensajes entre nodos (`/cluster/*`)

Sólo por la red interna `cluster`: nginx no los publica. Todos son `POST` con JSON y contestan con
el sobre de §1. Un mensaje mal formado (tipos, campos de más) → `422 PAYLOAD_INVALIDO`.

| Ruta | Cuerpo | Respuesta |
|---|---|---|
| `/cluster/eleccion` | `{"de": 1}` | `200 {"ok": <mi id>}`: es el `OK` de Bully; el receptor arranca su propia elección |
| `/cluster/coordinador` | `{"de": 3}` | `200 {"coordinador": 3}` si lo acepta. `409 COORDINADOR_RECHAZADO` si `de` es menor que el receptor, que convoca una elección |
| `/cluster/heartbeat` | `{"de": 1, "tareas_en_curso": 0}` | `200 {"coordinador": <mi id>}`. `409 NO_ES_COORDINADOR` si el receptor no es el coordinador: el emisor convoca una elección |
| `/cluster/asignar` | `{"de": 1, "excluir": [3]}` | `200 {"nodo": 2}`: el nodo vivo con menos tareas, fuera de `excluir`. `409 NO_ES_COORDINADOR`; `503 CLUSTER_NO_DISPONIBLE` si no queda ninguno |
| `/cluster/ejecutar` | El de `POST /getRemoteTask` | Lo ejecuta en este nodo: `200 {"resultado", "nodo"}` o el error de la tarea, que el nodo de entrada le devuelve tal cual al cliente. Valida y mira la lista blanca otra vez |

