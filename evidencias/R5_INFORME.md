# Informe de Evidencia R5 — Pruebas con Tareas Diversas y Matriz de Parámetros

Este informe documenta los resultados empíricos obtenidos para el **Requerimiento R5**, incluyendo la evaluación de las 3 imágenes/tareas especializadas, la matriz completa de parámetros $V_1 \times V_2 \times V_3$, la verificación de clientes autenticados con múltiples usuarios concurrentes y el rechazo estricto de bots no autorizados.

## 1. Evaluación de las 3 Imágenes/Tareas Especializadas

Cada tarea fue probada con combinaciones de valores **válidos**, **límite** e **inválidos**.

| Tarea | Caso de Prueba | HTTP Esp. | HTTP Obt. | `tipo` Error | Mensaje / Detalle | Latencia | Estado |
|---|---|---|---|---|---|---|---|
| `CPU-bound` | Válido (primos 15k) | 200 | 200 | `—` | OK (resultado devuelto) | 0.465 s | **OK** |
| `CPU-bound` | Límite (primos 1.000.000, máximo permitido) | 200 | 200 | `—` | OK (resultado devuelto) | 0.763 s | **OK** |
| `CPU-bound` | Inválido (supera límite: 1.000.001) | 422 | 422 | `TAREA_RECHAZADA` | 'limite' supera el máximo permitido (1000000) | 0.686 s | **OK** |
| `CPU-bound` | Inválido (límite negativo) | 422 | 422 | `TAREA_RECHAZADA` | 'limite' debe ser un entero mayor a 1 | 0.681 s | **OK** |
| `Archivos` | Válido (2 archivos con contenido x 2 acciones) | 200 | 200 | `—` | OK (resultado devuelto) | 0.669 s | **OK** |
| `Archivos` | Límite (200 archivos x 5 acciones, tope de contrato) | 200 | 200 | `—` | OK (resultado devuelto) | 0.666 s | **OK** |
| `Archivos` | Inválido (supera tope: 201 archivos) | 422 | 422 | `TAREA_RECHAZADA` | la cantidad de archivos supera el límite permitido (200) | 0.651 s | **OK** |
| `Archivos` | Inválido (tamano_kb 1024 KB > 512 KB máximo) | 422 | 422 | `TAREA_RECHAZADA` | tamano_kb (1024 KB) supera el límite máximo permitido (512 KB) | 0.653 s | **OK** |
| `Archivos` | Inválido (acción inexistente) | 422 | 422 | `TAREA_RECHAZADA` | acción inválida 'accion_falsa'. Permitidas: ['analisis_frecuencia', 'comprimir', 'contar_palabras', 'hash_sha256', 'rotar_lineas'] | 0.667 s | **OK** |
| `Falla / Timeout` | Válido (dormir 0.3s) | 200 | 200 | `—` | OK (resultado devuelto) | 0.762 s | **OK** |
| `Falla / Timeout` | Inválido (división por cero) | 422 | 422 | `TAREA_RECHAZADA` | división por cero | 0.65 s | **OK** |
| `Falla / Timeout` | Falla intencional (explotar 500 -> 502) | 502 | 502 | `TAREA_FALLIDA` | la tarea falló o devolvió una respuesta inválida | 0.458 s | **OK** |
| `Falla / Timeout` | Límite (timeout contractual configurado a 60s) | 504 | 504 | `TAREA_SIN_RESPUESTA` | la tarea no respondió en 60 s (contrato §4 verificado) | 60.1 s | **OK** |

---

## 2. Matriz Obligatoria de Carga y Rendimiento ($V_1 \times V_2 \times V_3$)

Variables evaluadas y diseño experimental:
- **$V_1$ (Archivos de entrada)**: `1`, `10`, `100` archivos. Se envía **contenido real** dentro de cada petición HTTP (respetando el límite de 64 KiB de tamaño de cuerpo estipulado en el contrato público).
- **$V_2$ (Acciones sobre la entrada)**: `1`, `3`, `5` acciones encadenadas. **Justificación de $V_2 = 5$**: Corresponde al agotamiento exhaustivo del catálogo completo de operaciones implementadas en el servicio tarea (`hash_sha256`, `comprimir`, `contar_palabras`, `rotar_lineas`, `analisis_frecuencia`).
- **$V_3$ (Concurrencia de clientes)**: `1`, `10`, `50` clientes simultáneos. Para garantizar rigor estadístico y estabilidad de percentiles, se aplicó una **fase de calentamiento** preliminar y un número consistente de repeticiones (15 muestras en $V_3=1$, 20 en $V_3=10$ y 50 en $V_3=50$).
- **Mezcla de múltiples usuarios y bots**: En todas las celdas se inyectó tráfico legítimo proveniente de múltiples usuarios con tokens de autenticación diferenciados, mezclado con un ~20% de solicitudes de bots (sin token o con credenciales maliciosas).
- **Detección empírica de cuellos de botella**: Se monitoreó el endpoint `/health` en tiempo real mediante un hilo dedicado durante cada celda, registrando los valores reales de `workers_activos` y `tareas_encoladas` del pool.

| $V_1$ (Archivos) | $V_2$ (Acciones) | $V_3$ (Concurrencia) | Latencia p50 | Latencia p95 | Throughput (tpm) | Error Funcional | Diagnóstico Empírico (/health) |
|---|---|---|---|---|---|---|---|
| 1 | 1 | 1 | 0.665 s | 0.681 s | **90.1** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.67 s) |
| 1 | 1 | 10 | 1.529 s | 1.677 s | **297.5** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 1 | 1 | 50 | 4.532 s | 8.116 s | **294.4** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 1 | 3 | 1 | 0.7 s | 0.71 s | **85.7** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.7 s) |
| 1 | 3 | 10 | 1.547 s | 1.669 s | **294.2** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 1 | 3 | 50 | 4.523 s | 8.056 s | **295.6** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 1 | 5 | 1 | 0.692 s | 0.706 s | **86.6** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.69 s) |
| 1 | 5 | 10 | 1.576 s | 1.672 s | **286.7** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 1 | 5 | 50 | 4.509 s | 8.096 s | **295.0** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 10 | 1 | 1 | 0.703 s | 0.721 s | **85.0** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.7 s) |
| 10 | 1 | 10 | 1.543 s | 1.719 s | **291.2** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 10 | 1 | 50 | 4.536 s | 8.18 s | **290.6** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 10 | 3 | 1 | 0.72 s | 0.735 s | **83.4** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.72 s) |
| 10 | 3 | 10 | 1.608 s | 1.782 s | **280.1** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 10 | 3 | 50 | 4.613 s | 8.341 s | **286.5** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 10 | 5 | 1 | 0.729 s | 0.738 s | **82.2** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.73 s) |
| 10 | 5 | 10 | 1.723 s | 1.84 s | **270.1** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 10 | 5 | 50 | 4.797 s | 8.547 s | **279.5** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 100 | 1 | 1 | 0.74 s | 0.752 s | **81.0** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.74 s) |
| 100 | 1 | 10 | 1.678 s | 1.855 s | **272.3** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 100 | 1 | 50 | 4.729 s | 8.56 s | **279.4** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 100 | 3 | 1 | 0.745 s | 0.756 s | **80.3** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.74 s) |
| 100 | 3 | 10 | 1.669 s | 1.852 s | **271.4** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 100 | 3 | 50 | 4.814 s | 8.571 s | **277.5** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 100 | 5 | 1 | 0.755 s | 0.772 s | **79.5** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.75 s) |
| 100 | 5 | 10 | 1.624 s | 1.825 s | **272.9** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 100 | 5 | 50 | 4.795 s | 8.535 s | **277.9** | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |

---

## 3. Análisis Técnico y Respuestas a los Puntos de Evaluación

### 3.1 Contenido real en $V_1$ vs costo del contenedor
Cada archivo viaja con su contenido en el cuerpo JSON de la petición. Tamaño medido del cuerpo con $V_2 = 1$: 331 bytes para $V_1 = 1$, 2005 para $V_1 = 10$ y 18745 para $V_1 = 100$ (el contrato admite hasta 64 KiB).

Con un solo cliente ($V_3 = 1$) la latencia p50 pasa de 0.665 s ($V_1 = 1$, $V_2 = 1$) a 0.755 s ($V_1 = 100$, $V_2 = 5$). El piso medido, 0.67 s, es el de crear, arrancar, esperar y borrar el contenedor efímero: ese costo domina y el procesamiento de los archivos agrega poco sobre él. Por eso, con $V_3 = 10$ y $V_3 = 50$ la latencia depende sobre todo de la cola y casi no de $V_1$ ni de $V_2$. No se midió por separado cuánto tarda la tarea dentro del contenedor, así que el reparto exacto entre ambos costos es una inferencia.

### 3.2 Justificación de $V_2 = 5$ (Acciones)
El servicio de procesamiento de archivos define 5 transformaciones permitidas en su contrato: `hash_sha256`, `comprimir`, `contar_palabras`, `rotar_lineas` y `analisis_frecuencia`. Los valores seleccionados evalúan: (1) transformación unitaria de referencia, (3) pipeline intermedio representativo, y (5) el pipeline exhaustivo ($N = 5$) que agota la totalidad de acciones disponibles en el servicio, sometiendo al worker a la máxima profundidad de procesamiento por documento.

### 3.3 Muestras y calentamiento
1. **Calentamiento**: antes de la matriz se envían pedidos preliminares para que la imagen ya esté en el Docker local.
2. **Muestras por celda**: 15 pedidos con $V_3 = 1$, 20 con $V_3 = 10$ y 50 con $V_3 = 50$, de los cuales un 20 % los manda un bot (el resto, usuarios con token). Con tan pocas muestras, el p95 es el valor cercano al máximo de la celda y una sola corrida no permite medir la variación entre corridas; los números valen para comparar celdas entre sí en una misma corrida, no como valores absolutos. Los pedidos de bots no cuentan para p50, p95 ni throughput.

### 3.4 Autenticación y bots
- Pedidos emitidos por bots: **153**, en todas las celdas.
- Rechazados con `401 NO_AUTORIZADO`: **153/153 (100.0%)**.
- Latencia media de esos rechazos, medida por el cliente (incluye la red): **14.32 ms**.
- Los usuarios legítimos usan varios tokens distintos (los de `TP2_API_TOKEN`, separados por comas), no uno compartido.
- La comprobación está en un `@app.middleware('http')` de FastAPI: un pedido sin token válido se rechaza antes de llegar a la cola y sin crear ningún contenedor.

> [!NOTE]
> **Alcance**: esta autenticación sirve para separar bots y medir la cola de R5. No tiene roles, vencimiento ni `/stats`: eso corresponde a R4.

### 3.5 Cuello de botella medido en `/health`
Durante cada celda un hilo consulta `/health` cada 30 ms y guarda el máximo de `workers_activos` y de `tareas_encoladas`. Máximos observados en toda la matriz:

| $V_3$ | Workers activos (de 4) | Tareas encoladas |
|---|---|---|
| 1 | 1 | 0 |
| 10 | 4 | 4 |
| 50 | 4 | 35 |

- Con $V_3 = 1$ no hay cola: el cuello de botella es el ciclo de vida del contenedor (~0.67 s por tarea).
- Con $V_3 = 10$ y $V_3 = 50$ el pool llega a su tope y el resto espera en la cola: el cuello de botella es el número de workers (`TP2_WORKERS_MAX`). En esas celdas la latencia crece con la cola y el throughput deja de crecer.
- **No medido**: si el daemon de Docker se vuelve un segundo cuello con muchos contenedores a la vez es una hipótesis; esta corrida no la verifica.

### 3.6 Entorno de Ejecución y Timeout Contractual
- **Timeout Contractual**: El servidor se ejecuta respetando `TP2_TIMEOUT_EJECUCION = 60` segundos (estipulado en el contrato público).
- **Despliegue vs Local**: Las pruebas pueden ejecutarse localmente mediante Docker Compose o directamente contra el despliegue remoto en AWS EC2 (`http://18.231.127.74:8082`) suministrando la variable `TP2_SERVIDOR`.

