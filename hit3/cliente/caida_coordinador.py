"""Medición Experimental del Tiempo de Recuperación ante Caída del Coordinador (Hit 3).

Este módulo constituye el arnés de prueba de tolerancia a fallas y resiliencia en caliente
del cluster distribuido. Evalúa experimentalmente el comportamiento del algoritmo de
elección de Bully (García-Molina, 1982) y la capacidad de conmutación por error (failover)
del balanceador de carga cuando el nodo líder sufre una caída abrupta (crash failure / SIGKILL).

Metodología de Medición Rigurosa en Cada Repetición:
1. Verificación de Estabilidad Inicial:
   Verifica mediante sondeo round-robin sobre Nginx que los 3 nodos (nodo1, nodo2, nodo3)
   estén vivos y en consenso absoluto respecto a la identidad del coordinador (inicialmente nodo 3).
2. Generación de Carga Continua Concurrente:
   Lanza un conjunto de hilos clientes enviando tareas aritméticas a `/getRemoteTask`
   a través del balanceador Nginx sin pausas intermedias.
3. Inyección de Falla Imprevista (Crash Failure):
   Mata abruptamente el contenedor del coordinador con `docker kill` (señal SIGKILL inmediata,
   sin permitir un apagado ordenado o gracia TCP).
4. Medición Temporal de Precisión:
   - Obtiene el instante exacto del deceso mediante `State.FinishedAt` del daemon de Docker.
   - Registra el momento en que los nodos sobrevivientes detectan el timeout o la desconexión.
   - Registra el inicio de la elección Bully y la emisión de mensajes `COORDINADOR`.
   - Registra el instante en que el nuevo líder es visible a los clientes externos vía Nginx.
5. Reincorporación del Nodo Caído (Preemption / Vuelta a la Vida):
   Reinicia el contenedor caído (`docker start`) y valida que, al poseer el ID más alto del cluster,
   inicie una elección y recupere legítimamente el liderazgo sin interrumpir el servicio.
6. Exportación de Resultados y Métricas:
   Genera reportes JSON detallados en `hit3/mediciones/<nombre>.json`, trazas de auditoría
   de eventos en `<nombre>_eleccion.log`, y gráficos comparativos de latencia en `<nombre>.png`.

Ejemplos de Ejecución:
    cd hit3
    python3 cliente/caida_coordinador.py --nombre con_carga --clientes 3
    python3 cliente/caida_coordinador.py --nombre sin_carga --clientes 0
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

# Incorporar la raíz del cliente al path para importar el cliente HTTP
sys.path.insert(0, str(Path(__file__).parent))
from cliente import enviar_tarea

# Constantes del entorno distribuido
HIT3 = Path(__file__).resolve().parent.parent
NODOS = [1, 2, 3]

# Margen temporal en segundos: Docker anota State.FinishedAt ~1 ms después de la muerte del proceso.
# Los eventos ocurridos en ese micro-lapso (como una conexión cortada) quedan registrados ligeramente
# antes del timestamp oficial de Docker. Este margen asegura no omitir ningún evento correlacionado.
MARGEN = 0.05


# ==============================================================================
# 1. Administrador del Ciclo de Vida del Cluster Docker Compose
# ==============================================================================
class Cluster:
    """Controlador programático del cluster Docker Compose y observador de estado."""

    def __init__(self, servidor: str, env_file: str | None) -> None:
        """Inicializa las rutas base y los comandos de Docker Compose."""
        self.servidor = servidor.rstrip("/")
        # Comando base apuntando al archivo docker-compose.yml del Hit 3
        self._compose = ["docker", "compose", "-f", str(HIT3 / "docker-compose.yml")]
        if env_file:
            self._compose += ["--env-file", env_file]

    def compose(self, *argumentos: str) -> str:
        """Ejecuta un subcomando de Docker Compose de forma síncrona y retorna stdout."""
        return subprocess.run(
            [*self._compose, *argumentos],
            check=True,
            capture_output=True,
            text=True
        ).stdout

    def contenedor(self, nodo: int) -> str:
        """Obtiene el identificador de contenedor Docker (SHA) para un nodo específico."""
        return self.compose("ps", "-a", "-q", f"nodo{nodo}").strip()

    def cluster(self) -> dict:
        """Consulta el bloque `cluster` del endpoint `/health` del nodo alcanzado por Nginx.
        
        Dado que Nginx balancea en modo round-robin, consultas sucesivas permiten observar
        el estado interno visto por distintos nodos del cluster.
        """
        import urllib.request
        try:
            with urllib.request.urlopen(self.servidor + "/health", timeout=2) as respuesta:
                return json.loads(respuesta.read())["contenido"]["cluster"]
        except Exception:  # noqa: BLE001 (502 Bad Gateway de Nginx, timeouts o nodos arrancando)
            return {}

    def estable(self, coordinador: int) -> bool:
        """Verifica que el cluster se encuentre en estado estacionario y con consenso pleno.
        
        Realiza 6 consultas consecutivas para recorrer todos los nodos tras Nginx y comprueba:
        1. Que todos los nodos observados reconozcan al mismo `coordinador`.
        2. Que el registro soft-state del coordinador tenga registrados a los 3 nodos como 'vivo'.
        """
        vistos = [self.cluster() for _ in range(6)]
        # Todos deben reportar al coordinador esperado
        if not all(v.get("coordinador") == coordinador for v in vistos):
            return False
        # Extraer la tabla de membresía del nodo que ejerce como coordinador
        registro = next((v["nodos"] for v in vistos if v.get("rol") == "coordinador"), {})
        # Deben figurar los 3 nodos y todos deben encontrarse en estado 'vivo'
        return len(registro) == len(NODOS) and all(n["estado"] == "vivo" for n in registro.values())

    def esperar(self, condicion, timeout: float = 60.0, descripcion: str = "la condición") -> float:
        """Espera activamente hasta que una condición retorne True o expire el timeout."""
        inicio = time.time()
        while not condicion():
            if time.time() - inicio > timeout:
                sys.exit(f"Falla de sincronización: no se cumplió {descripcion} en {timeout:g} s")
            time.sleep(0.05)
        return time.time()

    def logs(self, desde: float) -> list[tuple[float, int, str]]:
        """Extrae y ordena cronológicamente los logs de los 3 nodos desde un instante epoch dado.
        
        Aprovecha que Docker añade timestamps UTC de alta precisión a cada línea de log
        (`docker compose logs -t`) compartiendo el mismo reloj del kernel del host.
        """
        iso = datetime.fromtimestamp(desde, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        lineas = []
        for nodo in NODOS:
            crudo = self.compose("logs", "-t", "--no-color", "--no-log-prefix", "--since", iso, f"nodo{nodo}")
            for linea in crudo.splitlines():
                marca, _, texto = linea.partition(" ")
                lineas.append((_epoch(marca), nodo, texto))
        # Retornar ordenados por timestamp absoluto
        return sorted(lineas)


def _epoch(marca: str) -> float:
    """Convierte un timestamp ISO 8601 de Docker (ej. '2026-10-03T04:13:17.170123456Z') a segundos epoch.
    
    Dado que Python sólo soporta hasta microsegundos (6 decimales), se trunca la fracción a 6 dígitos.
    """
    base, _, resto = marca.rstrip("Z").partition(".")
    return datetime.fromisoformat(f"{base}.{resto[:6]:0<6}+00:00").timestamp()


# ==============================================================================
# 2. Inyector de Carga Concurrente (Clientes Sintéticos)
# ==============================================================================
class Carga:
    """Generador de carga concurrente: despacha peticiones continuas sin pausas."""

    def __init__(self, servidor: str, imagen: str, clientes: int) -> None:
        self.servidor = servidor
        self.imagen = imagen
        # Historial de resultados: (ts_envio, ts_respuesta, codigo_http, detalle_nodo_o_error)
        self.resultados: list[tuple[float, float, int, str]] = []
        self._detener = threading.Event()
        # Creación de hilos de trabajo independientes
        self._hilos = [threading.Thread(target=self._cliente, args=(i,), daemon=True) for i in range(clientes)]

    def _cliente(self, i: int) -> None:
        """Bucle de ejecución de cada hilo cliente: envía tareas aritméticas continuas."""
        while not self._detener.is_set():
            envio = time.time()
            try:
                # Enviar tarea aritmética simple a través de Nginx
                sobre = enviar_tarea(self.servidor, "suma", {"a": i, "b": 1}, {}, self.imagen, timeout=60)
                codigo = sobre["codigo"]
                # Si tuvo éxito extraemos el nodo que procesó la tarea; si falló, el tipo de error
                detalle = sobre["contenido"].get("nodo") if codigo == 200 else sobre["contenido"]["error"]["tipo"]
            except Exception as error:  # noqa: BLE001
                # Captura excepciones de red o timeout a nivel socket
                codigo, detalle = 0, type(error).__name__
            self.resultados.append((envio, time.time(), codigo, detalle))

    def iniciar(self) -> None:
        """Inicia todos los hilos de carga en paralelo."""
        for hilo in self._hilos:
            hilo.start()

    def detener(self) -> None:
        """Señala a los hilos que terminen y aguarda su finalización."""
        self._detener.set()
        for hilo in self._hilos:
            hilo.join()


# ==============================================================================
# 3. Observador de Alta Frecuencia (Vigía de Nginx)
# ==============================================================================
class Vigia:
    """Monitorea el endpoint `/health` de Nginx a intervalos de 10ms para detectar el nuevo líder.
    
    ¿Por qué es necesario un hilo independiente antes del kill?
    Si se empezara a consultar a Nginx únicamente después de ejecutar `docker kill`,
    la medición arrastraría el retardo propio del CLI de Docker (~150 ms), falseando
    la latencia de detección y elección percibida por un cliente externo.
    """

    def __init__(self, cluster: Cluster) -> None:
        self._cluster = cluster
        # Lista cronológica: (timestamp_lectura, id_coordinador_observado)
        self.vistas: list[tuple[float, int | None]] = []
        self._detener = threading.Event()
        self._hilo = threading.Thread(target=self._mirar, daemon=True)

    def _mirar(self) -> None:
        """Sondeo incesante del endpoint /health de Nginx."""
        while not self._detener.is_set():
            coordinador = self._cluster.cluster().get("coordinador")
            self.vistas.append((time.time(), coordinador))
            time.sleep(0.01)  # Intervalo de sondeo de 10 milisegundos

    def iniciar(self) -> None:
        """Arranca el hilo de sondeo continuo."""
        self._hilo.start()

    def detener(self) -> None:
        """Detiene el hilo de sondeo y espera su terminación."""
        self._detener.set()
        self._hilo.join()

    def primera(self, desde: float, coordinador: int) -> float | None:
        """Retorna el primer instante en que se observó al nuevo coordinador a partir de `desde`."""
        return next((t for t, c in self.vistas if t >= desde and c == coordinador), None)


# ==============================================================================
# 4. Ejecución de un Escenario de Prueba (Una Repetición)
# ==============================================================================
def _primero(lineas: list[tuple[float, int, str]], desde: float, patron: str, nodos: list[int] = NODOS) -> float | None:
    """Busca en los logs el timestamp del primer mensaje que coincide con el patrón regex."""
    expresion = re.compile(patron)
    return next((t for t, n, texto in lineas if t >= desde and n in nodos and expresion.search(texto)), None)


def repeticion(cluster: Cluster, args: argparse.Namespace) -> tuple[dict, dict]:
    """Ejecuta una ronda completa de inyección de falla, elección de líder y recuperación.
    
    Fases del Experimento:
    1. Identifica el coordinador actual (víctima) y predice el nuevo líder por Bully (el mayor ID vivo).
    2. Arranca carga concurrente y vigía de alta frecuencia.
    3. Mata el coordinador con SIGKILL (`docker kill`).
    4. Lee `State.FinishedAt` del daemon de Docker como instante cero ($t_{kill}$).
    5. Espera a que el vigía registre el nuevo líder a través de Nginx.
    6. Detiene la carga y reinicia el nodo caído para validar su reintegración.
    7. Analiza logs de todos los nodos para correlacionar eventos temporales.
    """
    # Paso 1: Determinar topología actual y predicción determinística del algoritmo Bully
    actual = cluster.cluster().get("coordinador")
    victima = actual
    sobrevivientes = [n for n in NODOS if n != victima]
    # En el algoritmo Bully, el nodo sobreviviente con mayor ID numérico debe ganar la elección
    nuevo = max(sobrevivientes)

    # Paso 2: Iniciar generador de carga y observador de salud en paralelo
    carga = Carga(cluster.servidor, args.imagen, args.clientes)
    vigia = Vigia(cluster)
    carga.iniciar()
    vigia.iniciar()
    time.sleep(args.antes)  # Período de calentamiento para alcanzar régimen estacionario

    # Paso 3: Inyección de falla instantánea mediante SIGKILL
    contenedor = cluster.contenedor(victima)
    subprocess.run(["docker", "kill", contenedor], check=True, capture_output=True)

    # Paso 4: Timestamp físico de muerte según el kernel de Docker (reloj del host compartido)
    t_kill = _epoch(subprocess.run(
        ["docker", "inspect", "-f", "{{.State.FinishedAt}}", contenedor],
        check=True,
        capture_output=True,
        text=True
    ).stdout.strip())

    # Paso 5: Esperar hasta que el nuevo coordinador esté operativo y visible por Nginx
    cluster.esperar(lambda: vigia.primera(t_kill, nuevo), 30, "el líder nuevo")
    time.sleep(args.despues)

    # Detener monitoreo y tráfico sintético
    carga.detener()
    vigia.detener()
    t_visto = vigia.primera(t_kill, nuevo)

    # Paso 6: Reincorporación del nodo caído: arranca nuevamente el contenedor
    t_start = time.time()
    subprocess.run(["docker", "start", contenedor], check=True, capture_output=True)
    # Al tener el mayor ID, el nodo debe iniciar una elección tras su arranque y recuperar el liderazgo
    cluster.esperar(lambda: cluster.estable(victima), 60, "la vuelta del nodo")

    # Paso 7: Extracción y análisis forense de logs de eventos
    lineas = cluster.logs(t_kill - 1)
    desde = t_kill - MARGEN

    # Detección: primer mensaje donde un nodo nota que el coordinador no responde
    t_deteccion = _primero(lineas, desde, r"no responde", sobrevivientes)
    # Inicio formal de elección Bully
    t_eleccion = _primero(lineas, desde, r"inicia elección", sobrevivientes)
    # Proclamación del nuevo coordinador
    t_lider = _primero(lineas, desde, rf"nodo {nuevo} es el nuevo COORDINADOR", [nuevo])
    # Aceptación de los demás pares
    t_aceptan = max(_primero(lineas, desde, rf"acepta COORDINADOR {nuevo}", [n]) or 0
                    for n in sobrevivientes if n != nuevo)
    # Reintegración y recuperación de liderazgo por parte del nodo resucitado
    t_vuelta = _primero(lineas, t_start, rf"nodo {victima} es el nuevo COORDINADOR", [victima])
    t_arranque = _primero(lineas, t_start, r"arranca el nodo", [victima])
    reasignadas = sum(1 for t, _, texto in lineas
                      if desde <= t < t_start and f"se cayó el nodo {victima}" in texto)

    # Clasificación de tareas concurrentes afectadas durante la ventana de transición
    afectadas = [r for r in carga.resultados if r[0] <= t_lider and r[1] >= desde]
    normales = [r[1] - r[0] for r in carga.resultados if r[1] < desde and r[2] == 200]

    # Función auxiliar para calcular milisegundos con redondeo seguro
    ms = lambda a, b: max(0, round((a - b) * 1000)) if a and b else None  # noqa: E731

    # Estructura de resultados de la repetición
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

    # Trazas de auditoría para archivo de log
    detalle = {
        "t_kill": t_kill,
        "eventos": {"deteccion": t_deteccion, "lider": t_lider, "visto": t_visto},
        "tareas": carga.resultados,
        "log": [(t, n, texto) for t, n, texto in lineas
                if desde <= t <= t_lider + 0.05 and ("bully" in texto or "cluster |" in texto)]
    }
    return fila, detalle


# ==============================================================================
# 5. Generación de Resúmenes Estadísticos y Gráficos
# ==============================================================================
def resumen(filas: list[dict]) -> dict:
    """Calcula media, mínimo, máximo y desvío estándar de las métricas clave."""
    claves = [
        "deteccion_ms", "eleccion_ms", "lider_nuevo_ms", "todos_aceptan_ms",
        "visto_por_nginx_ms", "vuelta_arranque_ms", "vuelta_lider_ms"
    ]
    salida = {}
    for clave in claves:
        valores = [f[clave] for f in filas if f[clave] is not None]
        if valores:
            salida[clave] = {
                "media": round(statistics.mean(valores)),
                "min": min(valores),
                "max": max(valores),
                "desvio": round(statistics.stdev(valores)) if len(valores) > 1 else 0
            }
    return salida


def graficar(filas: list[dict], detalle: dict, destino: Path) -> None:
    """Genera un gráfico comparativo de dos paneles con Matplotlib y lo guarda como imagen PNG."""
    figura, (linea, barras) = plt.subplots(
        1, 2, figsize=(13, 5), dpi=120, gridspec_kw={"width_ratios": [3, 2]}
    )
    t0 = detalle["t_kill"]

    # Panel 1: Diagrama de Gantt / Línea temporal de cada petición en vuelo
    for envio, respuesta, codigo, _ in detalle["tareas"]:
        color = "tab:green" if codigo == 200 else "tab:red"
        linea.plot([envio - t0, respuesta - t0], [respuesta - envio] * 2, color=color, alpha=0.6, lw=2)

    # Líneas verticales que marcan hitos críticos del sistema distribuido
    for nombre, t, color in [
        ("kill", t0, "k"),
        ("detección", detalle["eventos"]["deteccion"], "C1"),
        ("líder nuevo", detalle["eventos"]["lider"], "C0")
    ]:
        if t:
            linea.axvline(t - t0, color=color, ls="--", label=f"{nombre} ({(t - t0) * 1000:.0f} ms)")

    linea.set_title("Una caída: cada tarea, de su envío a su respuesta")
    linea.set_xlabel("segundos desde el kill del coordinador")
    linea.set_ylabel("latencia de la tarea (s)")
    linea.plot([], [], color="tab:green", lw=2, label="tarea OK")
    linea.plot([], [], color="tab:red", lw=2, label="tarea fallida")
    linea.legend(loc="upper left", fontsize=8)
    linea.grid(True, linestyle=":", alpha=0.6)

    # Panel 2: Barras apiladas mostrando el desglose temporal (detección vs elección) por repetición
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


# ==============================================================================
# 6. Punto de Entrada Principal (CLI)
# ==============================================================================
def main() -> None:
    """Configura parámetros por terminal, ejecuta repeticiones y escribe artefactos de salida."""
    parser = argparse.ArgumentParser(
        description="Medición del tiempo de recuperación ante la caída del coordinador (Hit 3)"
    )
    parser.add_argument("--servidor", default="http://127.0.0.1:8080", help="Dirección del reverse proxy Nginx")
    parser.add_argument("--env-file", help="Ruta al archivo .env del compose (por defecto: hit3/.env)")
    parser.add_argument("--imagen", default="cerberusdistribuido/tarea:1.0.0", help="Imagen Docker de la tarea")
    parser.add_argument("--repeticiones", type=int, default=5, help="Número de caídas consecutivas a evaluar")
    parser.add_argument(
        "--clientes",
        type=int,
        default=3,
        help="Cantidad de hilos clientes concurrentes (0: sin carga externa, sólo mide heartbeat)"
    )
    parser.add_argument("--nombre", default="recuperacion", help="Prefijo de los archivos de reporte generados")
    parser.add_argument("--antes", type=float, default=3.0, help="Segundos de carga previa al kill del líder")
    parser.add_argument("--despues", type=float, default=3.0, help="Segundos de carga posterior a la elección")
    args = parser.parse_args()

    # Inicialización del cluster y espera hasta que el cluster esté estable
    cluster = Cluster(args.servidor, args.env_file)
    cluster.esperar(lambda: cluster.estable(max(NODOS)), 60, "un cluster estable")

    # Calentamiento inicial: la primera petición fuerza la descarga (docker pull) si la imagen no existía
    enviar_tarea(cluster.servidor, "suma", {"a": 0, "b": 0}, {}, args.imagen, timeout=600)

    # Ciclo de ejecuciones experimentales
    filas, detalles = [], []
    for i in range(1, args.repeticiones + 1):
        fila, detalle = repeticion(cluster, args)
        filas.append(fila)
        detalles.append(detalle)
        print(f"Repetición {i}: detección {fila['deteccion_ms']} ms · elección {fila['eleccion_ms']} ms · "
              f"líder nuevo {fila['lider_nuevo_ms']} ms · visto por nginx {fila['visto_por_nginx_ms']} ms · "
              f"tareas afectadas {fila['tareas_afectadas']} (fallidas: {len(fila['afectadas_fallidas'])}, "
              f"reasignadas: {fila['reasignadas']})")

    # Persistencia de artefactos en directorio mediciones/
    salida = HIT3 / "mediciones"
    salida.mkdir(exist_ok=True)
    datos = {
        "repeticiones": args.repeticiones,
        "clientes": args.clientes,
        "imagen": args.imagen,
        "intervalo_heartbeat_s": 1.0,
        "resumen": resumen(filas),
        "resultados": filas
    }
    (salida / f"{args.nombre}.json").write_text(
        json.dumps(datos, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8"
    )

    # Registro en log de la primera elección para ilustrar diagramas de secuencia en el informe técnico
    t0 = detalles[0]["t_kill"]
    with open(salida / f"{args.nombre}_eleccion.log", "w", encoding="utf-8") as archivo:
        archivo.write(f"# kill del nodo {filas[0]['coordinador_caido']} en t = 0 ms\n")
        for t, nodo, texto in detalles[0]["log"]:
            archivo.write(f"{(t - t0) * 1000:+8.1f} ms  nodo{nodo}  {texto.split(' | ', 2)[-1]}\n")

    # Generación y guardado del gráfico comparativo
    graficar(filas, detalles[0], salida / f"{args.nombre}.png")
    print(json.dumps(datos["resumen"], indent=2))
    print(f"Resultados guardados exitosamente en {salida}")


if __name__ == "__main__":
    main()

