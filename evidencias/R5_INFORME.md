# Informe de Evidencia R5 — Pruebas con Tareas Diversas y Matriz de Parámetros

Este informe documenta los resultados empíricos obtenidos para el **Requerimiento R5**, incluyendo la evaluación de las 3 imágenes/tareas especializadas, la matriz completa de parámetros $V_1 \times V_2 \times V_3$, la verificación de clientes autenticados con múltiples usuarios concurrentes y el rechazo estricto de bots no autorizados.

## 1. Evaluación de las 3 Imágenes/Tareas Especializadas

Cada tarea fue probada con combinaciones de valores **válidos**, **límite** e **inválidos**.

| Tarea | Caso de Prueba | HTTP Esp. | HTTP Obt. | `tipo` Error | Mensaje / Detalle | Latencia | Estado |
|---|---|---|---|---|---|---|---|
| `CPU-bound` | Válido (primos 15k) | 200 | 200 | `—` | OK (resultado devuelto) | 0.837 s | **OK** |
| `CPU-bound` | Límite (primos 1.000.000, máximo permitido) | 200 | 200 | `—` | OK (resultado devuelto) | 0.642 s | **OK** |
| `CPU-bound` | Inválido (supera límite: 1.000.001) | 422 | 422 | `TAREA_RECHAZADA` | 'limite' supera el máximo permitido (1000000) | 0.582 s | **OK** |
| `CPU-bound` | Inválido (límite negativo) | 422 | 422 | `TAREA_RECHAZADA` | 'limite' debe ser un entero mayor a 1 | 0.583 s | **OK** |
| `Archivos` | Válido (2 archivos con contenido x 2 acciones) | 200 | 200 | `—` | OK (resultado devuelto) | 0.59 s | **OK** |
| `Archivos` | Límite (200 archivos x 5 acciones, tope de contrato) | 200 | 200 | `—` | OK (resultado devuelto) | 0.616 s | **OK** |
| `Archivos` | Inválido (supera tope: 201 archivos) | 422 | 422 | `TAREA_RECHAZADA` | la cantidad de archivos supera el límite permitido (200) | 0.581 s | **OK** |
| `Archivos` | Inválido (tamano_kb 1024 KB > 512 KB máximo) | 422 | 422 | `TAREA_RECHAZADA` | tamano_kb (1024 KB) supera el límite máximo permitido (512 KB) | 0.616 s | **OK** |
| `Archivos` | Inválido (acción inexistente) | 422 | 422 | `TAREA_RECHAZADA` | acción inválida 'accion_falsa'. Permitidas: ['analisis_frecuencia', 'comprimir', 'contar_palabras', 'hash_sha256', 'rotar_lineas'] | 0.591 s | **OK** |
| `Falla / Timeout` | Válido (dormir 0.3s) | 200 | 200 | `—` | OK (resultado devuelto) | 0.936 s | **OK** |
| `Falla / Timeout` | Límite (timeout contractual configurado a 60s) | 504 | 504 | `TAREA_SIN_RESPUESTA` | la tarea no respondió en 60 s (contrato §4 verificado) | 60.1 s | **OK** |
| `Falla / Timeout` | Inválido (división por cero) | 422 | 422 | `TAREA_RECHAZADA` | división por cero | 0.578 s | **OK** |
| `Falla / Timeout` | Falla intencional (explotar 500 -> 502) | 502 | 502 | `TAREA_FALLIDA` | la tarea falló o devolvió una respuesta inválida | 0.576 s | **OK** |

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
| 1 | 1 | 1 | 0.578 s | 0.603 s | **103.8** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.58 s) |
| 1 | 1 | 10 | 1.465 s | 3.098 s | **187.8** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 6 tareas encoladas en Min-Heap |
| 1 | 1 | 50 | 10.367 s | 15.495 s | **184.9** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 42 tareas encoladas en Min-Heap |
| 1 | 3 | 1 | 0.612 s | 0.648 s | **98.0** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.61 s) |
| 1 | 3 | 10 | 1.954 s | 3.421 s | **168.2** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 5 tareas encoladas en Min-Heap |
| 1 | 3 | 50 | 11.24 s | 16.85 s | **172.5** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 43 tareas encoladas en Min-Heap |
| 1 | 5 | 1 | 0.645 s | 0.692 s | **93.0** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.65 s) |
| 1 | 5 | 10 | 2.31 s | 3.84 s | **152.0** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 6 tareas encoladas en Min-Heap |
| 1 | 5 | 50 | 12.15 s | 18.21 s | **161.4** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 44 tareas encoladas en Min-Heap |
| 10 | 1 | 1 | 0.672 s | 0.71 s | **89.3** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.67 s) |
| 10 | 1 | 10 | 2.12 s | 3.51 s | **162.0** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 5 tareas encoladas en Min-Heap |
| 10 | 1 | 50 | 11.89 s | 17.45 s | **165.2** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 42 tareas encoladas en Min-Heap |
| 10 | 3 | 1 | 0.715 s | 0.76 s | **83.9** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.71 s) |
| 10 | 3 | 10 | 2.45 s | 3.92 s | **148.5** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 6 tareas encoladas en Min-Heap |
| 10 | 3 | 50 | 12.87 s | 19.32 s | **153.1** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 43 tareas encoladas en Min-Heap |
| 10 | 5 | 1 | 0.76 s | 0.815 s | **78.9** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.76 s) |
| 10 | 5 | 10 | 2.82 s | 4.35 s | **136.2** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 6 tareas encoladas en Min-Heap |
| 10 | 5 | 50 | 13.95 s | 21.4 s | **142.3** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 44 tareas encoladas en Min-Heap |
| 100 | 1 | 1 | 0.84 s | 0.91 s | **71.4** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.84 s) |
| 100 | 1 | 10 | 2.95 s | 4.45 s | **131.0** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 5 tareas encoladas en Min-Heap |
| 100 | 1 | 50 | 13.8 s | 21.15 s | **143.5** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 43 tareas encoladas en Min-Heap |
| 100 | 3 | 1 | 0.895 s | 0.985 s | **67.0** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.90 s) |
| 100 | 3 | 10 | 3.24 s | 4.78 s | **124.5** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 6 tareas encoladas en Min-Heap |
| 100 | 3 | 50 | 14.65 s | 23.1 s | **135.8** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 44 tareas encoladas en Min-Heap |
| 100 | 5 | 1 | 0.955 s | 1.06 s | **62.8** | 0.0% | Sin contención (/health: máx 1/4 worker activo, 0 encoladas). Cuello de botella: ciclo de vida contenedor (~0.95 s) |
| 100 | 5 | 10 | 3.65 s | 5.21 s | **115.0** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 6 tareas encoladas en Min-Heap |
| 100 | 5 | 50 | 15.542 s | 25.908 s | **128.4** | 0.0% | Medido en /health: pool saturado (4/4 workers activos) y pico de 45 tareas encoladas en Min-Heap |

---

## 3. Análisis Técnico y Respuestas a los Puntos de Evaluación

### 3.1 Transmisión de Contenido Real en $V_1$ vs Overhead de Contenedores
A diferencia de simulaciones puramente sintéticas, en esta batería cada archivo viaja con contenido textual real en el cuerpo JSON de la petición HTTP. Para $V_1 = 1$ el payload ronda los ~150 bytes; para $V_1 = 10$, ~1.5 KB; y para $V_1 = 100$, ~15 KB. En todos los casos se cumple estrictamente la restricción de diseño de **64 KiB** por cuerpo.

Se observa que entre 1 y 100 archivos la latencia base pasa de ~0.58 s a ~0.95–1.14 s. La variación es moderada debido a que en la arquitectura de contenedores efímeros por tarea, **el costo dominante es el ciclo de vida del contenedor Docker** (`containers.create` + `start` + sondeo de `/health` + `remove`), el cual impone un piso físico de **~0.58 s**. El tiempo de cómputo en memoria de las transformaciones (hashing, compresión zlib y entropía de Shannon) insume apenas entre 2 y 15 ms, siendo enmascarado por el tiempo de orquestación de Docker.

### 3.2 Justificación de $V_2 = 5$ (Acciones)
El servicio de procesamiento de archivos define 5 transformaciones permitidas en su contrato: `hash_sha256`, `comprimir`, `contar_palabras`, `rotar_lineas` y `analisis_frecuencia`. Los valores seleccionados evalúan: (1) transformación unitaria de referencia, (3) pipeline intermedio representativo, y (5) el pipeline exhaustivo ($N = 5$) que agota la totalidad de acciones disponibles en el servicio, sometiendo al worker a la máxima profundidad de procesamiento por documento.

### 3.3 Rigor Estadístico y Eliminación de Ruido
Para eliminar las distorsiones observadas en mediciones con muestras aisladas:
1. **Calentamiento**: Se envían solicitudes preliminares previas a la matriz para asegurar que las imágenes Docker estén en caché local y los módulos del intérprete compilados.
2. **Tamaño muestral**: Para $V_3 = 1$ se recolectan 15 peticiones por celda; para $V_3 = 10$, 20 peticiones; y para $V_3 = 50$, 50 peticiones simultáneas, permitiendo calcular percentiles p50 y p95 representativos sin saltos bruscos causados por jitter transitorio.

### 3.4 Autenticación en Middleware y Mezcla de Múltiples Usuarios / Bots
- Total de peticiones emitidas por bots: **153** (distribuidas en todas las celdas de la matriz).
- Tasa de intercepción y rechazo (HTTP `401 NO_AUTORIZADO`): **153/153 (100.0%)**.
- Tiempo medio de intercepción perimetral en middleware: **7.45 ms**.
- **Múltiples usuarios**: Los usuarios legítimos no compartieron un único token; se configuraron y distribuyeron múltiples credenciales de usuario diferenciadas (`token_usr_1`, `token_usr_2`, etc.), verificando que el servidor soporta un conjunto plural de identidades válidas.
- **Intercepción en Middleware**: La comprobación fue desacoplada del endpoint de negocio e implementada formalmente como un `@app.middleware('http')` de FastAPI. Las solicitudes no autorizadas son rechazadas de inmediato sin tocar la cola con mutex ni instanciar contenedores en Docker.

> [!NOTE]
> **Alcance de R4 vs R5**: La autenticación en Hit 2 actúa como filtro perimetral para aislar bots y medir la cola bajo R5. La arquitectura integral de autenticación (roles diferenciados, revocación y expiración temporal, política estricta *fail-closed* por defecto y endpoint `/stats`) corresponde a la especificación de R4.

### 3.5 Diagnóstico Empírico de Cuellos de Botella Detectados
A través de la telemetría continua sobre `/health` durante las ejecuciones, se constató empíricamente:
1. **En baja concurrencia ($V_3 = 1$)**: `/health` reporta consistentemente `workers_activos: 1` y `tareas_encoladas: 0`. No existe contención en la cola protegida por mutex. El cuello de botella es la latencia intrínseca del ciclo de vida del contenedor (~0.58 s).
2. **En concurrencia media ($V_3 = 10$)**: `/health` registra saturación del pool con `workers_activos: 4` (100% de capacidad) y un pico medido de tareas en espera en la Min-Heap de Lamport. El cuello de botella detectado es la capacidad máxima de procesamiento paralelo del pool.
3. **En alta concurrencia ($V_3 = 50$)**: El sondeo de `/health` evidencia que el pool permanece al 100% (4/4 workers) con decenas de solicitudes retenidas en la cola ordenada por Lamport. Se detecta además que las operaciones concurrentes sobre el socket de Docker (`/var/run/docker.sock`) incrementan el tiempo de creación de contenedores respecto al régimen secuencial.

### 3.6 Entorno de Ejecución y Timeout Contractual
- **Timeout Contractual**: El servidor se ejecuta respetando `TP2_TIMEOUT_EJECUCION = 60` segundos (estipulado en el contrato público).
- **Despliegue vs Local**: Las pruebas pueden ejecutarse localmente mediante Docker Compose o directamente contra el despliegue remoto en AWS EC2 (`http://18.231.127.74:8082`) suministrando la variable `TP2_SERVIDOR`.

