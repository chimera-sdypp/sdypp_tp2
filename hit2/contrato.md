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
  "imagen": "cerberus/tarea:1.0.0"
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
- **Orden de la cola**: Las tareas en espera se despachan ordenadas por menor **timestamp de Lamport** (y FIFO en caso de empate).
