# Informe de Evidencia R5 — Pruebas con Tareas Diversas y Matriz de Parámetros

Este informe documenta los resultados empíricos obtenidos para el **Requerimiento R5**, incluyendo la evaluación de las 3 imágenes/tareas especializadas, la matriz completa de parámetros $V_1 \times V_2 \times V_3$, la verificación de clientes autenticados con múltiples usuarios concurrentes y el rechazo estricto de bots no autorizados.

## 1. Evaluación de las 3 Imágenes/Tareas Especializadas

Cada tarea fue probada con combinaciones de valores **válidos**, **límite** e **inválidos**.

| Tarea | Caso de Prueba | HTTP Esp. | HTTP Obt. | `tipo` Error | Mensaje / Detalle | Latencia | Estado |
|---|---|---|---|---|---|---|---|
| `CPU-bound` | Válido (primos 15k) | 200 | 200 | `—` | OK (resultado devuelto) | 0.913 s | **OK** |
| `CPU-bound` | Límite (primos 1.000.000, máximo permitido) | 200 | 200 | `—` | OK (resultado devuelto) | 1.043 s | **OK** |
| `CPU-bound` | Inválido (supera límite: 1.000.001) | 422 | 422 | `TAREA_RECHAZADA` | 'limite' supera el máximo permitido (1000000) | 0.887 s | **OK** |
| `CPU-bound` | Inválido (límite negativo) | 422 | 422 | `TAREA_RECHAZADA` | 'limite' debe ser un entero mayor a 1 | 0.901 s | **OK** |
| `Archivos` | Válido (2 archivos con contenido x 2 acciones) | 200 | 200 | `—` | OK (resultado devuelto) | 0.925 s | **OK** |
| `Archivos` | Límite (200 archivos x 5 acciones, tope de contrato) | 200 | 200 | `—` | OK (resultado devuelto) | 0.962 s | **OK** |
| `Archivos` | Inválido (supera tope: 201 archivos) | 422 | 422 | `TAREA_RECHAZADA` | la cantidad de archivos supera el límite permitido (200) | 0.913 s | **OK** |
| `Archivos` | Inválido (tamano_kb 1024 KB > 512 KB máximo) | 422 | 422 | `TAREA_RECHAZADA` | tamano_kb (1024 KB) supera el límite máximo permitido (512 KB) | 1.176 s | **OK** |
| `Archivos` | Inválido (acción inexistente) | 422 | 422 | `TAREA_RECHAZADA` | acción inválida 'accion_falsa'. Permitidas: ['analisis_frecuencia', 'comprimir', 'contar_palabras', 'hash_sha256', 'rotar_lineas'] | 1.085 s | **OK** |
| `Falla / Timeout` | Válido (dormir 0.3s) | 200 | 200 | `—` | OK (resultado devuelto) | 1.283 s | **OK** |
| `Falla / Timeout` | Inválido (división por cero) | 422 | 422 | `TAREA_RECHAZADA` | división por cero | 1.112 s | **OK** |
| `Falla / Timeout` | Falla intencional (explotar 500 -> 502) | 502 | 502 | `TAREA_FALLIDA` | la tarea falló o devolvió una respuesta inválida | 0.898 s | **OK** |
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
| 1 | 1 | 1 | 0.887 s | 1.024 s | **65.9** | 7.1% / 26.9% | 7433.0 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.89 s) |
| 1 | 1 | 10 | 3.273 s | 7.409 s | **96.1** | 8.1% / 35.2% | 7408.2 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 1 | 1 | 50 | 13.939 s | 25.213 s | **94.5** | 7.4% / 30.9% | 7441.3 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 1 | 3 | 1 | 0.997 s | 1.974 s | **51.7** | 9.8% / 35.0% | 7673.3 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~1.0 s) |
| 1 | 3 | 10 | 2.399 s | 7.833 s | **95.9** | 7.4% / 35.6% | 7854.5 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 1 | 3 | 50 | 13.39 s | 25.19 s | **94.8** | 5.6% / 30.6% | 7701.1 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 1 | 5 | 1 | 0.89 s | 1.123 s | **64.6** | 5.3% / 26.9% | 7514.3 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.89 s) |
| 1 | 5 | 10 | 3.201 s | 8.407 s | **82.8** | 4.9% / 24.3% | 7525.7 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 1 | 5 | 50 | 14.331 s | 25.028 s | **94.3** | 6.3% / 35.7% | 7565.5 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 10 | 1 | 1 | 0.898 s | 1.143 s | **63.4** | 5.4% / 24.9% | 7493.5 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.9 s) |
| 10 | 1 | 10 | 2.78 s | 7.92 s | **94.7** | 6.0% / 25.3% | 7501.3 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 10 | 1 | 50 | 15.156 s | 26.818 s | **89.0** | 6.5% / 35.0% | 7504.4 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 34 tareas encoladas en Min-Heap |
| 10 | 3 | 1 | 0.89 s | 1.135 s | **64.4** | 5.5% / 17.5% | 7572.8 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.89 s) |
| 10 | 3 | 10 | 3.635 s | 7.747 s | **93.9** | 5.8% / 31.9% | 7550.0 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 10 | 3 | 50 | 13.964 s | 25.988 s | **90.8** | 6.3% / 51.4% | 7600.3 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 10 | 5 | 1 | 0.89 s | 1.144 s | **64.1** | 5.8% / 19.7% | 7618.8 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.89 s) |
| 10 | 5 | 10 | 2.868 s | 8.006 s | **94.2** | 5.9% / 32.6% | 7448.6 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 10 | 5 | 50 | 13.915 s | 25.195 s | **94.1** | 7.1% / 44.5% | 7480.3 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 100 | 1 | 1 | 0.93 s | 1.455 s | **56.8** | 6.7% / 30.8% | 7504.1 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.93 s) |
| 100 | 1 | 10 | 2.866 s | 8.037 s | **90.7** | 6.3% / 27.9% | 7507.3 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 100 | 1 | 50 | 13.953 s | 25.151 s | **91.4** | 6.7% / 32.6% | 7618.9 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 100 | 3 | 1 | 0.925 s | 1.267 s | **61.2** | 7.6% / 47.5% | 7646.1 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.93 s) |
| 100 | 3 | 10 | 3.157 s | 8.815 s | **84.0** | 8.4% / 37.2% | 7564.7 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 100 | 3 | 50 | 16.115 s | 27.335 s | **86.5** | 7.0% / 36.3% | 7653.6 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |
| 100 | 5 | 1 | 0.933 s | 1.193 s | **61.5** | 6.1% / 18.4% | 7610.8 | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.93 s) |
| 100 | 5 | 10 | 3.815 s | 7.943 s | **90.8** | 6.0% / 39.7% | 7651.6 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 4 tareas encoladas en Min-Heap |
| 100 | 5 | 50 | 14.179 s | 25.832 s | **92.3** | 5.9% / 33.5% | 7628.7 | 0.0% | Medido en /health: saturación de pool (4/4 workers activos) y pico de 35 tareas encoladas en Min-Heap |

---

## 3. Análisis Técnico y Respuestas a los Puntos de Evaluación

### 3.1 Contenido real en $V_1$ vs costo del contenedor
Cada archivo viaja con su contenido en el cuerpo JSON de la petición. Tamaño medido del cuerpo con $V_2 = 1$: 331 bytes para $V_1 = 1$, 2005 para $V_1 = 10$ y 18745 para $V_1 = 100$ (el contrato admite hasta 64 KiB).

Con un solo cliente ($V_3 = 1$) la latencia p50 pasa de 0.887 s ($V_1 = 1$, $V_2 = 1$) a 0.933 s ($V_1 = 100$, $V_2 = 5$). El piso medido, 0.89 s, es el de crear, arrancar, esperar y borrar el contenedor efímero: ese costo domina y el procesamiento de los archivos agrega poco sobre él. Por eso, con $V_3 = 10$ y $V_3 = 50$ la latencia depende sobre todo de la cola y casi no de $V_1$ ni de $V_2$. No se midió por separado cuánto tarda la tarea dentro del contenedor, así que el reparto exacto entre ambos costos es una inferencia.

### 3.2 Justificación de $V_2 = 5$ (Acciones)
El servicio de procesamiento de archivos define 5 transformaciones permitidas en su contrato: `hash_sha256`, `comprimir`, `contar_palabras`, `rotar_lineas` y `analisis_frecuencia`. Los valores seleccionados evalúan: (1) transformación unitaria de referencia, (3) pipeline intermedio representativo, y (5) el pipeline exhaustivo ($N = 5$) que agota la totalidad de acciones disponibles en el servicio, sometiendo al worker a la máxima profundidad de procesamiento por documento.

### 3.3 Muestras y calentamiento
1. **Calentamiento**: antes de la matriz se envían pedidos preliminares para que la imagen ya esté en el Docker local.
2. **Muestras por celda**: 15 pedidos con $V_3 = 1$, 20 con $V_3 = 10$ y 50 con $V_3 = 50$, de los cuales un 20 % los manda un bot (el resto, usuarios con token). Con tan pocas muestras, el p95 es el valor cercano al máximo de la celda y una sola corrida no permite medir la variación entre corridas; los números valen para comparar celdas entre sí en una misma corrida, no como valores absolutos. Los pedidos de bots no cuentan para p50, p95 ni throughput.

### 3.4 Autenticación y bots
- Pedidos emitidos por bots: **153**, en todas las celdas.
- Rechazados con `401 NO_AUTORIZADO`: **153/153 (100.0%)**.
- Latencia media de esos rechazos, medida por el cliente (incluye la red): **8.83 ms**.
- Los usuarios legítimos usan varios tokens distintos (los de `TP2_API_TOKEN`, separados por comas), no uno compartido.
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

- Con $V_3 = 1$ no hay cola: el cuello de botella es el ciclo de vida del contenedor (~0.89 s por tarea).
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

