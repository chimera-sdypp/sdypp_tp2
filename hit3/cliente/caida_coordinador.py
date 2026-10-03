"""Medición del tiempo de recuperación del Hit 3 ante la caída del coordinador.

Con el cluster levantado (docker compose, con el .env de hit3/), en cada repetición:

1. Espera a que el cluster esté estable: todos ven al mismo coordinador y los 3 nodos vivos.
2. Arranca carga continua: varios clientes mandando tareas por nginx, sin pausa.
3. Mata el proceso del coordinador (`docker kill`, SIGKILL) y anota el instante.
4. Sigue mandando tareas hasta que pasa la caída, y mira por nginx cuándo se ve el líder nuevo.
5. Levanta otra vez el nodo y espera a que recupere el puesto (es el de ID mayor).

Los instantes de la detección y la elección salen de los logs de los nodos, con la marca
de tiempo que pone Docker a cada línea (`docker compose logs -t`): todos los contenedores
comparten el reloj del host, así que se pueden restar con el instante del kill.

Guarda en hit3/mediciones/ los resultados (<nombre>.json), el log de la primera elección
(<nombre>_eleccion.log) y el gráfico (<nombre>.png). Dos escenarios:

    cd hit3   # con hit3/.env completo
    python3 cliente/caida_coordinador.py --nombre con_carga                 # 3 clientes sin pausa
    python3 cliente/caida_coordinador.py --nombre sin_carga --clientes 0    # sólo el heartbeat
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).parent))
from cliente import enviar_tarea

HIT3 = Path(__file__).resolve().parent.parent
NODOS = [1, 2, 3]
# Docker anota FinishedAt ~1 ms después de que el proceso muere: lo que pasa en ese
# lapso (la primera conexión cortada, la primera detección) queda "antes" del kill.
# Se miran los eventos desde un poco antes; en ese margen no hay otra cosa que la caída.
MARGEN = 0.05


# ---------------------------------------------------------------- cluster
class Cluster:
    def __init__(self, servidor, env_file):
        self.servidor = servidor.rstrip("/")
        self._compose = ["docker", "compose", "-f", str(HIT3 / "docker-compose.yml")]
        if env_file:
            self._compose += ["--env-file", env_file]

    def compose(self, *argumentos):
        return subprocess.run([*self._compose, *argumentos], check=True, capture_output=True, text=True).stdout

    def contenedor(self, nodo):
        return self.compose("ps", "-a", "-q", f"nodo{nodo}").strip()

    def cluster(self):
        """El bloque `cluster` del /health del nodo que elija nginx ({} si no contesta)."""
        import urllib.request
        try:
            with urllib.request.urlopen(self.servidor + "/health", timeout=2) as respuesta:
                return json.loads(respuesta.read())["contenido"]["cluster"]
        except Exception:  # noqa: BLE001 (502 de nginx, timeout, nodo arrancando: todo es "todavía no")
            return {}

    def estable(self, coordinador):
        """Todos los que contestan ven a `coordinador`, y su registro tiene los 3 vivos."""
        vistos = [self.cluster() for _ in range(6)]  # round-robin: pasa por los 3
        if not all(v.get("coordinador") == coordinador for v in vistos):
            return False
        registro = next((v["nodos"] for v in vistos if v.get("rol") == "coordinador"), {})
        return len(registro) == len(NODOS) and all(n["estado"] == "vivo" for n in registro.values())

    def esperar(self, condicion, timeout=60.0, descripcion="la condición"):
        inicio = time.time()
        while not condicion():
            if time.time() - inicio > timeout:
                sys.exit(f"no se cumplió {descripcion} en {timeout:g} s")
            time.sleep(0.05)
        return time.time()

    def logs(self, desde):
        """[(instante, nodo, texto)] de los 3 nodos desde `desde` (epoch)."""
        iso = datetime.fromtimestamp(desde, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        lineas = []
        for nodo in NODOS:
            crudo = self.compose("logs", "-t", "--no-color", "--no-log-prefix", "--since", iso, f"nodo{nodo}")
            for linea in crudo.splitlines():
                marca, _, texto = linea.partition(" ")
                lineas.append((_epoch(marca), nodo, texto))
        return sorted(lineas)


def _epoch(marca):
    """2026-10-03T04:13:17.170123456Z → epoch (Python sólo lee hasta microsegundos)."""
    base, _, resto = marca.rstrip("Z").partition(".")
    return datetime.fromisoformat(f"{base}.{resto[:6]:0<6}+00:00").timestamp()


# ---------------------------------------------------------------- carga
class Carga:
    """`clientes` hilos mandando tareas sin pausa hasta `detener()`."""

    def __init__(self, servidor, imagen, clientes):
        self.servidor, self.imagen = servidor, imagen
        self.resultados = []  # (envio, respuesta, codigo, nodo o tipo de error)
        self._detener = threading.Event()
        self._hilos = [threading.Thread(target=self._cliente, args=(i,), daemon=True) for i in range(clientes)]

    def _cliente(self, i):
        while not self._detener.is_set():
            envio = time.time()
            try:
                sobre = enviar_tarea(self.servidor, "suma", {"a": i, "b": 1}, {}, self.imagen, timeout=60)
                codigo = sobre["codigo"]
                detalle = sobre["contenido"].get("nodo") if codigo == 200 else sobre["contenido"]["error"]["tipo"]
            except Exception as error:  # noqa: BLE001 (sin respuesta: también es un resultado)
                codigo, detalle = 0, type(error).__name__
            self.resultados.append((envio, time.time(), codigo, detalle))

    def iniciar(self):
        for hilo in self._hilos:
            hilo.start()

    def detener(self):
        self._detener.set()
        for hilo in self._hilos:
            hilo.join()


class Vigia:
    """Pregunta por nginx quién es el coordinador, sin pausa, desde antes del kill.

    Si se empezara a preguntar después de `docker kill`, la medición arrastraría lo que
    tarda el CLI de Docker en volver (~150 ms)."""

    def __init__(self, cluster):
        self._cluster = cluster
        self.vistas = []  # (instante de la respuesta, coordinador)
        self._detener = threading.Event()
        self._hilo = threading.Thread(target=self._mirar, daemon=True)

    def _mirar(self):
        while not self._detener.is_set():
            coordinador = self._cluster.cluster().get("coordinador")
            self.vistas.append((time.time(), coordinador))
            time.sleep(0.01)

    def iniciar(self):
        self._hilo.start()

    def detener(self):
        self._detener.set()
        self._hilo.join()

    def primera(self, desde, coordinador):
        return next((t for t, c in self.vistas if t >= desde and c == coordinador), None)


# ---------------------------------------------------------------- una repetición
def _primero(lineas, desde, patron, nodos=NODOS):
    patron = re.compile(patron)
    return next((t for t, n, texto in lineas if t >= desde and n in nodos and patron.search(texto)), None)


def repeticion(cluster, args):
    actual = cluster.cluster().get("coordinador")
    victima = actual
    sobrevivientes = [n for n in NODOS if n != victima]
    nuevo = max(sobrevivientes)  # Bully: el de ID mayor entre los vivos

    carga = Carga(cluster.servidor, args.imagen, args.clientes)
    vigia = Vigia(cluster)
    carga.iniciar()
    vigia.iniciar()
    time.sleep(args.antes)  # carga en régimen antes de la caída

    contenedor = cluster.contenedor(victima)
    subprocess.run(["docker", "kill", contenedor], check=True, capture_output=True)
    # El instante exacto en que murió el proceso, del mismo reloj que los logs. No sirve
    # tomar la hora al volver `docker kill`: el CLI tarda, y para entonces la elección
    # puede haber terminado (una tarea en curso detecta la caída en milisegundos).
    t_kill = _epoch(subprocess.run(["docker", "inspect", "-f", "{{.State.FinishedAt}}", contenedor],
                                   check=True, capture_output=True, text=True).stdout.strip())
    cluster.esperar(lambda: vigia.primera(t_kill, nuevo), 30, "el líder nuevo")
    time.sleep(args.despues)
    carga.detener()
    vigia.detener()
    t_visto = vigia.primera(t_kill, nuevo)

    # Vuelta del nodo: levanta otra vez el mismo contenedor y recupera el puesto.
    t_start = time.time()
    subprocess.run(["docker", "start", contenedor], check=True, capture_output=True)
    cluster.esperar(lambda: cluster.estable(victima), 60, "la vuelta del nodo")

    lineas = cluster.logs(t_kill - 1)
    desde = t_kill - MARGEN
    t_deteccion = _primero(lineas, desde, r"no responde", sobrevivientes)
    t_eleccion = _primero(lineas, desde, r"inicia elección", sobrevivientes)
    t_lider = _primero(lineas, desde, rf"nodo {nuevo} es el nuevo COORDINADOR", [nuevo])
    t_aceptan = max(_primero(lineas, desde, rf"acepta COORDINADOR {nuevo}", [n]) or 0
                    for n in sobrevivientes if n != nuevo)
    t_vuelta = _primero(lineas, t_start, rf"nodo {victima} es el nuevo COORDINADOR", [victima])
    t_arranque = _primero(lineas, t_start, r"arranca el nodo", [victima])
    reasignadas = sum(1 for t, _, texto in lineas
                      if desde <= t < t_start and f"se cayó el nodo {victima}" in texto)

    # Tareas afectadas: las que estaban en vuelo o se mandaron entre el kill y el líder nuevo.
    afectadas = [r for r in carga.resultados if r[0] <= t_lider and r[1] >= desde]
    normales = [r[1] - r[0] for r in carga.resultados if r[1] < desde and r[2] == 200]
    ms = lambda a, b: max(0, round((a - b) * 1000)) if a and b else None  # noqa: E731
    fila = {
        "coordinador_caido": victima,
        "coordinador_nuevo": nuevo,
        "deteccion_ms": ms(t_deteccion, t_kill),
        "eleccion_ms": ms(t_lider, t_eleccion),
        "lider_nuevo_ms": ms(t_lider, t_kill),
        "todos_aceptan_ms": ms(t_aceptan, t_kill),
        "visto_por_nginx_ms": ms(t_visto, t_kill),
        "vuelta_arranque_ms": ms(t_arranque, t_start),
        "vuelta_lider_ms": ms(t_vuelta, t_start),
        "tareas_total": len(carga.resultados),
        "tareas_afectadas": len(afectadas),
        "afectadas_ok": sum(1 for r in afectadas if r[2] == 200),
        "afectadas_fallidas": sorted(f"{r[2]} {r[3]}" for r in afectadas if r[2] != 200),
        "reasignadas": reasignadas,
        "latencia_normal_ms": round(statistics.median(normales) * 1000) if normales else None,
        "latencia_max_afectadas_ms": round(max(r[1] - r[0] for r in afectadas) * 1000) if afectadas else None,
    }
    detalle = {"t_kill": t_kill, "eventos": {"deteccion": t_deteccion, "lider": t_lider, "visto": t_visto},
               "tareas": carga.resultados,
               "log": [(t, n, texto) for t, n, texto in lineas if desde <= t <= t_lider + 0.05
                       and ("bully" in texto or "cluster |" in texto)]}
    return fila, detalle


# ---------------------------------------------------------------- salida
def resumen(filas):
    claves = ["deteccion_ms", "eleccion_ms", "lider_nuevo_ms", "todos_aceptan_ms", "visto_por_nginx_ms",
              "vuelta_arranque_ms", "vuelta_lider_ms"]
    salida = {}
    for clave in claves:
        valores = [f[clave] for f in filas if f[clave] is not None]
        if valores:
            salida[clave] = {"media": round(statistics.mean(valores)), "min": min(valores), "max": max(valores),
                             "desvio": round(statistics.stdev(valores)) if len(valores) > 1 else 0}
    return salida


def graficar(filas, detalle, destino):
    figura, (linea, barras) = plt.subplots(1, 2, figsize=(13, 5), dpi=120,
                                           gridspec_kw={"width_ratios": [3, 2]})
    t0 = detalle["t_kill"]
    for envio, respuesta, codigo, _ in detalle["tareas"]:
        color = "tab:green" if codigo == 200 else "tab:red"
        linea.plot([envio - t0, respuesta - t0], [respuesta - envio] * 2, color=color, alpha=0.6, lw=2)
    for nombre, t, color in [("kill", t0, "k"), ("detección", detalle["eventos"]["deteccion"], "C1"),
                             ("líder nuevo", detalle["eventos"]["lider"], "C0")]:
        if t:
            linea.axvline(t - t0, color=color, ls="--", label=f"{nombre} ({(t - t0) * 1000:.0f} ms)")
    linea.set_title("Una caída: cada tarea, de su envío a su respuesta")
    linea.set_xlabel("segundos desde el kill del coordinador")
    linea.set_ylabel("latencia de la tarea (s)")
    linea.plot([], [], color="tab:green", lw=2, label="tarea OK")
    linea.plot([], [], color="tab:red", lw=2, label="tarea fallida")
    linea.legend(loc="upper left", fontsize=8)
    linea.grid(True, linestyle=":", alpha=0.6)

    indices = range(1, len(filas) + 1)
    deteccion = [f["deteccion_ms"] or 0 for f in filas]
    resto = [(f["lider_nuevo_ms"] or 0) - d for f, d in zip(filas, deteccion)]
    barras.bar(indices, deteccion, label="detección", color="C1")
    barras.bar(indices, resto, bottom=deteccion, label="elección", color="C0")
    barras.plot(indices, [f["visto_por_nginx_ms"] for f in filas], "kx", label="visto por un cliente")
    barras.set_title("Tiempo hasta el líder nuevo, por repetición")
    barras.set_xlabel("repetición")
    barras.set_ylabel("ms desde el kill")
    barras.set_xticks(list(indices))
    barras.legend(fontsize=8)
    barras.grid(True, axis="y", linestyle=":", alpha=0.6)
    figura.tight_layout()
    figura.savefig(destino)
    plt.close(figura)


def main():
    parser = argparse.ArgumentParser(description="Tiempo de recuperación ante la caída del coordinador")
    parser.add_argument("--servidor", default="http://127.0.0.1:8080", help="nginx del cluster")
    parser.add_argument("--env-file", help="el .env del compose (por defecto, hit3/.env)")
    parser.add_argument("--imagen", default="cerberusdistribuido/tarea:1.0.0")
    parser.add_argument("--repeticiones", type=int, default=5)
    parser.add_argument("--clientes", type=int, default=3,
                        help="clientes mandando tareas sin pausa (0: sin carga, sólo detecta el heartbeat)")
    parser.add_argument("--nombre", default="recuperacion", help="prefijo de los archivos de resultados")
    parser.add_argument("--antes", type=float, default=3.0, help="segundos de carga antes del kill")
    parser.add_argument("--despues", type=float, default=3.0, help="segundos de carga después del líder nuevo")
    args = parser.parse_args()

    cluster = Cluster(args.servidor, args.env_file)
    cluster.esperar(lambda: cluster.estable(max(NODOS)), 60, "un cluster estable")
    # Calentamiento: la primera tarea puede incluir el pull de la imagen.
    enviar_tarea(cluster.servidor, "suma", {"a": 0, "b": 0}, {}, args.imagen, timeout=600)

    filas, detalles = [], []
    for i in range(1, args.repeticiones + 1):
        fila, detalle = repeticion(cluster, args)
        filas.append(fila)
        detalles.append(detalle)
        print(f"repetición {i}: detección {fila['deteccion_ms']} ms · elección {fila['eleccion_ms']} ms · "
              f"líder nuevo {fila['lider_nuevo_ms']} ms · visto por nginx {fila['visto_por_nginx_ms']} ms · "
              f"tareas afectadas {fila['tareas_afectadas']} (fallidas: {len(fila['afectadas_fallidas'])}, "
              f"reasignadas: {fila['reasignadas']})")

    salida = HIT3 / "mediciones"
    salida.mkdir(exist_ok=True)
    datos = {"repeticiones": args.repeticiones, "clientes": args.clientes, "imagen": args.imagen,
             "intervalo_heartbeat_s": 1.0,
             "resumen": resumen(filas), "resultados": filas}
    (salida / f"{args.nombre}.json").write_text(json.dumps(datos, indent=2, ensure_ascii=False) + "\n",
                                              encoding="utf-8")
    # El log de la primera elección: de ahí sale el diagrama de secuencia del informe.
    t0 = detalles[0]["t_kill"]
    with open(salida / f"{args.nombre}_eleccion.log", "w", encoding="utf-8") as archivo:
        archivo.write(f"# kill del nodo {filas[0]['coordinador_caido']} en t = 0 ms\n")
        for t, nodo, texto in detalles[0]["log"]:
            archivo.write(f"{(t - t0) * 1000:+8.1f} ms  nodo{nodo}  {texto.split(' | ', 2)[-1]}\n")
    graficar(filas, detalles[0], salida / f"{args.nombre}.png")
    print(json.dumps(datos["resumen"], indent=2))
    print(f"Resultados en {salida}")


if __name__ == "__main__":
    main()
