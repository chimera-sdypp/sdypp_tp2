"""Medición del throughput del Hit 2 con 1, 2, 4 y 8 workers.

Para cada N reinicia el servidor con TP2_WORKERS_MAX=N (docker compose, con el .env de hit2/),
le manda la misma carga de una vez (así la cola nunca se vacía) y mide cuánto tarda en
completarla. Repite cada medición, ajusta la ley de Amdahl a los speedups medidos y guarda
los resultados y el gráfico en hit2/mediciones/.

    cd hit2 && python3 cliente/benchmark.py        # con hit2/.env completo (ver README)
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).parent))
from cliente import ClienteHit2

HIT2 = Path(__file__).resolve().parent.parent
WORKERS = [1, 2, 4, 8]


def levantar_servidor(workers):
    """Recrea el servidor con TP2_WORKERS_MAX=workers y espera a que esté healthy."""
    entorno = {**os.environ, "TP2_WORKERS_MAX": str(workers)}
    subprocess.run(["docker", "compose", "-f", str(HIT2 / "docker-compose.yml"), "up", "-d", "--wait"],
                   env=entorno, check=True, capture_output=True)


def medir(servidor, imagen, tareas):
    """Manda `tareas` sumas a la vez. Devuelve (segundos totales, exitosas, latencia media)."""
    def una(i):
        inicio = time.perf_counter()
        respuesta = ClienteHit2(servidor).enviar_tarea("suma", {"a": i, "b": 1}, imagen=imagen, timeout=600)
        return respuesta["codigo"] == 200, time.perf_counter() - inicio

    inicio = time.perf_counter()
    with ThreadPoolExecutor(max_workers=tareas) as hilos:
        resultados = list(hilos.map(una, range(tareas)))
    return time.perf_counter() - inicio, sum(ok for ok, _ in resultados), statistics.mean(t for _, t in resultados)


def amdahl(n, p):
    return 1 / ((1 - p) + p / n)


def ajustar_fraccion_paralela(filas):
    """La P de Amdahl que mejor explica los speedups medidos (mínimos cuadrados, paso 0,001)."""
    return min((i / 1000 for i in range(1001)),
               key=lambda p: sum((f["speedup"] - amdahl(f["workers"], p)) ** 2 for f in filas))


def graficar(filas, p, destino):
    workers = [f["workers"] for f in filas]
    plt.figure(figsize=(8, 5), dpi=120)
    plt.plot(workers, workers, "k--", label="Ideal (S = N)")
    plt.plot(workers, [amdahl(n, p) for n in workers], "r-.", label=f"Amdahl ajustada (P = {p:.0%})")
    plt.plot(workers, [f["speedup"] for f in filas], "bo-", label="Medido")
    for f in filas:
        plt.annotate(f"{f['speedup']}x · {f['throughput_tpm']:.0f} tpm", (f["workers"], f["speedup"]),
                     textcoords="offset points", xytext=(0, 9), ha="center")
    plt.title("Hit 2: speedup según la cantidad de workers")
    plt.xlabel("Workers (N)")
    plt.ylabel("Speedup respecto de 1 worker")
    plt.xticks(workers)
    plt.grid(True, linestyle=":", alpha=0.6)
    plt.legend()
    plt.tight_layout()
    plt.savefig(destino)
    plt.close()


def main():
    parser = argparse.ArgumentParser(description="Throughput del Hit 2 con 1, 2, 4 y 8 workers")
    parser.add_argument("--servidor", default=os.environ.get("TP2_SERVIDOR", "http://127.0.0.1:8082"))
    parser.add_argument("--imagen", default="cerberusdistribuido/tarea:1.0.0")
    parser.add_argument("--tareas", type=int, default=32, help="tareas por medición")
    parser.add_argument("--repeticiones", type=int, default=3)
    args = parser.parse_args()

    filas = []
    for n in WORKERS:
        levantar_servidor(n)
        # Calentamiento: la primera tarea puede incluir el pull de la imagen.
        ClienteHit2(args.servidor).enviar_tarea("suma", {"a": 0, "b": 0}, imagen=args.imagen, timeout=600)
        corridas = [medir(args.servidor, args.imagen, args.tareas) for _ in range(args.repeticiones)]
        if any(exitosas != args.tareas for _, exitosas, _ in corridas):
            sys.exit(f"con {n} workers fallaron tareas: {corridas}")
        segundos = [s for s, _, _ in corridas]
        filas.append({
            "workers": n,
            "segundos": round(statistics.mean(segundos), 2),
            "desvio_segundos": round(statistics.stdev(segundos), 2) if len(segundos) > 1 else 0.0,
            "throughput_tpm": round(args.tareas / statistics.mean(segundos) * 60, 1),
            "latencia_media_s": round(statistics.mean(l for _, _, l in corridas), 2),
        })
        print(f"{n} workers: {filas[-1]}")

    for f in filas:
        f["speedup"] = round(f["throughput_tpm"] / filas[0]["throughput_tpm"], 2)
    p = ajustar_fraccion_paralela(filas)

    salida = HIT2 / "mediciones"
    salida.mkdir(exist_ok=True)
    resumen = {"tareas_por_medicion": args.tareas, "repeticiones": args.repeticiones,
               "fraccion_paralela_amdahl": p, "resultados": filas}
    (salida / "resultados_benchmark.json").write_text(json.dumps(resumen, indent=2) + "\n", encoding="utf-8")
    graficar(filas, p, salida / "escalabilidad.png")
    print(f"P de Amdahl ajustada: {p:.3f}. Resultados y gráfico en {salida}")


if __name__ == "__main__":
    main()
