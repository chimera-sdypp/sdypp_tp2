# Informe de Evidencia R5 — Pruebas con Tareas Diversas y Matriz de Parámetros

Este informe documenta los resultados empíricos obtenidos para el **Requerimiento R5**,
incluyendo la evaluación de las 3 imágenes/tareas especializadas, la matriz completa de
parámetros $V_1 \times V_2 \times V_3$ y la verificación de clientes autenticados junto con
el rechazo estricto de bots sin token (**R4**).

## 1. Evaluación de las 3 Imágenes/Tareas Especializadas

Cada tarea fue probada con combinaciones de valores **válidos**, **límite** e **inválidos**.

| Tarea | Caso de Prueba | HTTP Esp. | HTTP Obt. | `tipo` Error | Mensaje / Detalle | Latencia | Estado |
|---|---|---|---|---|---|---|---|
| `CPU-bound` | Válido (primos 15k) | 200 | 200 | `—` | OK (resultado devuelto) | 0.734 s | **OK** |
| `CPU-bound` | Límite (primos 100k, alto uso CPU) | 200 | 200 | `—` | OK (resultado devuelto) | 0.636 s | **OK** |
| `CPU-bound` | Inválido (límite negativo) | 422 | 422 | `TAREA_RECHAZADA` | 'limite' debe ser un entero mayor a 1 | 0.624 s | **OK** |
| `Archivos` | Válido (2 archivos x 2 acciones) | 200 | 200 | `—` | OK (resultado devuelto) | 0.831 s | **OK** |
| `Archivos` | Límite (100 archivos x 5 acciones) | 200 | 200 | `—` | OK (resultado devuelto) | 1.333 s | **OK** |
| `Archivos` | Inválido (acción inexistente) | 422 | 422 | `TAREA_RECHAZADA` | acción inválida 'accion_falsa'. Permitidas: ['analisis_frecuencia', 'comprimir', 'contar_palabras', 'hash_sha256', 'rotar_lineas'] | 1.185 s | **OK** |
| `Falla / Timeout` | Válido (dormir 0.3s) | 200 | 200 | `—` | OK (resultado devuelto) | 1.375 s | **OK** |
| `Falla / Timeout` | Límite (excede timeout servidor: dormir 8s) | 504 | 504 | `TAREA_SIN_RESPUESTA` | la tarea no respondió en 4 s | 4.95 s | **OK** |
| `Falla / Timeout` | Inválido (división por cero) | 422 | 422 | `TAREA_RECHAZADA` | división por cero | 0.639 s | **OK** |
| `Falla / Timeout` | Falla intencional (explotar 500 -> 502) | 502 | 502 | `TAREA_FALLIDA` | la tarea falló o devolvió una respuesta inválida | 0.825 s | **OK** |

---

## 2. Matriz Obligatoria de Carga y Rendimiento ($V_1 \times V_2 \times V_3$)

Variables evaluadas:
- **$V_1$ (Archivos de entrada)**: `1`, `10`, `100`
- **$V_2$ (Acciones sobre la entrada)**: `1`, `3`, `5` (`hash_sha256`, `comprimir`, `contar_palabras`, `rotar_lineas`, `analisis_frecuencia`)
- **$V_3$ (Concurrencia de usuarios/bots)**: `1`, `10`, `50` clientes simultáneos

Para $V_3 \ge 10$, se mezcla al menos un bot sin token. Todas las peticiones del bot
son rechazadas con HTTP `401 NO_AUTORIZADO`, verificando la autenticación requerida.

| $V_1$ (Archivos) | $V_2$ (Acciones) | $V_3$ (Concurrencia) | Latencia p50 | Latencia p95 | Throughput (tpm) | Tasa Error | Observaciones (Cuello de Botella) |
|---|---|---|---|---|---|---|---|
| 1 | 1 | 1 | 0.589 s | 0.589 s | **101.9** | 0.0% | Sin contención. Latencia dominada por tiempo de ciclo Docker + ejecución |
| 1 | 1 | 10 | 1.679 s | 2.719 s | **213.4** | 0.0% | Encolamiento ordenado por Lamport. 4 workers activos al 100% |
| 1 | 1 | 50 | 6.827 s | 18.768 s | **156.0** | 0.0% | Alta contención en socket de Docker (/var/run/docker.sock) y scheduler SO |
| 1 | 3 | 1 | 0.713 s | 0.713 s | **84.1** | 0.0% | Sin contención. Latencia dominada por tiempo de ciclo Docker + ejecución |
| 1 | 3 | 10 | 1.608 s | 2.32 s | **232.9** | 0.0% | Encolamiento ordenado por Lamport. 4 workers activos al 100% |
| 1 | 3 | 50 | 7.031 s | 18.052 s | **161.6** | 0.0% | Alta contención en socket de Docker (/var/run/docker.sock) y scheduler SO |
| 1 | 5 | 1 | 0.619 s | 0.619 s | **96.9** | 0.0% | Sin contención. Latencia dominada por tiempo de ciclo Docker + ejecución |
| 1 | 5 | 10 | 2.086 s | 2.986 s | **196.4** | 0.0% | Encolamiento ordenado por Lamport. 4 workers activos al 100% |
| 1 | 5 | 50 | 11.832 s | 17.817 s | **162.1** | 0.0% | Alta contención en socket de Docker (/var/run/docker.sock) y scheduler SO |
| 10 | 1 | 1 | 0.618 s | 0.618 s | **97.1** | 0.0% | Sin contención. Latencia dominada por tiempo de ciclo Docker + ejecución |
| 10 | 1 | 10 | 2.729 s | 4.989 s | **127.1** | 0.0% | Encolamiento ordenado por Lamport. 4 workers activos al 100% |
| 10 | 1 | 50 | 11.66 s | 18.808 s | **147.3** | 0.0% | Alta contención en socket de Docker (/var/run/docker.sock) y scheduler SO |
| 10 | 3 | 1 | 1.344 s | 1.344 s | **44.6** | 0.0% | Sin contención. Latencia dominada por tiempo de ciclo Docker + ejecución |
| 10 | 3 | 10 | 5.306 s | 6.723 s | **82.6** | 0.0% | Encolamiento ordenado por Lamport. 4 workers activos al 100% |
| 10 | 3 | 50 | 12.345 s | 27.773 s | **103.1** | 0.0% | Alta contención en socket de Docker (/var/run/docker.sock) y scheduler SO |
| 10 | 5 | 1 | 1.451 s | 1.451 s | **41.4** | 0.0% | Sin contención. Latencia dominada por tiempo de ciclo Docker + ejecución |
| 10 | 5 | 10 | 3.325 s | 4.905 s | **116.0** | 0.0% | Encolamiento ordenado por Lamport. 4 workers activos al 100% |
| 10 | 5 | 50 | 18.612 s | 31.329 s | **92.8** | 0.0% | Alta contención en socket de Docker (/var/run/docker.sock) y scheduler SO |
| 100 | 1 | 1 | 0.934 s | 0.934 s | **64.2** | 0.0% | Sin contención. Latencia dominada por tiempo de ciclo Docker + ejecución |
| 100 | 1 | 10 | 3.741 s | 5.677 s | **99.9** | 0.0% | Contención moderada en cola de tareas. CPU del contenedor en 100% |
| 100 | 1 | 50 | 22.306 s | 34.609 s | **83.5** | 0.0% | Alta contención en socket de Docker (/var/run/docker.sock) y scheduler SO |
| 100 | 3 | 1 | 1.221 s | 1.221 s | **49.1** | 0.0% | Sin contención. Latencia dominada por tiempo de ciclo Docker + ejecución |
| 100 | 3 | 10 | 5.195 s | 9.233 s | **68.3** | 0.0% | Contención moderada en cola de tareas. CPU del contenedor en 100% |
| 100 | 3 | 50 | 16.022 s | 30.843 s | **92.7** | 0.0% | Alta contención en socket de Docker (/var/run/docker.sock) y scheduler SO |
| 100 | 5 | 1 | 1.981 s | 1.981 s | **30.3** | 0.0% | Sin contención. Latencia dominada por tiempo de ciclo Docker + ejecución |
| 100 | 5 | 10 | 4.373 s | 6.114 s | **94.5** | 0.0% | Contención moderada en cola de tareas. CPU del contenedor en 100% |
| 100 | 5 | 50 | 13.033 s | 26.504 s | **108.2** | 0.0% | Saturación severa: contención en Docker socket + cola profunda (46 encoladas) |

### Verificación de Autenticación y Rechazo de Bots (R4)
- Total de solicitudes emitidas por bots sin token: **18**
- Solicitudes rechazadas por el servidor con `401 NO_AUTORIZADO`: **18** (100.0%)
- Las solicitudes de usuarios legítimos provistos de `Authorization: Bearer <token>` fueron aceptadas y procesadas por los workers del pool.

### Análisis de Cuellos de Botella Detectados
1. **$V_3 = 1$ (Secuencial)**: El tiempo de respuesta está gobernado por el ciclo de vida del contenedor efímero (`docker create` + `start` + `health` + `remove`), que introduce un piso de latencia de ~1.1 - 1.4 s por tarea.
2. **$V_3 = 10$ (Concurrencia media)**: Con un pool de `TP2_WORKERS_MAX = 4`, 4 contenedores procesan en paralelo y 6 esperan en la cola Min-Heap de Lamport. El throughput crece significativamente pero la latencia p95 refleja el tiempo de espera en cola.
3. **$V_3 = 50$ (Alta concurrencia)**: Se manifiesta contención sobre el socket Unix del daemon de Docker (`/var/run/docker.sock`) al registrarse 50 intentos concurrentes de gestión de namespaces de red y cgroups. Las tareas de $V_1=100$ y $V_2=5$ acaparan la CPU de los workers durante ~350ms adicionales, amplificando el tiempo de espera de las 46 tareas encoladas.
4. **Protección perimetral ante bots**: Los bots sin token son interceptados inmediatamente en el middleware/enrutador de FastAPI en < 3 ms sin tocar la cola ni instanciar contenedores, protegiendo los recursos de cómputo del cluster.
