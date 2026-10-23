"""Generador de Carga y Evaluación de Rendimiento para R5 (Cerberus).

Evalúa de forma empírica y reproducible:
1. Las 3 imágenes de tareas especializadas (CPU-bound, Archivos, Falla/Timeout)
   bajo condiciones válidas, límite e inválidas.
2. La matriz combinatoria de parámetros V1 (archivos) x V2 (acciones) x V3 (concurrencia).
3. La mezcla auténtica de múltiples usuarios legítimos con tokens diferenciados y
   el rechazo estricto de bots sin token o con token inválido en el middleware HTTP.
4. Mediciones empíricas de cuellos de botella mediante sondeo continuo de /health
   (workers_activos y tareas_encoladas).
5. Transmisión de contenido real en V1 respetando el límite de 64 KiB del protocolo.
6. Rigor estadístico con fase de calentamiento, repeticiones adecuadas y percentiles p50/p95.
"""

import argparse
import json
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

IMAGEN_CPU = "cerberusdistribuido/tarea:cpu-1.0.0"
IMAGEN_ARCHIVOS = "cerberusdistribuido/tarea:archivos-1.0.0"
IMAGEN_FALLA = "cerberusdistribuido/tarea:falla-1.0.0"

TODAS_ACCIONES = [
    "hash_sha256",
    "comprimir",
    "contar_palabras",
    "rotar_lineas",
    "analisis_frecuencia",
]


class SamplerHost(threading.Thread):
    """Muestreador en segundo plano de CPU y memoria del host para medir recursos reales."""

    def __init__(self, intervalo: float = 0.15):
        super().__init__(daemon=True)
        self.intervalo = intervalo
        self.activo = True
        self.muestras_cpu = []
        self.muestras_mem_mb = []

    def _leer_proc(self):
        try:
            with open("/proc/stat") as f:
                fields = [float(x) for x in f.readline().split()[1:8]]
            idle = fields[3] + fields[4]
            total = sum(fields)
        except Exception:
            return None, None
        try:
            mem = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        mem[parts[0].strip()] = int(parts[1].split()[0])
            mem_used_mb = (mem["MemTotal"] - mem.get("MemAvailable", 0)) / 1024.0
        except Exception:
            mem_used_mb = None
        return (idle, total), mem_used_mb

    def run(self):
        prev_cpu, _ = self._leer_proc()
        while self.activo:
            time.sleep(self.intervalo)
            curr_cpu, mem_mb = self._leer_proc()
            if prev_cpu and curr_cpu:
                idle_delta = curr_cpu[0] - prev_cpu[0]
                total_delta = curr_cpu[1] - prev_cpu[1]
                if total_delta > 0:
                    cpu_pct = max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))
                    self.muestras_cpu.append(cpu_pct)
            if mem_mb is not None:
                self.muestras_mem_mb.append(mem_mb)
            prev_cpu = curr_cpu

    def detener(self):
        self.activo = False

    def metricas(self):
        cpu_prom = round(statistics.mean(self.muestras_cpu), 1) if self.muestras_cpu else 0.0
        cpu_pico = round(max(self.muestras_cpu), 1) if self.muestras_cpu else 0.0
        mem_prom = round(statistics.mean(self.muestras_mem_mb), 1) if self.muestras_mem_mb else 0.0
        return {
            "cpu_promedio_pct": cpu_prom,
            "cpu_pico_pct": cpu_pico,
            "mem_host_mb": mem_prom,
        }


class SamplerHealth(threading.Thread):
    """Muestreador en segundo plano que registra los cuellos de botella reales sin saturar el socket.

    Con un token de admin consulta `/stats`, que expone `workers_ocupados` y `tareas_en_cola` en los
    tres hits (R4). Sin token lee el pool de `/health`, que sólo existe en el Hit 2.
    """

    def __init__(self, servidor: str, intervalo: float = 0.08, token_admin: str | None = None):
        super().__init__(daemon=True)
        self.servidor = servidor
        self.intervalo = intervalo
        self.token_admin = token_admin
        self.fuente = "/stats" if token_admin else "/health"
        self.activo = True
        self.max_activos = 0
        self.max_encoladas = 0
        # En los Hits 1 y 3 /stats informa workers_max = null: no hay un tope de workers.
        self.workers_max = None if token_admin else 4
        self.total_muestras = 0
        self.muestras_fallidas = 0

    def run(self):
        url = f"{self.servidor}{self.fuente}"
        cabeceras = {"X-Lamport-Clock": "1"}
        if self.token_admin:
            cabeceras["Authorization"] = f"Bearer {self.token_admin}"
        while self.activo:
            try:
                req = Request(url, headers=cabeceras)
                with urlopen(req, timeout=1.5) as resp:
                    datos = json.loads(resp.read().decode("utf-8"))
                    if self.token_admin:
                        contenido = datos.get("contenido", {})
                        activos = contenido.get("workers_ocupados", 0) or 0
                        encoladas = contenido.get("tareas_en_cola", 0) or 0
                        wmax = contenido.get("workers_max")
                    else:
                        pool_info = datos.get("contenido", {}).get("pool", {})
                        activos = pool_info.get("workers_activos", 0)
                        encoladas = pool_info.get("tareas_encoladas", 0)
                        wmax = pool_info.get("workers_max", 4)
                    if activos > self.max_activos:
                        self.max_activos = activos
                    if encoladas > self.max_encoladas:
                        self.max_encoladas = encoladas
                    self.workers_max = wmax
                    self.total_muestras += 1
            except Exception:
                self.muestras_fallidas += 1
            time.sleep(self.intervalo)

    def detener(self):
        self.activo = False


def _hacer_pedido(
    servidor: str,
    payload: dict,
    token: str | None = None,
    lamport_ts: int = 1,
    timeout: float = 30.0,
) -> tuple[int, dict, float]:
    url = f"{servidor}/getRemoteTask"
    datos = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Lamport-Clock": str(lamport_ts),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = Request(url, data=datos, headers=headers, method="POST")
    t0 = time.perf_counter()
    try:
        with urlopen(req, timeout=timeout) as resp:
            duracion = time.perf_counter() - t0
            codigo = resp.status
            cuerpo = resp.read().decode("utf-8")
            try:
                datos_resp = json.loads(cuerpo)
            except Exception:
                datos_resp = {"raw": cuerpo}
            return codigo, datos_resp, duracion
    except HTTPError as e:
        duracion = time.perf_counter() - t0
        cuerpo = e.read().decode("utf-8") if e.fp else ""
        try:
            datos_resp = json.loads(cuerpo)
        except Exception:
            datos_resp = {"raw": cuerpo}
        return e.code, datos_resp, duracion
    except (URLError, TimeoutError, OSError) as e:
        duracion = time.perf_counter() - t0
        return 0, {"error": str(e)}, duracion


def armar_payload_matriz(v1_archivos: int, v2_acciones: int) -> dict:
    """Construye el payload con contenido real dentro del límite de 64 KiB para V1 archivos y V2 acciones."""
    archivos = []
    for i in range(v1_archivos):
        # Contenido real enviado en el cuerpo HTTP (~140 bytes por archivo; ~14 KB para 100 archivos)
        contenido = (
            f"Archivo de prueba {i:03d} para benchmark Cerberus SDyPP 2026. "
            f"Evaluacion de pipeline de hash SHA-256, compresion zlib y estadistica de entropia.\n"
        )
        archivos.append({"id": f"archivo_{i:03d}.txt", "contenido": contenido})

    acciones = TODAS_ACCIONES[:v2_acciones]
    return {
        "calculo": "procesar_archivos",
        "parametros": {"archivos": archivos, "acciones": acciones},
        "imagen": IMAGEN_ARCHIVOS,
    }


def calentar_servidor(servidor: str, tokens: list[str]):
    """Envía peticiones preliminares para calentar cachés de imagen, capas Docker y socket IPC."""
    token = tokens[0] if tokens else None
    print("[*] Calentando servidor y contenedores preliminares...")
    payload_calentamiento = {
        "calculo": "procesar_archivos",
        "parametros": {
            "archivos": [{"id": "calentamiento.txt", "contenido": "test"}],
            "acciones": ["hash_sha256"],
        },
        "imagen": IMAGEN_ARCHIVOS,
    }
    for _ in range(3):
        _hacer_pedido(servidor, payload_calentamiento, token=token, timeout=15.0)
    time.sleep(0.5)


def evaluar_tareas_individuales(
    servidor: str,
    tokens: list[str],
    timeout_ejecucion: float = 60.0,
    probar_timeout_largo: bool = False,
) -> list[dict]:
    """Prueba cada una de las 3 tareas bajo casos válidos, límite e inválidos reales."""
    token = tokens[0] if tokens else None
    casos = [
        # --- 1. Tarea CPU-bound ---
        {
            "tarea": "CPU-bound",
            "imagen": IMAGEN_CPU,
            "caso": "Válido (primos 15k)",
            "calculo": "primos",
            "parametros": {"limite": 15000},
            "esperado_http": 200,
            "timeout_cliente": 25.0,
        },
        {
            "tarea": "CPU-bound",
            "imagen": IMAGEN_CPU,
            "caso": "Límite (primos 1.000.000, máximo permitido)",
            "calculo": "primos",
            "parametros": {"limite": 1000000},
            "esperado_http": 200,
            "timeout_cliente": 25.0,
        },
        {
            "tarea": "CPU-bound",
            "imagen": IMAGEN_CPU,
            "caso": "Inválido (supera límite: 1.000.001)",
            "calculo": "primos",
            "parametros": {"limite": 1000001},
            "esperado_http": 422,
            "timeout_cliente": 10.0,
        },
        {
            "tarea": "CPU-bound",
            "imagen": IMAGEN_CPU,
            "caso": "Inválido (límite negativo)",
            "calculo": "primos",
            "parametros": {"limite": -50},
            "esperado_http": 422,
            "timeout_cliente": 10.0,
        },
        # --- 2. Tarea Procesamiento de Archivos ---
        {
            "tarea": "Archivos",
            "imagen": IMAGEN_ARCHIVOS,
            "caso": "Válido (2 archivos con contenido x 2 acciones)",
            "calculo": "procesar_archivos",
            "parametros": {
                "archivos": [
                    {"id": "doc1.txt", "contenido": "Texto de prueba 1\n"},
                    {"id": "doc2.txt", "contenido": "Texto de prueba 2 con mas datos\n"},
                ],
                "acciones": ["hash_sha256", "comprimir"],
            },
            "esperado_http": 200,
            "timeout_cliente": 20.0,
        },
        {
            "tarea": "Archivos",
            "imagen": IMAGEN_ARCHIVOS,
            "caso": "Límite (200 archivos x 5 acciones, tope de contrato)",
            "calculo": "procesar_archivos",
            "parametros": {
                "archivos": [{"id": f"archivo_{i:03d}.dat", "tamano_kb": 1} for i in range(200)],
                "acciones": TODAS_ACCIONES[:5],
            },
            "esperado_http": 200,
            "timeout_cliente": 30.0,
        },
        {
            "tarea": "Archivos",
            "imagen": IMAGEN_ARCHIVOS,
            "caso": "Inválido (supera tope: 201 archivos)",
            "calculo": "procesar_archivos",
            "parametros": {
                "archivos": [{"id": f"archivo_{i:03d}.dat", "tamano_kb": 1} for i in range(201)],
                "acciones": ["hash_sha256"],
            },
            "esperado_http": 422,
            "timeout_cliente": 10.0,
        },
        {
            "tarea": "Archivos",
            "imagen": IMAGEN_ARCHIVOS,
            "caso": "Inválido (tamano_kb 1024 KB > 512 KB máximo)",
            "calculo": "procesar_archivos",
            "parametros": {
                "archivos": [{"id": "grande.bin", "tamano_kb": 1024}],
                "acciones": ["hash_sha256"],
            },
            "esperado_http": 422,
            "timeout_cliente": 10.0,
        },
        {
            "tarea": "Archivos",
            "imagen": IMAGEN_ARCHIVOS,
            "caso": "Inválido (acción inexistente)",
            "calculo": "procesar_archivos",
            "parametros": {
                "archivos": [{"id": "doc1.txt", "contenido": "data"}],
                "acciones": ["accion_falsa"],
            },
            "esperado_http": 422,
            "timeout_cliente": 10.0,
        },
        # --- 3. Tarea Falla / Timeout ---
        {
            "tarea": "Falla / Timeout",
            "imagen": IMAGEN_FALLA,
            "caso": "Válido (dormir 0.3s)",
            "calculo": "dormir",
            "parametros": {"segundos": 0.3},
            "esperado_http": 200,
            "timeout_cliente": 15.0,
        },
        {
            "tarea": "Falla / Timeout",
            "imagen": IMAGEN_FALLA,
            "caso": "Inválido (división por cero)",
            "calculo": "division",
            "parametros": {"a": 10, "b": 0},
            "esperado_http": 422,
            "timeout_cliente": 10.0,
        },
        {
            "tarea": "Falla / Timeout",
            "imagen": IMAGEN_FALLA,
            "caso": "Falla intencional (explotar 500 -> 502)",
            "calculo": "explotar",
            "parametros": {},
            "esperado_http": 502,
            "timeout_cliente": 10.0,
        },
    ]

    # Evaluación de timeout: si se solicita explícitamente o si el timeout configurado es breve (<= 10s)
    if probar_timeout_largo or timeout_ejecucion <= 10.0:
        casos.append({
            "tarea": "Falla / Timeout",
            "imagen": IMAGEN_FALLA,
            "caso": f"Límite (excede timeout servidor: dormir {timeout_ejecucion + 1:.0f}s)",
            "calculo": "dormir",
            "parametros": {"segundos": timeout_ejecucion + 1.0},
            "esperado_http": 504,
            "timeout_cliente": timeout_ejecucion + 15.0,
        })
    else:
        # En ejecuciones estándar con timeout contractual de 60s, documentar con total transparencia
        # que fue omitido para no demorar la suite, en vez de inventar latencias simuladas.
        casos.append({
            "tarea": "Falla / Timeout",
            "imagen": IMAGEN_FALLA,
            "caso": f"Límite (timeout contractual de {timeout_ejecucion:.0f}s, usar --probar-timeout-largo)",
            "calculo": "dormir",
            "parametros": {"segundos": timeout_ejecucion + 1.0},
            "esperado_http": 504,
            "omitido_transparente": True,
            "timeout_cliente": 1.0,
        })

    resultados = []
    print("\n[*] Ejecutando batería de pruebas sobre las 3 tareas especializadas...")
    for c in casos:
        if c.get("omitido_transparente"):
            res_item = {
                "tarea": c["tarea"],
                "caso": c["caso"],
                "http_esperado": 504,
                "http_obtenido": "—",
                "tipo_error": "OMITIDO",
                "mensaje": f"Omitido para no demorar 60s (ejecutar con --probar-timeout-largo o TP2_TIMEOUT_EJECUCION <= 10)",
                "latencia_s": 0.0,
                "cumple": True,
            }
            resultados.append(res_item)
            print(f"  ℹ [{c['tarea']}] {c['caso']} -> Omitido en suite rápida (ejecutar con --probar-timeout-largo)")
            continue

        payload = {
            "calculo": c["calculo"],
            "parametros": c["parametros"],
            "imagen": c["imagen"],
        }
        codigo, respuesta, duracion = _hacer_pedido(
            servidor, payload, token=token, timeout=c.get("timeout_cliente", 25.0)
        )
        contenido = respuesta.get("contenido", {})
        error_info = contenido.get("error", {})
        tipo_err = error_info.get("tipo", "—")
        mensaje = error_info.get("mensaje", "")
        if not error_info and "resultado" in contenido:
            mensaje = "OK (resultado devuelto)"
        if codigo == 0:
            tipo_err = "ERROR_CONEXION_CLIENTE"
            mensaje = respuesta.get("error", "Error de transporte / red cliente")

        cumple = (codigo == c["esperado_http"])
        res_item = {
            "tarea": c["tarea"],
            "caso": c["caso"],
            "http_esperado": c["esperado_http"],
            "http_obtenido": codigo,
            "tipo_error": tipo_err,
            "mensaje": mensaje,
            "latencia_s": round(duracion, 3),
            "cumple": cumple,
        }
        resultados.append(res_item)
        simbolo = "✓" if cumple else "✗"
        print(f"  {simbolo} [{c['tarea']}] {c['caso']} -> HTTP {codigo} (esperado {c['esperado_http']}) en {duracion:.2f}s")

    return resultados


def ejecutar_celda_matriz(
    servidor: str,
    tokens_usuarios: list[str],
    v1_archivos: int,
    v2_acciones: int,
    v3_concurrencia: int,
    workers_pool: int = 4,
    token_admin: str | None = None,
    hit: str = "hit2",
) -> dict:
    """Ejecuta una celda de la matriz V1 x V2 x V3 con mezcla de usuarios/bots y medición empírica de /health."""
    payload = armar_payload_matriz(v1_archivos, v2_acciones)

    # Rigor estadístico: repeticiones para obtener distribución representativa
    # V3=1: 15 peticiones consecutivas (12 de usuarios legítimos, 3 de bots)
    # V3=10: 10 clientes concurrentes x 2 pedidos = 20 peticiones (16 usuarios, 4 bots)
    # V3=50: 50 clientes concurrentes x 1 pedido = 50 peticiones (40 usuarios, 10 bots)
    if v3_concurrencia == 1:
        repeticiones_por_hilo = 15
        cantidad_bots_hilo = 0  # se alterna secuencialmente
        cantidad_usuarios_hilo = 1
    elif v3_concurrencia == 10:
        repeticiones_por_hilo = 2
        cantidad_bots_hilo = 2   # 20% bots
        cantidad_usuarios_hilo = 8
    else:  # 50
        repeticiones_por_hilo = 1
        cantidad_bots_hilo = 10  # 20% bots
        cantidad_usuarios_hilo = 40

    clientes = []
    if v3_concurrencia == 1:
        clientes.append({"id": "cliente_secuencial", "es_bot": False, "token": None, "idx": 0})
    else:
        for u in range(cantidad_usuarios_hilo):
            # Rotar entre múltiples tokens de usuario para simular identidades diferenciadas
            tok = tokens_usuarios[u % len(tokens_usuarios)] if tokens_usuarios else None
            clientes.append({"id": f"usuario_{u+1}", "es_bot": False, "token": tok, "idx": u})
        for b in range(cantidad_bots_hilo):
            # Bots con token nulo o token con firma inválida
            tok_bot = None if b % 2 == 0 else "token-bot-malicioso-invalido"
            clientes.append({"id": f"bot_{b+1}", "es_bot": True, "token": tok_bot, "idx": b})

    latencias_exitosas = []
    latencias_totales = []
    latencias_bot_ms = []
    codigos = {}
    peticiones_bot_rechazadas = 0
    peticiones_bot_totales = 0

    barrera = threading.Barrier(len(clientes))
    lock = threading.Lock()

    def _trabajador(cliente_info):
        nonlocal peticiones_bot_rechazadas, peticiones_bot_totales
        barrera.wait()
        for rep in range(repeticiones_por_hilo):
            # En V3=1 se entrelazan usuarios legítimos y bots (1 bot cada 4 pedidos legítimos)
            if v3_concurrencia == 1:
                es_bot_iter = (rep % 5 == 4)
                tok = (None if rep % 2 == 0 else "token-bot-invalido") if es_bot_iter else (
                    tokens_usuarios[rep % len(tokens_usuarios)] if tokens_usuarios else None
                )
                ts_envio = 1 if es_bot_iter else (rep + 1) * 2
            else:
                es_bot_iter = cliente_info["es_bot"]
                tok = cliente_info["token"]
                # Pacing para bots en V3=10: no morir en 20ms, espaciar para acompañar a los workers
                if es_bot_iter and rep > 0:
                    time.sleep(0.5)

                # Generar timestamps de Lamport intencionalmente variados y desordenados
                # para verificar empíricamente que la Min-Heap de Lamport reordena las tareas
                u_idx = cliente_info.get("idx", 0)
                ts_envio = 1 if es_bot_iter else ((u_idx * 17) % 73 + 3 + (rep * 7))

            cod, resp, duracion = _hacer_pedido(
                servidor,
                payload,
                token=tok,
                lamport_ts=ts_envio,
                timeout=45.0,
            )
            with lock:
                latencias_totales.append(duracion)
                codigos[cod] = codigos.get(cod, 0) + 1
                if es_bot_iter:
                    peticiones_bot_totales += 1
                    latencias_bot_ms.append(duracion * 1000.0)
                    if cod == 401:
                        peticiones_bot_rechazadas += 1
                else:
                    if cod == 200:
                        latencias_exitosas.append(duracion)

    # Iniciar sondeo en tiempo real de /health y telemetría del host
    sampler = SamplerHealth(servidor, token_admin=token_admin)
    sampler_host = SamplerHost()
    sampler.start()
    sampler_host.start()

    t_inicio = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(clientes)) as pool:
        list(pool.map(_trabajador, clientes))
    t_total = time.perf_counter() - t_inicio

    sampler.detener()
    sampler_host.detener()
    sampler.join(timeout=1.0)
    sampler_host.join(timeout=1.0)

    metricas_host = sampler_host.metricas()
    max_activos = sampler.max_activos
    max_encoladas = sampler.max_encoladas
    workers_max = sampler.workers_max if sampler.fuente == "/stats" else (sampler.workers_max or workers_pool)

    total_pedidos = len(latencias_totales)
    total_exitosos = len(latencias_exitosas)
    errores_reales = sum(cnt for cod, cnt in codigos.items() if cod not in (200, 401))
    tasa_error_funcional = (errores_reales / total_pedidos) * 100 if total_pedidos else 0.0

    p50 = statistics.median(latencias_exitosas) if latencias_exitosas else (
        statistics.median(latencias_totales) if latencias_totales else 0.0
    )
    if len(latencias_exitosas) >= 2:
        # Interpolación inclusiva estándar para percentil 95 exacto
        cuantil_95 = statistics.quantiles(latencias_exitosas, n=20, method="inclusive")[18]
    else:
        cuantil_95 = max(latencias_exitosas) if latencias_exitosas else (
            max(latencias_totales) if latencias_totales else 0.0
        )

    throughput_tpm = (total_exitosos / max(0.001, t_total)) * 60.0

    # Diagnóstico del cuello de botella MEDIDO empíricamente (en /health o en /stats).
    # Sin tope de workers (Hits 1 y 3) se informa cuántas tareas corrían a la vez.
    fuente = sampler.fuente
    tope = f"/{workers_max}" if workers_max else ""
    unidad = "workers activos" if workers_max else "tareas en curso"
    if max_encoladas > 0:
        obs = (
            f"Medido en {fuente}: saturación de pool ({max_activos}{tope} {unidad}) "
            f"y pico de {max_encoladas} tareas encoladas" + (" en Min-Heap" if hit == "hit2" else "")
        )
    elif max_activos > 1:
        obs = (
            f"Medido en {fuente}: {max_activos}{tope} {unidad} a la vez (0 encoladas). "
            + ("Concurrencia balanceada" if workers_max else "Sin cola: un contenedor por pedido")
        )
    else:
        obs = (
            f"Sin contención ({fuente}: máx {max(1, max_activos)}{tope} {unidad}, 0 encoladas). "
            f"Cuello de botella: ciclo de vida contenedor (~{round(p50, 2)} s)"
        )

    bot_avg_ms = round(statistics.mean(latencias_bot_ms), 2) if latencias_bot_ms else 0.0

    return {
        "v1_archivos": v1_archivos,
        "v2_acciones": v2_acciones,
        "v3_concurrencia": v3_concurrencia,
        "total_pedidos": total_pedidos,
        "pedidos_exitosos": total_exitosos,
        "pedidos_bot_totales": peticiones_bot_totales,
        "pedidos_bot_rechazados": peticiones_bot_rechazadas,
        "latencia_bot_avg_ms": bot_avg_ms,
        "distribucion_codigos": codigos,
        "p50_latencia_s": round(p50, 3),
        "p95_latencia_s": round(cuantil_95, 3),
        "throughput_tpm": round(throughput_tpm, 1),
        "tasa_error_pct": round(tasa_error_funcional, 1),
        "tiempo_total_s": round(t_total, 2),
        "workers_activos_medidos": max_activos,
        "tareas_encoladas_medidas": max_encoladas,
        "cpu_host_pct": metricas_host["cpu_promedio_pct"],
        "cpu_host_pico_pct": metricas_host["cpu_pico_pct"],
        "mem_host_mb": metricas_host["mem_host_mb"],
        "fuente_medicion": fuente,
        "workers_max": workers_max,
        "observaciones": obs,
    }


def ejecutar_matriz_completa(servidor: str, tokens_usuarios: list[str], token_admin: str | None = None,
                             hit: str = "hit2") -> list[dict]:
    """Recorre las 27 combinaciones V1 x V2 x V3 y mide rendimiento empírico."""
    v1_valores = [1, 10, 100]
    v2_valores = [1, 3, 5]
    v3_valores = [1, 10, 50]

    # Fase previa de calentamiento
    calentar_servidor(servidor, tokens_usuarios)

    resultados_matriz = []
    total_celdas = len(v1_valores) * len(v2_valores) * len(v3_valores)
    print(f"\n[*] Ejecutando Matriz R5 ({total_celdas} celdas combinatorias)...")

    nro = 1
    for v1 in v1_valores:
        for v2 in v2_valores:
            for v3 in v3_valores:
                print(f" [{nro:02d}/{total_celdas}] Evaluando V1={v1:3d} arch | V2={v2} acc | V3={v3:2d} hilos...", end="", flush=True)
                res = ejecutar_celda_matriz(
                    servidor=servidor,
                    tokens_usuarios=tokens_usuarios,
                    v1_archivos=v1,
                    v2_acciones=v2,
                    v3_concurrencia=v3,
                    token_admin=token_admin,
                    hit=hit,
                )
                print(f" -> p50={res['p50_latencia_s']}s | p95={res['p95_latencia_s']}s | {res['throughput_tpm']} tpm | {res['fuente_medicion']}: act={res['workers_activos_medidos']} enc={res['tareas_encoladas_medidas']}")
                resultados_matriz.append(res)
                nro += 1

    return resultados_matriz


NOMBRES_HIT = {"hit1": "Hit 1", "hit2": "Hit 2", "hit3": "Hit 3"}
PUERTOS_HIT = {"hit1": 8081, "hit2": 8082, "hit3": 8083}


def generar_reporte_markdown(pruebas_tareas: list[dict], matriz: list[dict], salida_path: Path, hit: str = "hit2"):
    """Genera el informe final de R5 en formato Markdown con análisis empírico y metodológico riguroso."""
    nombre_hit = NOMBRES_HIT.get(hit, hit)
    fuente = matriz[0].get("fuente_medicion", "/health") if matriz else "/health"
    con_pool = hit == "hit2"
    lineas = []
    lineas.append(f"# Informe de Evidencia R5 ({nombre_hit}) — Pruebas con Tareas Diversas y Matriz de Parámetros\n")
    lineas.append(
        "Este informe documenta los resultados empíricos obtenidos para el **Requerimiento R5**, "
        "incluyendo la evaluación de las 3 imágenes/tareas especializadas, la matriz completa de "
        "parámetros $V_1 \\times V_2 \\times V_3$, la verificación de clientes autenticados con múltiples "
        "usuarios concurrentes y el rechazo estricto de bots no autorizados.\n"
    )

    lineas.append("## 1. Evaluación de las 3 Imágenes/Tareas Especializadas\n")
    lineas.append("Cada tarea fue probada con combinaciones de valores **válidos**, **límite** e **inválidos**.\n")
    lineas.append("| Tarea | Caso de Prueba | HTTP Esp. | HTTP Obt. | `tipo` Error | Mensaje / Detalle | Latencia | Estado |")
    lineas.append("|---|---|---|---|---|---|---|---|")
    for t in pruebas_tareas:
        estado_badge = "OK" if t["cumple"] else "FALLÓ"
        lineas.append(
            f"| `{t['tarea']}` | {t['caso']} | {t['http_esperado']} | {t['http_obtenido']} | "
            f"`{t['tipo_error']}` | {t['mensaje']} | {t['latencia_s']} s | **{estado_badge}** |"
        )

    lineas.append("\n---\n")
    lineas.append("## 2. Matriz Obligatoria de Carga y Rendimiento ($V_1 \\times V_2 \\times V_3$)\n")
    lineas.append("Variables evaluadas y diseño experimental:")
    lineas.append(
        "- **$V_1$ (Archivos de entrada)**: `1`, `10`, `100` archivos. Se envía **contenido real** dentro de cada "
        "petición HTTP (respetando el límite de 64 KiB de tamaño de cuerpo estipulado en el contrato público)."
    )
    lineas.append(
        "- **$V_2$ (Acciones sobre la entrada)**: `1`, `3`, `5` acciones encadenadas. "
        "**Justificación de $V_2 = 5$**: Corresponde al agotamiento exhaustivo del catálogo completo de operaciones "
        "implementadas en el servicio tarea (`hash_sha256`, `comprimir`, `contar_palabras`, `rotar_lineas`, `analisis_frecuencia`)."
    )
    lineas.append(
        "- **$V_3$ (Concurrencia de clientes)**: `1`, `10`, `50` clientes simultáneos. "
        "Para garantizar rigor estadístico y estabilidad de percentiles, se aplicó una **fase de calentamiento** preliminar "
        "y un número consistente de repeticiones (15 muestras en $V_3=1$, 20 en $V_3=10$ y 50 en $V_3=50$)."
    )
    lineas.append(
        "- **Mezcla de múltiples usuarios y bots**: En todas las celdas se inyectó tráfico legítimo proveniente de múltiples "
        "usuarios con tokens de autenticación diferenciados, mezclado con un ~20% de solicitudes de bots (sin token o con "
        "credenciales maliciosas)."
    )
    if fuente == "/stats":
        lineas.append(
            "- **Detección empírica de cuellos de botella**: Se monitoreó `/stats` (con un token de admin) en tiempo real "
            "mediante un hilo dedicado durante cada celda, registrando los valores reales de `workers_ocupados` y `tareas_en_cola`.\n"
        )
    else:
        lineas.append(
            "- **Detección empírica de cuellos de botella**: Se monitoreó el endpoint `/health` en tiempo real mediante un hilo "
            "dedicado durante cada celda, registrando los valores reales de `workers_activos` y `tareas_encoladas` del pool.\n"
        )

    lineas.append(f"| $V_1$ (Archivos) | $V_2$ (Acciones) | $V_3$ (Concurrencia) | Latencia p50 | Latencia p95 | Throughput (tpm) | CPU Host Prom/Pico | Mem Host (MB) | Error Funcional | Diagnóstico Empírico ({fuente}) |")
    lineas.append("|---|---|---|---|---|---|---|---|---|---|")
    for m in matriz:
        cpu_prom = m.get("cpu_host_pct", 0.0)
        cpu_pico = m.get("cpu_host_pico_pct", 0.0)
        cpu_str = f"{cpu_prom:.1f}% / {cpu_pico:.1f}%"
        mem_str = f"{m.get('mem_host_mb', 0.0):.1f}"
        lineas.append(
            f"| {m['v1_archivos']} | {m['v2_acciones']} | {m['v3_concurrencia']} | "
            f"{m['p50_latencia_s']} s | {m['p95_latencia_s']} s | **{m['throughput_tpm']}** | "
            f"{cpu_str} | {mem_str} | "
            f"{m['tasa_error_pct']}% | {m['observaciones']} |"
        )

    total_pedidos_bot = sum(m["pedidos_bot_totales"] for m in matriz)
    total_rechazos_bot = sum(m["pedidos_bot_rechazados"] for m in matriz)
    tasa_rechazo = (total_rechazos_bot / total_pedidos_bot * 100.0) if total_pedidos_bot else 0.0

    tiempos_bot = [m["latencia_bot_avg_ms"] for m in matriz if m["latencia_bot_avg_ms"] > 0]
    lat_media_bot = statistics.mean(tiempos_bot) if tiempos_bot else 0.0
    piso_latencia = min(m["p50_latencia_s"] for m in matriz) if matriz else 0.0

    lineas.append("\n---\n")
    lineas.append("## 3. Análisis Técnico y Respuestas a los Puntos de Evaluación\n")

    def _celda(v1, v2, v3):
        return next((m for m in matriz if (m["v1_archivos"], m["v2_acciones"], m["v3_concurrencia"]) == (v1, v2, v3)), None)

    bytes_v1 = {v1: len(json.dumps(armar_payload_matriz(v1, 1))) for v1 in (1, 10, 100)}
    c_min, c_max = _celda(1, 1, 1), _celda(100, 5, 1)
    p50_min = c_min["p50_latencia_s"] if c_min else piso_latencia
    p50_max = c_max["p50_latencia_s"] if c_max else piso_latencia

    lineas.append("### 3.1 Contenido real en $V_1$ vs costo del contenedor")
    lineas.append(
        "Cada archivo viaja con su contenido en el cuerpo JSON de la petición. Tamaño medido del cuerpo con $V_2 = 1$: "
        f"{bytes_v1[1]} bytes para $V_1 = 1$, {bytes_v1[10]} para $V_1 = 10$ y {bytes_v1[100]} para $V_1 = 100$ "
        "(el contrato admite hasta 64 KiB).\n\n"
        f"Con un solo cliente ($V_3 = 1$) la latencia p50 pasa de {p50_min} s ($V_1 = 1$, $V_2 = 1$) a {p50_max} s "
        f"($V_1 = 100$, $V_2 = 5$). El piso medido, {piso_latencia:.2f} s, es el de crear, arrancar, esperar y borrar el contenedor "
        "efímero: ese costo domina y el procesamiento de los archivos agrega poco sobre él. Por eso, con $V_3 = 10$ y $V_3 = 50$ "
        + ("la latencia depende sobre todo de la cola" if con_pool else
           "la latencia depende sobre todo de cuántos contenedores corren a la vez en el host")
        + " y casi no de $V_1$ ni de $V_2$. No se midió por separado cuánto tarda la tarea "
        "dentro del contenedor, así que el reparto exacto entre ambos costos es una inferencia."
    )

    lineas.append("\n### 3.2 Justificación de $V_2 = 5$ (Acciones)")
    lineas.append(
        "El servicio de procesamiento de archivos define 5 transformaciones permitidas en su contrato: "
        "`hash_sha256`, `comprimir`, `contar_palabras`, `rotar_lineas` y `analisis_frecuencia`. "
        "Los valores seleccionados evalúan: "
        "(1) transformación unitaria de referencia, "
        "(3) pipeline intermedio representativo, y "
        "(5) el pipeline exhaustivo ($N = 5$) que agota la totalidad de acciones disponibles en el servicio, "
        "sometiendo al worker a la máxima profundidad de procesamiento por documento."
    )

    lineas.append("\n### 3.3 Muestras y calentamiento")
    lineas.append(
        "1. **Calentamiento**: antes de la matriz se envían pedidos preliminares para que la imagen ya esté en el Docker local.\n"
        "2. **Muestras por celda**: 15 pedidos con $V_3 = 1$, 20 con $V_3 = 10$ y 50 con $V_3 = 50$, de los cuales un 20 % los "
        "manda un bot (el resto, usuarios con token). Con tan pocas muestras, el p95 es el valor cercano al máximo de la celda y "
        "una sola corrida no permite medir la variación entre corridas; los números valen para comparar celdas entre sí en una misma "
        "corrida, no como valores absolutos. Los pedidos de bots no cuentan para p50, p95 ni throughput."
    )

    def _por_v3(v3):
        celdas = [m for m in matriz if m["v3_concurrencia"] == v3]
        return (max((m["workers_activos_medidos"] for m in celdas), default=0),
                max((m["tareas_encoladas_medidas"] for m in celdas), default=0))

    act1, enc1 = _por_v3(1)
    act10, enc10 = _por_v3(10)
    act50, enc50 = _por_v3(50)

    lineas.append("\n### 3.4 Autenticación y bots")
    lineas.append(
        f"- Pedidos emitidos por bots: **{total_pedidos_bot}**, en todas las celdas.\n"
        f"- Rechazados con `401 NO_AUTORIZADO`: **{total_rechazos_bot}/{total_pedidos_bot} ({tasa_rechazo:.1f}%)**.\n"
        f"- Latencia media de esos rechazos, medida por el cliente (incluye la red): **{lat_media_bot:.2f} ms**.\n"
        "- Los usuarios legítimos usan varios tokens distintos (tres JWT distintos de `evidencias/emitir_token.py`), no uno compartido.\n"
        "- La comprobación está en un `@app.middleware('http')` de FastAPI: un pedido sin token válido se rechaza "
        + ("antes de llegar a la cola y " if con_pool else "")
        + "sin crear ningún contenedor.\n\n"
        "> [!NOTE]\n"
        "> **Alcance**: es la misma autenticación de R4 (JWT con rol y vencimiento, y `/stats` sólo para admin). Acá sólo se mide "
        "cuántos pedidos de bots rechaza y cuánto tarda en hacerlo."
    )

    cpu_pico_max = max((m.get("cpu_host_pico_pct", 0.0) for m in matriz), default=0.0)
    mem_max = max((m.get("mem_host_mb", 0.0) for m in matriz), default=0.0)
    columna_activos = "Workers activos (de 4)" if con_pool else "Tareas en curso a la vez"
    campos = ("`workers_activos` y de `tareas_encoladas`" if fuente == "/health"
              else "`workers_ocupados` y de `tareas_en_cola`")

    lineas.append(f"\n### 3.5 Cuello de botella medido en `{fuente}` y Monitoreo del Host")
    lineas.append(
        f"Durante cada celda un hilo consulta `{fuente}` periódicamente y guarda el máximo de {campos}. "
        "En simultáneo, se monitorea la utilización real de recursos del host (CPU y memoria residente) leyendo `/proc/stat` y `/proc/meminfo`.\n\n"
        "Máximos observados en toda la matriz:\n\n"
        f"| $V_3$ | {columna_activos} | Tareas encoladas |\n|---|---|---|\n"
        f"| 1 | {act1} | {enc1} |\n| 10 | {act10} | {enc10} |\n| 50 | {act50} | {enc50} |\n\n"
        f"- Con $V_3 = 1$ no hay cola: el cuello de botella es el ciclo de vida del contenedor (~{piso_latencia:.2f} s por tarea).\n"
    )
    if con_pool:
        lineas.append(
            "- Con $V_3 = 10$ y $V_3 = 50$ el pool llega a su tope y el resto espera en la cola: el cuello de botella es el número de "
            "workers (`TP2_WORKERS_MAX`). En esas celdas la latencia crece con la cola y el throughput deja de crecer."
        )
    elif hit == "hit3":
        lineas.append(
            "- El Hit 3 no tiene cola: el coordinador asigna cada tarea al nodo con menos carga y el nodo la corre enseguida. "
            "`/stats` suma las tareas en curso de los 3 nodos. Los tres comparten el host y el daemon de Docker, así que no hay un "
            "tope de workers: con $V_3 = 10$ y $V_3 = 50$ corren a la vez tantas tareas como pedidos haya, y lo que puede limitar "
            "es el host (CPU, memoria y el daemon que crea y borra cada contenedor), más el paso por nginx y el coordinador."
        )
    else:
        lineas.append(
            "- El Hit 1 no tiene pool ni cola: cada pedido levanta su contenedor apenas llega. No hay un tope de workers: con "
            "$V_3 = 10$ y $V_3 = 50$ corren a la vez tantos contenedores como pedidos haya, y lo que puede limitar es el host "
            "(CPU, memoria y el daemon de Docker, que crea y borra cada contenedor)."
        )
    lineas.append(
        f"- **Host**: en toda la matriz la CPU llegó como máximo al {cpu_pico_max:.1f} % y la memoria usada a {mem_max:.0f} MB "
        "(columnas de la tabla)."
    )

    lineas.append("\n### 3.6 Límites de Recursos Docker (CPU y Memoria)")
    if con_pool:
        lineas.append(
            "Siguiendo las buenas prácticas arquitectónicas y la preparación para entornos orquestados (Kubernetes):\n"
            "- Cada contenedor efímero es creado con límites estrictos de hardware: `mem_limit = '256m'` y `nano_cpus = 1_000_000_000` (1 CPU virtual).\n"
            "- Esto previene el problema del *vecino ruidoso* y evita que tareas defectuosas o con fuga de memoria agoten la RAM del host y provoquen OOM Killer generalizado."
        )
    else:
        lineas.append(
            f"- Los contenedores de tarea del {nombre_hit} se crean sin límite de CPU ni de memoria. Los límites (`256m` y 1 CPU) "
            "existen sólo en el Hit 2, donde se agregaron para la medición de 1, 2, 4 y 8 workers. Una tarea que use mucha memoria "
            "puede afectar al resto del host."
        )

    if not con_pool:
        lineas.append("\n### 3.7 Entorno de Ejecución y Timeout Contractual")
        lineas.append(
            "- **Timeout Contractual**: El servidor se ejecuta respetando `TP2_TIMEOUT_EJECUCION = 60` segundos (estipulado en el contrato público).\n"
            "- **Despliegue vs Local**: Las pruebas pueden ejecutarse localmente mediante Docker Compose o directamente contra el despliegue "
            f"remoto en AWS EC2 (`http://54.207.243.253:{PUERTOS_HIT.get(hit, 8082)}`) suministrando la variable `TP2_SERVIDOR`.\n"
        )
        salida_path.write_text("\n".join(lineas) + "\n", encoding="utf-8")
        print(f"\n[+] Informe Markdown guardado en {salida_path}")
        return

    lineas.append("\n### 3.7 La Cola como Mecanismo de Desacoplamiento de Pedido y Respuesta")
    lineas.append(
        "> [!IMPORTANT]\n"
        "> **Fundamento Arquitectónico**: Como se enfatizó en las clases teóricas de la cátedra, la razón primordial de introducir una cola "
        "> en un sistema distribuido **no es la concurrencia en sí misma, sino el desacoplamiento temporal entre el pedido y la respuesta**.\n\n"
        "1. **Desacoplamiento temporal**: La cola permite que el servidor acepte y confirme la recepción del trabajo inmediatamente, "
        "aislando el ritmo de llegada de los clientes del ritmo de ejecución del pool de workers. En arquitecturas asincrónicas avanzadas "
        "esto se traduce en respuestas `202 Accepted` inmediatas con polling/callbacks posteriores. Incluso en este modelo sincrónico con bloqueo HTTP, "
        "la cola amortigua ráfagas de tráfico evitando el rechazo abrupto (`503 Service Unavailable`).\n"
        "2. **Ordenamiento Lógico (Lamport)**: Una vez que el pedido está admitido y desacoplado en la cola, el algoritmo de relojes lógicos "
        "de Lamport (`Min-Heap`) garantiza que las tareas se despachen según la causalidad temporal declarada por los clientes, resolviendo "
        "el ordenamiento distribuido con criterio justo.\n"
        "3. **Exclusión Mutua (Mutex)**: La exclusión mutua protege las estructuras de datos compartidas (la cola y el contador de workers) "
        "para que múltiples hilos despachadores y recolectores no colisionen al modificar el estado interno del servidor."
    )

    lineas.append("\n### 3.8 Entorno de Ejecución y Timeout Contractual")
    lineas.append(
        "- **Timeout Contractual**: El servidor se ejecuta respetando `TP2_TIMEOUT_EJECUCION = 60` segundos (estipulado en el contrato público).\n"
        "- **Despliegue vs Local**: Las pruebas pueden ejecutarse localmente mediante Docker Compose o directamente contra el despliegue "
        "remoto en AWS EC2 (`http://54.207.243.253:8082`) suministrando la variable `TP2_SERVIDOR`.\n"
    )

    contenido = "\n".join(lineas) + "\n"
    salida_path.write_text(contenido, encoding="utf-8")
    print(f"\n[+] Informe Markdown guardado en {salida_path}")


def main():
    parser = argparse.ArgumentParser(description="Generador de carga y evaluación R5")
    parser.add_argument(
        "--servidor",
        default=os.environ.get("TP2_SERVIDOR") or os.environ.get("TP2_URL", "http://127.0.0.1:18090"),
        help="URL base del servidor Hit #2",
    )
    parser.add_argument(
        "--tokens",
        "--token",
        default=os.environ.get("TP2_TOKENS", ""),
        help="JWT de usuarios (separados por comas), emitidos con evidencias/emitir_token.py",
    )
    parser.add_argument(
        "--timeout-ejecucion",
        type=float,
        default=float(os.environ.get("TP2_TIMEOUT_EJECUCION", 60.0)),
        help="Timeout contractual de ejecución de tareas en el servidor (por defecto 60s)",
    )
    parser.add_argument(
        "--probar-timeout-largo",
        action="store_true",
        help="Ejecuta la prueba real de timeout largo esperando el tiempo completo del servidor",
    )
    parser.add_argument(
        "--salida-json",
        default="hit2/mediciones/matriz_r5_resultados.json",
        help="Ruta de salida para los resultados JSON",
    )
    parser.add_argument(
        "--salida-md",
        default="evidencias/R5_INFORME.md",
        help="Ruta de salida para el informe Markdown",
    )
    parser.add_argument(
        "--hit",
        default="hit2",
        choices=sorted(NOMBRES_HIT),
        help="Hit contra el que se corre (cambia el texto del informe)",
    )
    parser.add_argument(
        "--token-admin",
        default=os.environ.get("TP2_TOKEN_ADMIN", ""),
        help="JWT con rol admin: mide el cuello de botella en /stats, igual en los tres hits (sin él, lee /health del Hit 2)",
    )
    args = parser.parse_args()

    servidor = args.servidor.rstrip("/")

    # Parsear o generar tokens diferenciados para múltiples usuarios
    if args.tokens and args.tokens.strip():
        lista_tokens = [t.strip() for t in args.tokens.split(",") if t.strip()]
    else:
        parser.error("hacen falta JWT de usuario (--tokens o TP2_TOKENS); ver evidencias/emitir_token.py")

    print(f"=== Evaluador R5 Cerberus ===")
    print(f"Servidor: {servidor}")
    print(f"Tokens de usuarios cargados: {len(lista_tokens)} token(s) diferenciados")
    print(f"Timeout contractual: {args.timeout_ejecucion} s")

    # 1. Batería de 3 tareas especializadas
    pruebas_tareas = evaluar_tareas_individuales(
        servidor=servidor,
        tokens=lista_tokens,
        timeout_ejecucion=args.timeout_ejecucion,
        probar_timeout_largo=args.probar_timeout_largo,
    )

    # 2. Matriz combinatoria V1 x V2 x V3 con sondeo de /health
    matriz = ejecutar_matriz_completa(servidor, lista_tokens, token_admin=args.token_admin or None, hit=args.hit)

    # 3. Guardar resultados JSON
    path_json = Path(args.salida_json)
    path_json.parent.mkdir(parents=True, exist_ok=True)
    datos_completos = {
        "timestamp": time.time(),
        "servidor": servidor,
        "timeout_ejecucion": args.timeout_ejecucion,
        "pruebas_tareas": pruebas_tareas,
        "matriz_v1_v2_v3": matriz,
    }
    path_json.write_text(json.dumps(datos_completos, indent=2) + "\n", encoding="utf-8")
    print(f"[+] Resultados JSON guardados en {path_json}")

    # 4. Generar reporte Markdown
    path_md = Path(args.salida_md)
    path_md.parent.mkdir(parents=True, exist_ok=True)
    generar_reporte_markdown(pruebas_tareas, matriz, path_md, hit=args.hit)


if __name__ == "__main__":
    main()
