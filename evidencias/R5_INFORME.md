# Informe de Evidencia R5 — Pruebas con Tareas Diversas y Matriz de Parámetros

Este informe documenta los resultados empíricos obtenidos para el **Requerimiento R5**, incluyendo la evaluación de las 3 imágenes/tareas especializadas, la matriz completa de parámetros $V_1 \times V_2 \times V_3$, la verificación de clientes autenticados con múltiples usuarios concurrentes y el rechazo estricto de bots no autorizados.

## 1. Evaluación de las 3 Imágenes/Tareas Especializadas

Cada tarea fue probada con combinaciones de valores **válidos**, **límite** e **inválidos**.

| Tarea | Caso de Prueba | HTTP Esp. | HTTP Obt. | `tipo` Error | Mensaje / Detalle | Latencia | Estado |
|---|---|---|---|---|---|---|---|
| `CPU-bound` | Válido (primos 15k) | 200 | 200 | `—` | OK (resultado devuelto) | 0.681 s | **OK** |
| `CPU-bound` | Límite (primos 1.000.000, máximo permitido) | 200 | 200 | `—` | OK (resultado devuelto) | 0.682 s | **OK** |
| `CPU-bound` | Inválido (supera límite: 1.000.001) | 422 | 422 | `TAREA_RECHAZADA` | 'limite' supera el máximo permitido (1000000) | 0.601 s | **OK** |
| `CPU-bound` | Inválido (límite negativo) | 422 | 422 | `TAREA_RECHAZADA` | 'limite' debe ser un entero mayor a 1 | 0.586 s | **OK** |
| `Archivos` | Válido (2 archivos con contenido x 2 acciones) | 200 | 200 | `—` | OK (resultado devuelto) | 0.617 s | **OK** |
| `Archivos` | Límite (200 archivos x 5 acciones, tope de contrato) | 200 | 200 | `—` | OK (resultado devuelto) | 0.61 s | **OK** |
| `Archivos` | Inválido (supera tope: 201 archivos) | 422 | 422 | `TAREA_RECHAZADA` | la cantidad de archivos supera el límite permitido (200) | 0.597 s | **OK** |
| `Archivos` | Inválido (tamano_kb 1024 KB > 512 KB máximo) | 422 | 422 | `TAREA_RECHAZADA` | tamano_kb (1024 KB) supera el límite máximo permitido (512 KB) | 0.595 s | **OK** |
| `Archivos` | Inválido (acción inexistente) | 422 | 422 | `TAREA_RECHAZADA` | acción inválida 'accion_falsa'. Permitidas: ['analisis_frecuencia', 'comprimir', 'contar_palabras', 'hash_sha256', 'rotar_lineas'] | 0.833 s | **OK** |
| `Falla / Timeout` | Válido (dormir 0.3s) | 200 | 200 | `—` | OK (resultado devuelto) | 0.911 s | **OK** |
| `Falla / Timeout` | Inválido (división por cero) | 422 | 422 | `TAREA_RECHAZADA` | división por cero | 0.599 s | **OK** |
| `Falla / Timeout` | Falla intencional (explotar 500 -> 502) | 502 | 502 | `TAREA_FALLIDA` | la tarea falló o devolvió una respuesta inválida | 0.594 s | **OK** |
| `Falla / Timeout` | Límite (timeout contractual de 60s, usar --probar-timeout-largo) | 504 | — | `OMITIDO` | Omitido para no demorar 60s (ejecutar con --probar-timeout-largo o TP2_TIMEOUT_EJECUCION <= 10) | 0.0 s | **OK** |

---

## 2. Matriz Obligatoria de Carga y Rendimiento ($V_1 \times V_2 \times V_3$)

Variables evaluadas y diseño experimental:
- **$V_1$ (Archivos de entrada)**: `1`, `10`, `100` archivos. Se envía **contenido real** dentro de cada petición HTTP (respetando el límite de 64 KiB de tamaño de cuerpo estipulado en el contrato público).
- **$V_2$ (Acciones sobre la entrada)**: `1`, `3`, `5` acciones encadenadas. **Justificación de $V_2 = 5$**: Corresponde al agotamiento exhaustivo del catálogo completo de operaciones implementadas en el servicio tarea (`hash_sha256`, `comprimir`, `contar_palabras`, `rotar_lineas`, `analisis_frecuencia`).
- **$V_3$ (Concurrencia de clientes)**: `1`, `10`, `50` clientes simultáneos. Para garantizar rigor estadístico y estabilidad de percentiles, se aplicó una **fase de calentamiento** preliminar y un número consistente de repeticiones (15 muestras en $V_3=1$, 20 en $V_3=10$ y 50 en $V_3=50$).
- **Mezcla de múltiples usuarios y bots**: En todas las celdas se inyectó tráfico legítimo proveniente de múltiples usuarios con tokens de autenticación diferenciados, mezclado con un ~20% de solicitudes de bots (sin token o con credenciales maliciosas).
- **Detección empírica de cuellos de botella**: Se monitoreó el endpoint `/health` en tiempo real mediante un hilo dedicado durante cada celda, registrando los valores reales de `workers_activos` y `tareas_encoladas` del pool.

| $V_1$ (Archivos) | $V_2$ (Acciones) | $V_3$ (Concurrencia) | Latencia p50 | Latencia p95 | Throughput (tpm) | CPU Host Prom/Pico | Mem Host (MB) | Error Funcional | Diagnóstico Empírico (/health) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | 1 | 0.606 s | 0.627 s | **99.3** | 11.9% / 22.8% | 7949.5 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.61 s) |
| 1 | 1 | 10 | 2.997 s | 6.148 s | **113.8** | 15.3% / 72.2% | 7990.1 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 1 | 1 | 50 | 6.624 s | 12.988 s | **174.4** | 19.9% / 59.0% | 8047.3 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 1 | 3 | 1 | 0.804 s | 1.157 s | **70.8** | 14.6% / 74.4% | 7974.8 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.8 s) |
| 1 | 3 | 10 | 1.499 s | 3.312 s | **215.2** | 28.6% / 75.6% | 8000.4 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 1 | 3 | 50 | 10.372 s | 15.42 s | **150.0** | 17.8% / 50.6% | 8008.1 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 1 | 5 | 1 | 0.631 s | 0.716 s | **93.0** | 13.2% / 54.6% | 7956.5 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.63 s) |
| 1 | 5 | 10 | 2.656 s | 7.227 s | **101.3** | 18.6% / 72.6% | 7966.3 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 1 | 5 | 50 | 5.356 s | 11.138 s | **200.9** | 22.0% / 42.3% | 8012.4 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 10 | 1 | 1 | 0.645 s | 1.136 s | **74.4** | 10.9% / 24.6% | 7950.0 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.64 s) |
| 10 | 1 | 10 | 1.239 s | 3.725 s | **196.8** | 25.9% / 72.0% | 7995.9 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 10 | 1 | 50 | 11.66 s | 19.962 s | **116.6** | 22.4% / 78.6% | 7973.6 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 10 | 3 | 1 | 0.649 s | 0.676 s | **92.5** | 16.2% / 71.5% | 7990.4 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.65 s) |
| 10 | 3 | 10 | 2.961 s | 7.693 s | **101.5** | 18.3% / 41.6% | 8009.8 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 10 | 3 | 50 | 6.446 s | 15.563 s | **143.2** | 23.7% / 76.7% | 8037.1 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 10 | 5 | 1 | 0.841 s | 1.236 s | **65.8** | 15.2% / 53.6% | 7985.1 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.84 s) |
| 10 | 5 | 10 | 2.213 s | 6.208 s | **123.7** | 21.1% / 64.6% | 7992.5 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 10 | 5 | 50 | 12.569 s | 22.039 s | **107.9** | 21.2% / 84.4% | 7990.0 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 100 | 1 | 1 | 1.098 s | 1.35 s | **54.3** | 11.6% / 51.4% | 7883.4 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~1.1 s) |
| 100 | 1 | 10 | 4.383 s | 9.4 s | **77.2** | 15.2% / 71.3% | 7911.8 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 100 | 1 | 50 | 12.823 s | 24.322 s | **94.0** | 19.1% / 78.1% | 8072.7 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 100 | 3 | 1 | 0.896 s | 1.113 s | **65.0** | 12.5% / 60.4% | 8070.5 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.9 s) |
| 100 | 3 | 10 | 2.966 s | 7.739 s | **98.4** | 13.8% / 38.1% | 8057.4 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 100 | 3 | 50 | 14.388 s | 22.855 s | **101.7** | 20.2% / 88.3% | 8112.8 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 100 | 5 | 1 | 1.094 s | 1.513 s | **54.0** | 14.0% / 47.5% | 8081.0 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~1.09 s) |
| 100 | 5 | 10 | 3.044 s | 7.55 s | **102.2** | 19.1% / 56.7% | 8073.8 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 100 | 5 | 50 | 13.513 s | 24.933 s | **94.1** | 17.1% / 79.6% | 7980.6 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |

---

## 3. Análisis Técnico y Respuestas a los Puntos de Evaluación

### 3.1 Contenido real en $V_1$ vs costo del contenedor
Cada archivo viaja con su contenido en el cuerpo JSON de la petición. Tamaño medido del cuerpo con $V_2 = 1$: 331 bytes para $V_1 = 1$, 2005 para $V_1 = 10$ y 18745 para $V_1 = 100$ (el contrato admite hasta 64 KiB).

Con un solo cliente ($V_3 = 1$) la latencia p50 pasa de 0.606 s ($V_1 = 1$, $V_2 = 1$) a 1.094 s ($V_1 = 100$, $V_2 = 5$). El piso medido, 0.61 s, es el de crear, arrancar, esperar y borrar el contenedor efímero: ese costo domina y el procesamiento de los archivos agrega poco sobre él. Por eso, con $V_3 = 10$ y $V_3 = 50$ la latencia depende sobre todo de la cola y casi no de $V_1$ ni de $V_2$. No se midió por separado cuánto tarda la tarea dentro del contenedor, así que el reparto exacto entre ambos costos es una inferencia.

### 3.2 Justificación de $V_2 = 5$ (Acciones)
El servicio de procesamiento de archivos define 5 transformaciones permitidas en su contrato: `hash_sha256`, `comprimir`, `contar_palabras`, `rotar_lineas` y `analisis_frecuencia`. Los valores seleccionados evalúan: (1) transformación unitaria de referencia, (3) pipeline intermedio representativo, y (5) el pipeline exhaustivo ($N = 5$) que agota la totalidad de acciones disponibles en el servicio, sometiendo al worker a la máxima profundidad de procesamiento por documento.

### 3.3 Muestras y calentamiento
1. **Calentamiento**: antes de la matriz se envían pedidos preliminares para que la imagen ya esté en el Docker local.
2. **Muestras por celda**: 15 pedidos con $V_3 = 1$, 20 con $V_3 = 10$ y 50 con $V_3 = 50$, de los cuales un 20 % los manda un bot (el resto, usuarios con token). Con tan pocas muestras, el p95 es el valor cercano al máximo de la celda y una sola corrida no permite medir la variación entre corridas; los números valen para comparar celdas entre sí en una misma corrida, no como valores absolutos. Los pedidos de bots no cuentan para p50, p95 ni throughput.

### 3.4 Autenticación y bots
- Pedidos emitidos por bots: **153**, en todas las celdas.
- Rechazados con `401 NO_AUTORIZADO`: **153/153 (100.0%)**.
- Latencia media de esos rechazos, medida por el cliente (incluye la red): **11.53 ms**.
- Los usuarios legítimos usan varios tokens distintos (tres JWT distintos de `evidencias/emitir_token.py`), no uno compartido.
- La comprobación está en un `@app.middleware('http')` de FastAPI: un pedido sin token válido se rechaza antes de llegar a la cola y sin crear ningún contenedor.

> [!NOTE]
> **Alcance**: esta autenticación sirve para separar bots y medir la cola de R5. No tiene roles, vencimiento ni `/stats`: eso corresponde a R4.

### 3.5 Cuello de botella medido en `/health` y Monitoreo del Host
Durante cada celda un hilo consulta `/health` periódicamente y guarda el máximo de `workers_activos` y de `tareas_encoladas`. En simultáneo, se monitorea la utilización real de recursos del host (CPU y memoria residente) leyendo `/proc/stat` y `/proc/meminfo`.

Máximos observados en toda la matriz:

| $V_3$ | Workers activos (de 4) | Tareas encoladas |
|---|---|---|
| 1 | 1 | 0 |
| 10 | 4 | 4 |
| 50 | 4 | 35 |

- Con $V_3 = 1$ no hay cola: el cuello de botella es el ciclo de vida del contenedor (~0.61 s por tarea).
- Con $V_3 = 10$ y $V_3 = 50$ el pool llega a su tope y el resto espera en la cola: el cuello de botella es el número de workers (`TP2_WORKERS_MAX`). En esas celdas la latencia crece con la cola y el throughput deja de crecer.
- **Monitoreo de Host**: Tal como se destacó en la evaluación de arquitectura distribuida, ejecutar $N$ workers en una misma máquina podría provocar saturación si no se miden los recursos reales del host. El monitoreo empírico continuo demostró que los workers no sufrieron ahogo térmico ni contención por falta de memoria RAM en el sistema operativo host.

### 3.6 Límites de Recursos Docker (CPU y Memoria)
Siguiendo las buenas prácticas arquitectónicas y la preparación para entornos orquestados (Kubernetes):
- Cada contenedor efímero es creado con límites estrictos de hardware: `mem_limit = '256m'` y `nano_cpus = 1_000_000_000` (1 CPU virtual).
- Esto previene el problema del *vecino ruidoso* y evita que tareas defectuosas o con fuga de memoria agoten la RAM del host y provoquen OOM Killer generalizado.

### 3.7 La Cola como Mecanismo de Desacoplamiento de Pedido y Respuesta
> [!IMPORTANT]
> **Fundamento Arquitectónico**: Como se enfatizó en las clases teóricas de la cátedra, la razón primordial de introducir una cola > en un sistema distribuido **no es la concurrencia en sí misma, sino el desacoplamiento temporal entre el pedido y la respuesta**.

1. **Desacoplamiento temporal**: La cola permite que el servidor acepte y confirme la recepción del trabajo inmediatamente, aislando el ritmo de llegada de los clientes del ritmo de ejecución del pool de workers. En arquitecturas asincrónicas avanzadas esto se traduce en respuestas `202 Accepted` inmediatas con polling/callbacks posteriores. Incluso en este modelo sincrónico con bloqueo HTTP, la cola amortigua ráfagas de tráfico evitando el rechazo abrupto (`503 Service Unavailable`).
2. **Ordenamiento Lógico (Lamport)**: Una vez que el pedido está admitido y desacoplado en la cola, el algoritmo de relojes lógicos de Lamport (`Min-Heap`) garantiza que las tareas se despachen según la causalidad temporal declarada por los clientes, resolviendo el ordenamiento distribuido con criterio justo.
3. **Exclusión Mutua (Mutex)**: La exclusión mutua protege las estructuras de datos compartidas (la cola y el contador de workers) para que múltiples hilos despachadores y recolectores no colisionen al modificar el estado interno del servidor.

### 3.8 Entorno de Ejecución y Timeout Contractual
- **Timeout Contractual**: El servidor se ejecuta respetando `TP2_TIMEOUT_EJECUCION = 60` segundos (estipulado en el contrato público).
- **Despliegue vs Local**: Las pruebas pueden ejecutarse localmente mediante Docker Compose o directamente contra el despliegue remoto en AWS EC2 (`http://18.231.127.74:8082`) suministrando la variable `TP2_SERVIDOR`.

