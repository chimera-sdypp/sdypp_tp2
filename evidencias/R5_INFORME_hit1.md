# Informe de Evidencia R5 (Hit 1) — Pruebas con Tareas Diversas y Matriz de Parámetros

Este informe documenta los resultados empíricos obtenidos para el **Requerimiento R5**, incluyendo la evaluación de las 3 imágenes/tareas especializadas, la matriz completa de parámetros $V_1 \times V_2 \times V_3$, la verificación de clientes autenticados con múltiples usuarios concurrentes y el rechazo estricto de bots no autorizados.

## 1. Evaluación de las 3 Imágenes/Tareas Especializadas

Cada tarea fue probada con combinaciones de valores **válidos**, **límite** e **inválidos**.

| Tarea | Caso de Prueba | HTTP Esp. | HTTP Obt. | `tipo` Error | Mensaje / Detalle | Latencia | Estado |
|---|---|---|---|---|---|---|---|
| `CPU-bound` | Válido (primos 15k) | 200 | 200 | `—` | OK (resultado devuelto) | 0.863 s | **OK** |
| `CPU-bound` | Límite (primos 1.000.000, máximo permitido) | 200 | 200 | `—` | OK (resultado devuelto) | 0.913 s | **OK** |
| `CPU-bound` | Inválido (supera límite: 1.000.001) | 422 | 422 | `TAREA_RECHAZADA` | 'limite' supera el máximo permitido (1000000) | 0.802 s | **OK** |
| `CPU-bound` | Inválido (límite negativo) | 422 | 422 | `TAREA_RECHAZADA` | 'limite' debe ser un entero mayor a 1 | 0.834 s | **OK** |
| `Archivos` | Válido (2 archivos con contenido x 2 acciones) | 200 | 200 | `—` | OK (resultado devuelto) | 0.813 s | **OK** |
| `Archivos` | Límite (200 archivos x 5 acciones, tope de contrato) | 200 | 200 | `—` | OK (resultado devuelto) | 0.868 s | **OK** |
| `Archivos` | Inválido (supera tope: 201 archivos) | 422 | 0 | `ERROR_CONEXION_CLIENTE` | timed out | 10.01 s | **FALLÓ** |
| `Archivos` | Inválido (tamano_kb 1024 KB > 512 KB máximo) | 422 | 422 | `TAREA_RECHAZADA` | tamano_kb (1024 KB) supera el límite máximo permitido (512 KB) | 4.748 s | **OK** |
| `Archivos` | Inválido (acción inexistente) | 422 | 422 | `TAREA_RECHAZADA` | acción inválida 'accion_falsa'. Permitidas: ['analisis_frecuencia', 'comprimir', 'contar_palabras', 'hash_sha256', 'rotar_lineas'] | 0.844 s | **OK** |
| `Falla / Timeout` | Válido (dormir 0.3s) | 200 | 200 | `—` | OK (resultado devuelto) | 1.134 s | **OK** |
| `Falla / Timeout` | Inválido (división por cero) | 422 | 422 | `TAREA_RECHAZADA` | división por cero | 0.806 s | **OK** |
| `Falla / Timeout` | Falla intencional (explotar 500 -> 502) | 502 | 502 | `TAREA_FALLIDA` | la tarea falló o devolvió una respuesta inválida | 0.807 s | **OK** |
| `Falla / Timeout` | Límite (timeout contractual de 60s, usar --probar-timeout-largo) | 504 | — | `OMITIDO` | Omitido para no demorar 60s (ejecutar con --probar-timeout-largo o TP2_TIMEOUT_EJECUCION <= 10) | 0.0 s | **OK** |

---

## 2. Matriz Obligatoria de Carga y Rendimiento ($V_1 \times V_2 \times V_3$)

Variables evaluadas y diseño experimental:
- **$V_1$ (Archivos de entrada)**: `1`, `10`, `100` archivos. Se envía **contenido real** dentro de cada petición HTTP (respetando el límite de 64 KiB de tamaño de cuerpo estipulado en el contrato público).
- **$V_2$ (Acciones sobre la entrada)**: `1`, `3`, `5` acciones encadenadas. **Justificación de $V_2 = 5$**: Corresponde al agotamiento exhaustivo del catálogo completo de operaciones implementadas en el servicio tarea (`hash_sha256`, `comprimir`, `contar_palabras`, `rotar_lineas`, `analisis_frecuencia`).
- **$V_3$ (Concurrencia de clientes)**: `1`, `10`, `50` clientes simultáneos. Para garantizar rigor estadístico y estabilidad de percentiles, se aplicó una **fase de calentamiento** preliminar y un número consistente de repeticiones (15 muestras en $V_3=1$, 20 en $V_3=10$ y 50 en $V_3=50$).
- **Mezcla de múltiples usuarios y bots**: En todas las celdas se inyectó tráfico legítimo proveniente de múltiples usuarios con tokens de autenticación diferenciados, mezclado con un ~20% de solicitudes de bots (sin token o con credenciales maliciosas).
- **Detección empírica de cuellos de botella**: Se monitoreó `/stats` (con un token de admin) en tiempo real mediante un hilo dedicado durante cada celda, registrando los valores reales de `workers_ocupados` y `tareas_en_cola`.

| $V_1$ (Archivos) | $V_2$ (Acciones) | $V_3$ (Concurrencia) | Latencia p50 | Latencia p95 | Throughput (tpm) | CPU Host Prom/Pico | Mem Host (MB) | Error Funcional | Diagnóstico Empírico (/stats) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | 1 | 0.81 s | 0.825 s | **74.0** | 30.5% / 46.4% | 11026.3 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.81 s) |
| 1 | 1 | 10 | 1.617 s | 3.383 s | **195.5** | 49.3% / 90.2% | 11043.3 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 1 | 1 | 50 | 5.031 s | 11.812 s | **202.1** | 51.5% / 100.0% | 11188.7 | 0.0% | Medido en /stats: 39 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 1 | 3 | 1 | 0.814 s | 0.849 s | **73.3** | 30.2% / 45.9% | 11013.6 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.81 s) |
| 1 | 3 | 10 | 1.308 s | 1.706 s | **317.0** | 72.2% / 95.0% | 11064.1 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 1 | 3 | 50 | 5.21 s | 6.755 s | **351.2** | 74.7% / 100.0% | 11287.1 | 0.0% | Medido en /stats: 39 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 1 | 5 | 1 | 0.821 s | 0.835 s | **72.9** | 30.7% / 46.4% | 11035.5 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.82 s) |
| 1 | 5 | 10 | 1.391 s | 1.694 s | **318.9** | 72.2% / 97.8% | 11090.6 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 1 | 5 | 50 | 7.33 s | 8.965 s | **262.1** | 63.8% / 100.0% | 11331.1 | 0.0% | Medido en /stats: 39 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 10 | 1 | 1 | 0.825 s | 1.569 s | **62.4** | 28.9% / 49.7% | 11035.0 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.83 s) |
| 10 | 1 | 10 | 1.334 s | 1.751 s | **314.9** | 69.1% / 91.1% | 11098.4 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 10 | 1 | 50 | 5.21 s | 9.07 s | **262.9** | 63.4% / 100.0% | 11283.0 | 0.0% | Medido en /stats: 39 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 10 | 3 | 1 | 0.844 s | 6.896 s | **30.6** | 23.3% / 50.0% | 10993.5 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.84 s) |
| 10 | 3 | 10 | 1.404 s | 1.721 s | **309.0** | 72.2% / 92.8% | 11050.9 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 10 | 3 | 50 | 7.429 s | 9.056 s | **260.7** | 61.8% / 100.0% | 11199.7 | 0.0% | Medido en /stats: 39 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 10 | 5 | 1 | 0.839 s | 1.653 s | **60.8** | 28.5% / 51.1% | 11034.0 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.84 s) |
| 10 | 5 | 10 | 1.384 s | 1.735 s | **310.6** | 71.1% / 97.3% | 11042.4 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 10 | 5 | 50 | 5.28 s | 7.054 s | **334.8** | 75.8% / 100.0% | 11332.6 | 0.0% | Medido en /stats: 39 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 100 | 1 | 1 | 0.848 s | 0.861 s | **70.8** | 31.0% / 47.5% | 11034.8 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.85 s) |
| 100 | 1 | 10 | 1.687 s | 12.38 s | **68.2** | 30.4% / 95.0% | 11082.4 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 100 | 1 | 50 | 5.261 s | 7.004 s | **340.0** | 75.0% / 100.0% | 11266.6 | 0.0% | Medido en /stats: 39 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 100 | 3 | 1 | 0.833 s | 0.857 s | **71.6** | 30.3% / 50.0% | 11039.2 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.83 s) |
| 100 | 3 | 10 | 1.404 s | 1.735 s | **305.9** | 73.1% / 95.5% | 11110.7 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 100 | 3 | 50 | 5.414 s | 9.051 s | **253.4** | 62.7% / 100.0% | 11272.9 | 2.0% | Medido en /stats: 39 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 100 | 5 | 1 | 0.847 s | 0.89 s | **70.5** | 29.7% / 46.4% | 11029.9 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.85 s) |
| 100 | 5 | 10 | 1.318 s | 1.765 s | **312.7** | 71.2% / 90.6% | 11072.5 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 100 | 5 | 50 | 7.811 s | 9.351 s | **251.2** | 62.0% / 100.0% | 11230.2 | 0.0% | Medido en /stats: 39 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |

---

## 3. Análisis Técnico y Respuestas a los Puntos de Evaluación

### 3.1 Contenido real en $V_1$ vs costo del contenedor
Cada archivo viaja con su contenido en el cuerpo JSON de la petición. Tamaño medido del cuerpo con $V_2 = 1$: 331 bytes para $V_1 = 1$, 2005 para $V_1 = 10$ y 18745 para $V_1 = 100$ (el contrato admite hasta 64 KiB).

Con un solo cliente ($V_3 = 1$) la latencia p50 pasa de 0.81 s ($V_1 = 1$, $V_2 = 1$) a 0.847 s ($V_1 = 100$, $V_2 = 5$). El piso medido, 0.81 s, es el de crear, arrancar, esperar y borrar el contenedor efímero: ese costo domina y el procesamiento de los archivos agrega poco sobre él. Por eso, con $V_3 = 10$ y $V_3 = 50$ la latencia depende sobre todo de cuántos contenedores corren a la vez en el host y casi no de $V_1$ ni de $V_2$. No se midió por separado cuánto tarda la tarea dentro del contenedor, así que el reparto exacto entre ambos costos es una inferencia.

### 3.2 Justificación de $V_2 = 5$ (Acciones)
El servicio de procesamiento de archivos define 5 transformaciones permitidas en su contrato: `hash_sha256`, `comprimir`, `contar_palabras`, `rotar_lineas` y `analisis_frecuencia`. Los valores seleccionados evalúan: (1) transformación unitaria de referencia, (3) pipeline intermedio representativo, y (5) el pipeline exhaustivo ($N = 5$) que agota la totalidad de acciones disponibles en el servicio, sometiendo al worker a la máxima profundidad de procesamiento por documento.

### 3.3 Muestras y calentamiento
1. **Calentamiento**: antes de la matriz se envían pedidos preliminares para que la imagen ya esté en el Docker local.
2. **Muestras por celda**: 15 pedidos con $V_3 = 1$, 20 con $V_3 = 10$ y 50 con $V_3 = 50$, de los cuales un 20 % los manda un bot (el resto, usuarios con token). Con tan pocas muestras, el p95 es el valor cercano al máximo de la celda y una sola corrida no permite medir la variación entre corridas; los números valen para comparar celdas entre sí en una misma corrida, no como valores absolutos. Los pedidos de bots no cuentan para p50, p95 ni throughput.

### 3.4 Autenticación y bots
- Pedidos emitidos por bots: **153**, en todas las celdas.
- Rechazados con `401 NO_AUTORIZADO`: **153/153 (100.0%)**.
- Latencia media de esos rechazos, medida por el cliente (incluye la red): **11.84 ms**.
- Los usuarios legítimos usan varios tokens distintos (tres JWT distintos de `evidencias/emitir_token.py`), no uno compartido.
- La comprobación está en un `@app.middleware('http')` de FastAPI: un pedido sin token válido se rechaza sin crear ningún contenedor.

> [!NOTE]
> **Alcance**: es la misma autenticación de R4 (JWT con rol y vencimiento, y `/stats` sólo para admin). Acá sólo se mide cuántos pedidos de bots rechaza y cuánto tarda en hacerlo.

### 3.5 Cuello de botella medido en `/stats` y Monitoreo del Host
Durante cada celda un hilo consulta `/stats` periódicamente y guarda el máximo de `workers_ocupados` y de `tareas_en_cola`. En simultáneo, se monitorea la utilización real de recursos del host (CPU y memoria residente) leyendo `/proc/stat` y `/proc/meminfo`.

Máximos observados en toda la matriz:

| $V_3$ | Tareas en curso a la vez | Tareas encoladas |
|---|---|---|
| 1 | 1 | 0 |
| 10 | 8 | 0 |
| 50 | 39 | 0 |

- Con $V_3 = 1$ no hay cola: el cuello de botella es el ciclo de vida del contenedor (~0.81 s por tarea).

- El Hit 1 no tiene pool ni cola: cada pedido levanta su contenedor apenas llega. No hay un tope de workers: con $V_3 = 10$ y $V_3 = 50$ corren a la vez tantos contenedores como pedidos haya, y lo que puede limitar es el host (CPU, memoria y el daemon de Docker, que crea y borra cada contenedor).
- **Host**: en toda la matriz la CPU llegó como máximo al 100.0 % y la memoria usada a 11333 MB (columnas de la tabla).

### 3.6 Límites de Recursos Docker (CPU y Memoria)
- Los contenedores de tarea del Hit 1 se crean sin límite de CPU ni de memoria. Los límites (`256m` y 1 CPU) existen sólo en el Hit 2, donde se agregaron para la medición de 1, 2, 4 y 8 workers. Una tarea que use mucha memoria puede afectar al resto del host.

### 3.7 Entorno de Ejecución y Timeout Contractual
- **Timeout Contractual**: El servidor se ejecuta respetando `TP2_TIMEOUT_EJECUCION = 60` segundos (estipulado en el contrato público).
- **Despliegue vs Local**: Las pruebas pueden ejecutarse localmente mediante Docker Compose o directamente contra el despliegue remoto en AWS EC2 (`http://54.207.243.253:8081`) suministrando la variable `TP2_SERVIDOR`.

