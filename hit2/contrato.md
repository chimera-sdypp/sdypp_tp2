# Contrato — Hit 2 (servidor de tareas remotas con Concurrencia y Relojes Lógicos)

Define qué recibe y qué devuelve el servidor en el Hit #2. Extiende el contrato del Hit #1 agregando soporte para **Pool de Workers con Exclusión Mutua** y **Relojes Lógicos de Lamport [LAM78]**.

## 1. El sobre y Relojes Lógicos

Toda respuesta es JSON y mantiene el mismo sobre que en Hit #1, incorporando además el timestamp de Lamport del servidor tanto en la cabecera HTTP `X-Lamport-Clock` como en la clave `lamport_ts` dentro del `contenido`:

```json
{"codigo": 200, "contenido": {"calculo": "suma", "resultado": 7, "lamport_ts": 5}}
{"codigo": 403, "contenido": {"error": {"tipo": "IMAGEN_NO_PERMITIDA", "mensaje": "…", "detalles": []}}}
```

| Clave / Cabecera | Qué es |
|---|---|
| `X-Lamport-Clock` (HTTP Header) | Timestamp del Reloj Lógico de Lamport del emisor |
| `codigo` | Código de estado HTTP |
| `contenido` | Objeto con el resultado de la tarea (o error) y el timestamp `lamport_ts` |

### Regla de actualización de Lamport
- Al recibir una solicitud con la cabecera `X-Lamport-Clock: L_cliente`, el servidor actualiza su reloj local:  
  $$L_{servidor} = \max(L_{servidor}, L_{cliente}) + 1$$
- Al enviar la respuesta, el servidor vuelve a incrementar su reloj ($L_{respuesta} = L_{servidor} + 1$) y lo adjunta en `X-Lamport-Clock` y `contenido.lamport_ts`.

---

## 2. Operaciones

| Método y ruta | Qué hace |
|---|---|
| `POST /getRemoteTask` | Encola la tarea en el **Pool de Workers (con exclusión mutua)**. Cuando hay un worker libre, ejecuta el contenedor efímero y devuelve el resultado |
| `GET /health` | Estado público del servidor, Docker y el Pool de Workers |

### `POST /getRemoteTask`

Cabeceras obligatorias:
- `Content-Type: application/json`
- `X-Lamport-Clock: <entero>` (opcional, por defecto `0`)

```json
{
  "calculo": "suma",
  "parametros": {"a": 3, "b": 4},
  "datos": {},
  "imagen": "cerberusdistribuido/tarea:1.0.0"
}
```

---

### `GET /health`

```json
{
  "codigo": 200,
  "contenido": {
    "servidor": "ok",
    "docker": "ok",
    "pool": {
      "workers_activos": 2,
      "workers_max": 4,
      "tareas_encoladas": 0
    },
    "lamport_ts": 12
  }
}
```

`200` si el servidor y Docker están operativos. `503` si el daemon de Docker no responde (`"docker": "caido"`).

---

## 3. Concurrencia y Pool de Workers

- **Límite configurable**: `TP2_WORKERS_MAX` (por defecto `4`).
- **Exclusión mutua**: La cola de tareas está protegida por un **Mutex**. Toda asignación de tareas a un worker se realiza en una sección crítica para evitar race conditions.
- **Orden de la cola**: Las tareas en espera se despachan ordenadas por menor **timestamp de Lamport del cliente** (el `X-Lamport-Clock` del pedido, que marca el evento de envío), y FIFO en caso de empate. No se usa el reloj del servidor al recibir: ese crece con cada llegada y daría el mismo orden que la llegada.

---

## 4. Lista blanca de imágenes

`TP2_IMAGENES_PERMITIDAS` es una lista de repositorios separados por comas
(`cerberusdistribuido/tarea`). Los nombres se normalizan antes de comparar:
`cerberusdistribuido/tarea` ≡ `docker.io/cerberusdistribuido/tarea`. Si la variable está vacía no
se permite **ninguna** imagen.

---

## 5. Lo que el servidor espera del servicio tarea

Lo implementa [`tarea/tarea.py`](tarea/tarea.py). Usa **el mismo sobre** que el servidor (§1):
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

