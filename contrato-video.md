# Contrato — Transcodificación de video (Hits 2 y 3)

> **Borrador para acordar.** Las decisiones que todavía están abiertas están al final, en
> [Para acordar](#8-para-acordar).

Le mandás un video y las calidades que querés, y el servidor te devuelve una versión del video en
cada calidad. Transcodificar puede tardar desde segundos hasta decenas de minutos, así que el
pedido es **asíncrono**: el servidor acepta el trabajo enseguida, te da un `id`, y vos consultás
con ese `id` hasta que esté listo.

**Es el mismo contrato en el Hit 2 y en el Hit 3**: mismas rutas, mismos campos, mismos límites y
mismos errores. Que atrás haya un servidor o un cluster de 3 nodos no cambia nada de lo que ve el
cliente.

```
POST /videos                  →  202  {id, estado: "en_cola"}
GET  /videos/{id}             →  200  {estado: "en_cola" | "procesando" | "listo" | "error", …}
GET  /videos/{id}/{calidad}   →  200  el video (video/mp4), cuando el estado es "listo"
```

## 1. Lo común

- **Autenticación:** igual que `/getRemoteTask`. Todo pedido lleva
  `Authorization: Bearer <JWT>` (HS256, con `sub`, `role` y `exp`). Sin token, o con un token
  inválido o vencido: `401 NO_AUTORIZADO`.
- **Cada trabajo es de quien lo creó** (el `sub` del token). Si otro usuario consulta ese `id`,
  recibe `404`, igual que si el trabajo no existiera. El rol `admin` puede ver todos.
- **El sobre** es el de siempre, `{"codigo", "contenido"}`, salga bien o mal. La única excepción es
  la descarga del video: si sale bien, el cuerpo es el archivo `video/mp4`. Si falla, vuelve a ser el
  sobre JSON.
- `X-Lamport-Clock` funciona como en el resto de las rutas: es opcional en el pedido y viene siempre
  en la respuesta.

## 2. Calidades

`calidades` es una lista de alturas en píxeles, sin repetidos, con al menos una:

| Calidad | Resolución de salida (16:9) | Video (H.264) | CRF | Audio (AAC) |
|---|---|---|---|---|
| `1080` | 1920×1080 | hasta 5 Mb/s | 23 | 128 kb/s |
| `720` | 1280×720 | hasta 3 Mb/s | 23 | 128 kb/s |
| `480` | 854×480 | hasta 1,5 Mb/s | 24 | 96 kb/s |
| `360` | 640×360 | hasta 800 kb/s | 26 | 96 kb/s |
| `240` | 426×240 | hasta 400 kb/s | 28 | 64 kb/s |

- **Se respeta la relación de aspecto del original:** se fija la altura y el ancho se calcula. Un
  video 4:3 en `720` sale de 960×720.
- **Nunca se agranda:** si pedís una calidad mayor que la altura del original, el pedido se rechaza
  enseguida (`422 CALIDAD_SUPERIOR_AL_ORIGINAL`) y el mensaje dice cuál es la altura del original.
- La salida siempre es MP4 (H.264 + AAC) con los fps del original. Si el original no tiene audio, la
  salida tampoco.

Cómo se traduce cada calidad a ffmpeg (es informativo, no hace falta para usar la API):
`-map 0:v:0 -map 0:a:0? -vf scale=-2:<altura> -c:v libx264 -preset veryfast -crf <CRF>
-maxrate <video> -bufsize <2×video> -pix_fmt yuv420p -c:a aac -b:a <audio> -movflags +faststart`.
`scale=-2` calcula el ancho manteniendo la proporción y lo redondea a par, que es lo que pide
H.264. El `?` en `-map 0:a:0?` hace que un original sin audio no sea un error.

## 3. Límites

| Límite | Valor | Si se supera |
|---|---|---|
| Tamaño del video | 1 GiB (1 073 741 824 bytes) | `413 VIDEO_DEMASIADO_GRANDE`, sin esperar a que termine la subida |
| Duración | 15 min (900 s) | `422 VIDEO_DEMASIADO_LARGO` |
| Resolución de entrada | hasta 3840×2160 (4K) | `422 RESOLUCION_NO_SOPORTADA` |
| Trabajos activos (en cola o procesando) por usuario | 3 | `429 DEMASIADOS_TRABAJOS` |
| Trabajos en cola, sumando todos los usuarios | 20 | `503 SERVICIO_SATURADO`, con `Retry-After` |
| Transcodificaciones en paralelo | 2 | No es un error: el resto espera en la cola |
| Tiempo máximo de procesamiento de un trabajo | 60 min | El trabajo termina en `error` con `TIEMPO_AGOTADO` |
| Cuánto se guarda el resultado | 24 h desde que terminó | Después de eso el trabajo se borra y su `id` da `404` |

**Todo lo que se puede validar antes de aceptar el trabajo se valida antes.** Si el pedido supera un
límite, o el archivo no es un video, o una calidad no es válida, la respuesta es un 4xx inmediato, en
el mismo `POST`. El trabajo no llega a crearse. Un `202` quiere decir que el video es válido y que
el trabajo **va** a procesarse. A partir de ahí, solo puede terminar en `error` por algo que pase
durante la transcodificación (§6).

El tamaño se controla con el `Content-Length`, antes de leer el cuerpo. Si el pedido no trae
`Content-Length` (es chunked), la subida se corta apenas pasa el límite.

## 4. `POST /videos`: crear un trabajo

Hay dos formas de mandar el video. **La del archivo es la de la primera entrega.** La de la URL
viene después.

### Con el archivo: `multipart/form-data`

| Campo | Obligatorio | Qué es |
|---|---|---|
| `video` | sí | El archivo de video |
| `calidades` | sí | Las calidades separadas por comas: `720,480` |

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  -F video=@bbb_1080p.mp4 -F calidades=720,480 \
  http://localhost:8082/videos
```

### Con una URL: `application/json` (segunda etapa)

```json
{"url": "https://ejemplo.com/video.mp4", "calidades": [720, 480]}
```

- La URL tiene que ser `http` o `https`, y apuntar a una dirección pública. Las direcciones locales o
  privadas (`localhost`, `10.x`, `192.168.x`, `169.254.x`…) se rechazan: si no, el servidor se podría
  usar para leer servicios internos (SSRF).
- Antes de aceptar el trabajo, el servidor consulta la URL. Si no responde o no hay un video ahí,
  devuelve `422 URL_INACCESIBLE`. También lee los datos del video, así que duración, resolución y
  tamaño se validan en ese mismo momento. La descarga completa se hace después, como parte del trabajo.

En los dos casos hay que mandar el video de una sola forma: archivo **o** URL, no los dos.

### Respuesta: `202 Accepted`

Viene con la cabecera `Location: /videos/{id}`.

```json
{"codigo": 202, "contenido": {
  "id": "3f9c2a7e5b1d4c8e9a0f6b2d1e7c4a93",
  "estado": "en_cola",
  "posicion_en_cola": 2,
  "calidades": [720, 480],
  "original": {"nombre": "bbb_1080p.mp4", "bytes": 427023749, "duracion_s": 634.57,
               "ancho": 1920, "alto": 1080},
  "consultar": "/videos/3f9c2a7e5b1d4c8e9a0f6b2d1e7c4a93",
  "lamport_ts": 41}}
```

## 5. `GET /videos/{id}`: consultar un trabajo

Consultar cada 5 a 10 segundos es suficiente. Mientras el trabajo no termina, la respuesta trae
`Retry-After` con los segundos que conviene esperar antes de volver a preguntar.

| `estado` | Qué quiere decir | Campos que importan |
|---|---|---|
| `en_cola` | Esperando que se libere un lugar | `posicion_en_cola` (1 = el próximo) |
| `procesando` | Transcodificando | `progreso`, de 0 a 100. Es aproximado y puede ser `null` al empezar |
| `listo` | Terminó bien | `resultados`: una entrada por calidad, con su link de descarga |
| `error` | Terminó mal | `error`: `{tipo, mensaje}` (§6) |

`listo` y `error` son estados finales: el trabajo ya no cambia.

```json
{"codigo": 200, "contenido": {
  "id": "3f9c2a7e5b1d4c8e9a0f6b2d1e7c4a93",
  "estado": "listo",
  "progreso": 100,
  "calidades": [720, 480],
  "original": {"nombre": "bbb_1080p.mp4", "bytes": 427023749, "duracion_s": 634.57,
               "ancho": 1920, "alto": 1080},
  "resultados": {
    "720": {"ancho": 1280, "alto": 720, "bytes": 98765432, "descargar": "/videos/3f9c…/720"},
    "480": {"ancho": 854,  "alto": 480, "bytes": 45678901, "descargar": "/videos/3f9c…/480"}
  },
  "tiempos": {"creado": "2026-10-20T12:00:00Z", "inicio": "2026-10-20T12:00:03Z",
              "fin": "2026-10-20T12:07:41Z", "en_cola_s": 3.1, "procesamiento_s": 458.2},
  "expira": "2026-10-21T12:07:41Z",
  "error": null,
  "lamport_ts": 97}}
```

### `GET /videos/{id}/{calidad}`: descargar

Responde `200`, con `Content-Type: video/mp4` y
`Content-Disposition: attachment; filename="<nombre>_<calidad>p.mp4"`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" -o bbb_720p.mp4 \
  http://localhost:8082/videos/3f9c2a7e5b1d4c8e9a0f6b2d1e7c4a93/720
```

Si el trabajo todavía no terminó: `409 RESULTADO_NO_LISTO`. Si esa calidad no se pidió:
`404 CALIDAD_NO_SOLICITADA`.

## 6. Errores

### Al crear o consultar (código HTTP de la respuesta)

| Código | `tipo` | Cuándo |
|---|---|---|
| 400 | `CUERPO_VACIO`, `JSON_INVALIDO` | No hay cuerpo, o el JSON está mal formado |
| 401 | `NO_AUTORIZADO` | Sin token, o con un token inválido o vencido |
| 404 | `TRABAJO_INEXISTENTE` | El `id` no existe, es de otro usuario o ya venció |
| 404 | `CALIDAD_NO_SOLICITADA` | Se pidió descargar una calidad que no estaba en el pedido |
| 409 | `RESULTADO_NO_LISTO` | Se pidió la descarga antes de que el trabajo esté `listo` |
| 413 | `VIDEO_DEMASIADO_GRANDE` | El video pesa más de 1 GiB |
| 415 | `TIPO_DE_CONTENIDO` | El pedido no es `multipart/form-data` ni `application/json` |
| 422 | `PAYLOAD_INVALIDO` | Falta `video` o `url`, vinieron los dos, hay un campo de más, o `calidades` está vacía, repetida o trae un valor que no está en la tabla. `detalles` dice qué campo falló |
| 422 | `ARCHIVO_NO_ES_VIDEO` | El archivo no se puede leer como video: no tiene pista de video, está truncado o es otro tipo de archivo |
| 422 | `VIDEO_DEMASIADO_LARGO` | Dura más de 15 min |
| 422 | `RESOLUCION_NO_SOPORTADA` | La entrada supera 3840×2160 |
| 422 | `CALIDAD_SUPERIOR_AL_ORIGINAL` | Se pidió una calidad mayor que la altura del original |
| 422 | `URL_INVALIDA` | La URL no es `http`/`https`, o apunta a una dirección privada |
| 422 | `URL_INACCESIBLE` | La URL no responde, devuelve un error o no hay un video ahí |
| 429 | `DEMASIADOS_TRABAJOS` | El usuario ya tiene 3 trabajos activos |
| 429 | `DEMASIADOS_PEDIDOS` | Superó el límite de pedidos por minuto (el mismo de `/getRemoteTask`). Viene con `Retry-After` |
| 500 | `ERROR_INTERNO` | Error inesperado del servidor |
| 503 | `SERVICIO_SATURADO` | La cola está llena (20 trabajos). Viene con `Retry-After`: reintentá más tarde |
| 503 | `SERVICIO_NO_DISPONIBLE` | El servidor no puede procesar videos en este momento |

### Durante el procesamiento (`estado: "error"`, en `contenido.error`)

| `tipo` | Qué pasó | Qué hacer |
|---|---|---|
| `TRANSCODIFICACION_FALLIDA` | ffmpeg falló a mitad de camino: por ejemplo, un archivo dañado hacia el final | Revisar el video |
| `TIEMPO_AGOTADO` | El procesamiento pasó los 60 min | Pedir menos calidades o mandar un video más corto |
| `DESCARGA_FALLIDA` | Solo con URL: la descarga se cortó o el contenido cambió | Volver a mandarlo |
| `TRABAJO_PERDIDO` | El servidor se reinició, o se cayó el nodo que procesaba el trabajo | Volver a mandarlo: nada se reintenta solo |

## 7. `GET /health`

Sigue siendo pública. Además de lo que ya tiene cada hit, suma una sección `videos`, que es la misma
en los dos hits:

```json
"videos": {"en_cola": 3, "procesando": 2, "max_simultaneos": 2, "max_en_cola": 20}
```

## 8. Para acordar

1. **El cliente no elige la imagen.** En `/getRemoteTask` el cliente manda `imagen`. Acá la imagen de
   ffmpeg la fija el servidor (`cerberusdistribuido/tarea-video:1.0.0`), para que el cliente no tenga
   que conocerla. ¿Les sirve así o prefieren mandarla?
2. **Pedir una calidad mayor que el original es un rechazo** (`422`), no una calidad salteada. Es más
   simple de probar. La otra opción es generar las calidades que se puedan y marcar las demás como
   `omitida`.
3. **Los números de los límites** (§3) están pensados para que los videos de prueba de 10 min, de
   240p a 4K, entren todos. ¿Les cierran?
4. **`calidades` en multipart** va como `720,480`, y en JSON como lista `[720, 480]`.
5. **No hay `DELETE /videos/{id}`** (cancelar o borrar) ni `GET /videos` (listar los trabajos propios)
   en esta versión.

---

## Anexo (interno): el servidor y la imagen `tarea-video`

No hace falta para usar la API. Es para los que implementan el servidor y la imagen.

- **La imagen sigue el mismo contrato que el servicio tarea** (`hit2/contrato.md` §5): HTTP en el
  puerto `8080`, `GET /health` y `POST /ejecutarTarea` con el sobre de siempre. Base
  `debian:trixie-slim` con `ffmpeg` y `python3`, y el servidor HTTP de la biblioteca estándar
  (`ThreadingHTTPServer`, igual que `tarea/tarea.py`): sin pip. Es una sola imagen para el Hit 2 y el
  Hit 3, así que va en `tarea-video/`, en la raíz del repo.
- **El mismo usuario en las dos imágenes: UID 10001.** Servidor y tarea leen y escriben los mismos
  archivos (el servidor escribe `entrada`, la tarea escribe `salida/` y el servidor la lee). Si los
  UID no coinciden, alguno de los dos no puede leer o escribir. Por la misma razón, para probar la
  tarea sola con una carpeta del host montada, esa carpeta tiene que ser del UID 10001
  (`sudo chown -R 10001 <carpeta>`).
- **El video no viaja por HTTP.** Servidor y tarea comparten el volumen `tp2-videos`, montado en
  `/trabajos`. Cada trabajo es una carpeta `/trabajos/<id>/` con `entrada`, `salida/`, `estado.json`
  y `progreso.json`. En el Hit 3 los tres nodos montan el mismo volumen, y por eso cualquier nodo
  puede contestar el `GET` de cualquier trabajo.
- **El pedido a la tarea:**
  ```json
  {"calculo": "transcodificar",
   "parametros": {"calidades": [720, 480]},
   "datos": {"entrada": "/trabajos/<id>/entrada", "salida": "/trabajos/<id>/salida",
             "progreso": "/trabajos/<id>/progreso.json", "duracion_s": 634.57}}
  ```
  `duracion_s` la manda el servidor, que ya la midió con `ffprobe`. La tarea la usa para calcular el
  progreso y no necesita volver a medirla.
- **Lo que hace la tarea:** genera todas las calidades con **un solo ffmpeg**, que decodifica una
  vez y codifica varias salidas, con los argumentos de §2. Con `-progress pipe:1` lee `out_time_us`
  y va escribiendo `{"progreso": <0-100>}` en `progreso.json` como máximo una vez por segundo. Para
  que el servidor nunca lea un archivo a medio escribir, escribe en un `.tmp` y lo renombra con
  `os.replace`. Al final responde
  `{"resultado": {"720": {"archivo": "720.mp4", "bytes": …, "ancho": …, "alto": …}, …}}`. Si ffmpeg
  termina con un código distinto de 0, responde `422` con las últimas líneas del stderr en `mensaje`.
- **Los límites del contenedor no son los de `/getRemoteTask`.** El timeout de la llamada es
  `TP2_VIDEO_TIMEOUT` (60 min), y la memoria y la CPU son `TP2_VIDEO_MEM_LIMIT` (2g) y
  `TP2_VIDEO_CPUS` (2). Con los 256 MB y la CPU de las tareas de cálculo, un ffmpeg con entrada 4K se
  queda sin memoria. Los 60 s siguen valiendo solo para los cálculos.
- **La tarea usa tantos hilos como CPU le da Docker.** Los lee del cgroup (`/sys/fs/cgroup/cpu.max`),
  no del host. Si ffmpeg usara los 16 núcleos del host, un 4K a 5 calidades pasaría de 1 GiB. Con 2
  hilos el pico medido es de 755 MiB.
- **La imagen tiene que estar en `TP2_IMAGENES_PERMITIDAS`**, como cualquier otra: el servidor la
  valida y la baja con el mismo código de R1/R2.
- **Al arrancar, el servidor revisa `/trabajos`.** Los trabajos que estaban `en_cola` o `procesando`
  pasan a `error` con `TRABAJO_PERDIDO`: nada se reintenta solo. Las carpetas sin `estado.json` son
  subidas que no terminaron, y se borran. Los trabajos que terminaron hace más de 24 h también se
  borran. En el Hit 3, `estado.json` anota qué nodo procesa cada trabajo, y un nodo que arranca solo
  marca como perdidos los suyos: los de los nodos que siguen vivos no se tocan.
- **El servidor valida con `ffprobe`** (lo trae su imagen) antes de responder el `202`: que haya una
  pista de video, la duración, la resolución y que las calidades no superen el original. Así los
  rechazos son inmediatos y la tarea solo recibe videos que ya pasaron la validación.
