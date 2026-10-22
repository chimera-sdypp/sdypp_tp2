# Informe de Evidencia R5 (Hit 3) — Pruebas con Tareas Diversas y Matriz de Parámetros

Este informe documenta los resultados empíricos obtenidos para el **Requerimiento R5**, incluyendo la evaluación de las 3 imágenes/tareas especializadas, la matriz completa de parámetros $V_1 \times V_2 \times V_3$, la verificación de clientes autenticados con múltiples usuarios concurrentes y el rechazo estricto de bots no autorizados.

## 1. Evaluación de las 3 Imágenes/Tareas Especializadas

Cada tarea fue probada con combinaciones de valores **válidos**, **límite** e **inválidos**.

| Tarea | Caso de Prueba | HTTP Esp. | HTTP Obt. | `tipo` Error | Mensaje / Detalle | Latencia | Estado |
|---|---|---|---|---|---|---|---|
| `CPU-bound` | Válido (primos 15k) | 200 | 200 | `—` | OK (resultado devuelto) | 0.936 s | **OK** |
| `CPU-bound` | Límite (primos 1.000.000, máximo permitido) | 200 | 200 | `—` | OK (resultado devuelto) | 0.907 s | **OK** |
| `CPU-bound` | Inválido (supera límite: 1.000.001) | 422 | 422 | `TAREA_RECHAZADA` | 'limite' supera el máximo permitido (1000000) | 0.823 s | **OK** |
| `CPU-bound` | Inválido (límite negativo) | 422 | 422 | `TAREA_RECHAZADA` | 'limite' debe ser un entero mayor a 1 | 0.809 s | **OK** |
| `Archivos` | Válido (2 archivos con contenido x 2 acciones) | 200 | 200 | `—` | OK (resultado devuelto) | 0.815 s | **OK** |
| `Archivos` | Límite (200 archivos x 5 acciones, tope de contrato) | 200 | 200 | `—` | OK (resultado devuelto) | 0.863 s | **OK** |
| `Archivos` | Inválido (supera tope: 201 archivos) | 422 | 422 | `TAREA_RECHAZADA` | la cantidad de archivos supera el límite permitido (200) | 0.832 s | **OK** |
| `Archivos` | Inválido (tamano_kb 1024 KB > 512 KB máximo) | 422 | 422 | `TAREA_RECHAZADA` | tamano_kb (1024 KB) supera el límite máximo permitido (512 KB) | 0.843 s | **OK** |
| `Archivos` | Inválido (acción inexistente) | 422 | 422 | `TAREA_RECHAZADA` | acción inválida 'accion_falsa'. Permitidas: ['analisis_frecuencia', 'comprimir', 'contar_palabras', 'hash_sha256', 'rotar_lineas'] | 0.832 s | **OK** |
| `Falla / Timeout` | Válido (dormir 0.3s) | 200 | 200 | `—` | OK (resultado devuelto) | 1.111 s | **OK** |
| `Falla / Timeout` | Inválido (división por cero) | 422 | 422 | `TAREA_RECHAZADA` | división por cero | 0.813 s | **OK** |
| `Falla / Timeout` | Falla intencional (explotar 500 -> 502) | 502 | 502 | `TAREA_FALLIDA` | la tarea falló o devolvió una respuesta inválida | 0.863 s | **OK** |
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
| 1 | 1 | 1 | 0.836 s | 0.856 s | **71.7** | 31.0% / 45.4% | 10982.8 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.84 s) |
| 1 | 1 | 10 | 1.514 s | 1.677 s | **293.2** | 76.6% / 100.0% | 11051.8 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 1 | 1 | 50 | 5.201 s | 8.723 s | **268.2** | 65.3% / 100.0% | 11201.0 | 0.0% | Medido en /stats: 40 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 1 | 3 | 1 | 0.846 s | 0.86 s | **71.0** | 31.2% / 47.5% | 10985.0 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.85 s) |
| 1 | 3 | 10 | 1.357 s | 1.711 s | **313.6** | 71.9% / 91.7% | 11054.6 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 1 | 3 | 50 | 9.611 s | 10.956 s | **216.8** | 58.7% / 100.0% | 11239.5 | 0.0% | Medido en /stats: 40 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 1 | 5 | 1 | 0.881 s | 10.331 s | **23.4** | 23.8% / 63.0% | 11008.6 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.88 s) |
| 1 | 5 | 10 | 1.382 s | 1.822 s | **303.0** | 70.0% / 91.2% | 11076.2 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 1 | 5 | 50 | 6.484 s | 7.699 s | **309.3** | 78.9% / 100.0% | 11216.1 | 0.0% | Medido en /stats: 40 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 10 | 1 | 1 | 0.899 s | 4.145 s | **40.1** | 26.4% / 47.0% | 11000.3 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.9 s) |
| 10 | 1 | 10 | 1.489 s | 1.971 s | **289.0** | 72.6% / 94.5% | 11054.3 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 10 | 1 | 50 | 6.954 s | 8.145 s | **290.2** | 75.8% / 98.9% | 11249.6 | 0.0% | Medido en /stats: 40 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 10 | 3 | 1 | 0.892 s | 1.954 s | **55.2** | 28.6% / 60.7% | 10995.1 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.89 s) |
| 10 | 3 | 10 | 2.217 s | 3.556 s | **197.6** | 58.1% / 94.5% | 11038.0 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 10 | 3 | 50 | 9.457 s | 10.607 s | **224.0** | 63.1% / 100.0% | 11135.5 | 0.0% | Medido en /stats: 40 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 10 | 5 | 1 | 0.909 s | 1.673 s | **57.3** | 28.8% / 48.9% | 10998.0 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.91 s) |
| 10 | 5 | 10 | 1.518 s | 1.942 s | **281.2** | 74.8% / 93.4% | 11060.5 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 10 | 5 | 50 | 21.695 s | 22.843 s | **104.6** | 39.6% / 100.0% | 11058.5 | 0.0% | Medido en /stats: 40 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 100 | 1 | 1 | 0.907 s | 1.517 s | **58.8** | 30.4% / 58.8% | 10971.3 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.91 s) |
| 100 | 1 | 10 | 1.493 s | 2.002 s | **279.9** | 74.9% / 92.3% | 10999.7 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 100 | 1 | 50 | 6.724 s | 7.97 s | **296.9** | 75.9% / 100.0% | 11185.7 | 0.0% | Medido en /stats: 40 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 100 | 3 | 1 | 0.914 s | 0.943 s | **65.5** | 31.4% / 89.9% | 11007.2 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.91 s) |
| 100 | 3 | 10 | 1.498 s | 1.943 s | **285.4** | 75.5% / 94.5% | 11095.7 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 100 | 3 | 50 | 7.341 s | 8.485 s | **278.9** | 73.0% / 98.4% | 11404.1 | 0.0% | Medido en /stats: 40 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 100 | 5 | 1 | 0.9 s | 0.919 s | **66.4** | 31.5% / 48.4% | 11244.3 | 0.0% | Sin contención (/stats: máx 1 tareas en curso, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.9 s) |
| 100 | 5 | 10 | 1.531 s | 1.938 s | **278.9** | 74.1% / 93.9% | 11257.6 | 0.0% | Medido en /stats: 8 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |
| 100 | 5 | 50 | 6.891 s | 10.236 s | **231.2** | 64.7% / 100.0% | 11412.4 | 0.0% | Medido en /stats: 40 tareas en curso a la vez (0 encoladas). Sin cola: un contenedor por pedido |

---

## 3. Análisis Técnico y Respuestas a los Puntos de Evaluación

### 3.1 Contenido real en $V_1$ vs costo del contenedor
Cada archivo viaja con su contenido en el cuerpo JSON de la petición. Tamaño medido del cuerpo con $V_2 = 1$: 331 bytes para $V_1 = 1$, 2005 para $V_1 = 10$ y 18745 para $V_1 = 100$ (el contrato admite hasta 64 KiB).

Con un solo cliente ($V_3 = 1$) la latencia p50 pasa de 0.836 s ($V_1 = 1$, $V_2 = 1$) a 0.9 s ($V_1 = 100$, $V_2 = 5$). El piso medido, 0.84 s, es el de crear, arrancar, esperar y borrar el contenedor efímero: ese costo domina y el procesamiento de los archivos agrega poco sobre él. Por eso, con $V_3 = 10$ y $V_3 = 50$ la latencia depende sobre todo de cuántos contenedores corren a la vez en el host y casi no de $V_1$ ni de $V_2$. No se midió por separado cuánto tarda la tarea dentro del contenedor, así que el reparto exacto entre ambos costos es una inferencia.

### 3.2 Justificación de $V_2 = 5$ (Acciones)
El servicio de procesamiento de archivos define 5 transformaciones permitidas en su contrato: `hash_sha256`, `comprimir`, `contar_palabras`, `rotar_lineas` y `analisis_frecuencia`. Los valores seleccionados evalúan: (1) transformación unitaria de referencia, (3) pipeline intermedio representativo, y (5) el pipeline exhaustivo ($N = 5$) que agota la totalidad de acciones disponibles en el servicio, sometiendo al worker a la máxima profundidad de procesamiento por documento.

### 3.3 Muestras y calentamiento
1. **Calentamiento**: antes de la matriz se envían pedidos preliminares para que la imagen ya esté en el Docker local.
2. **Muestras por celda**: 15 pedidos con $V_3 = 1$, 20 con $V_3 = 10$ y 50 con $V_3 = 50$, de los cuales un 20 % los manda un bot (el resto, usuarios con token). Con tan pocas muestras, el p95 es el valor cercano al máximo de la celda y una sola corrida no permite medir la variación entre corridas; los números valen para comparar celdas entre sí en una misma corrida, no como valores absolutos. Los pedidos de bots no cuentan para p50, p95 ni throughput.

### 3.4 Autenticación y bots
- Pedidos emitidos por bots: **153**, en todas las celdas.
- Rechazados con `401 NO_AUTORIZADO`: **153/153 (100.0%)**.
- Latencia media de esos rechazos, medida por el cliente (incluye la red): **9.72 ms**.
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
| 50 | 40 | 0 |

- Con $V_3 = 1$ no hay cola: el cuello de botella es el ciclo de vida del contenedor (~0.84 s por tarea).

- El Hit 3 no tiene cola: el coordinador asigna cada tarea al nodo con menos carga y el nodo la corre enseguida. `/stats` suma las tareas en curso de los 3 nodos. Los tres comparten el host y el daemon de Docker, así que no hay un tope de workers: con $V_3 = 10$ y $V_3 = 50$ corren a la vez tantas tareas como pedidos haya, y lo que puede limitar es el host (CPU, memoria y el daemon que crea y borra cada contenedor), más el paso por nginx y el coordinador.
- **Host**: en toda la matriz la CPU llegó como máximo al 100.0 % y la memoria usada a 11412 MB (columnas de la tabla).

### 3.6 Límites de Recursos Docker (CPU y Memoria)
- Los contenedores de tarea del Hit 3 se crean sin límite de CPU ni de memoria. Los límites (`256m` y 1 CPU) existen sólo en el Hit 2, donde se agregaron para la medición de 1, 2, 4 y 8 workers. Una tarea que use mucha memoria puede afectar al resto del host.

### 3.7 Entorno de Ejecución y Timeout Contractual
- **Timeout Contractual**: El servidor se ejecuta respetando `TP2_TIMEOUT_EJECUCION = 60` segundos (estipulado en el contrato público).
- **Despliegue vs Local**: Las pruebas pueden ejecutarse localmente mediante Docker Compose o directamente contra el despliegue remoto en AWS EC2 (`http://54.207.243.253:8083`) suministrando la variable `TP2_SERVIDOR`.

