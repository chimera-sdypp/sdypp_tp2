"""Un nodo del cluster: junta la elección (Bully), el registro de nodos y el lanzador.

Cada nodo cumple tres papeles a la vez:

- **Entrada**: nginx le pasa un pedido; le pide al coordinador a qué nodo
  mandarlo, se lo reenvía y le contesta al cliente. Es el dueño de la tarea
  hasta que termina: si el ejecutor se cae, pide otra asignación.
- **Worker**: ejecuta las tareas que le asignan (el `ejecutarTareaRemota()` del Hit 1).
- **Coordinador**, si ganó la elección: asigna tareas y lleva el registro de nodos.
"""

import threading
import time

from app.bully import Bully
from app.cluster import RegistroNodos
from app.errores import ErrorApi, TipoError
from app.lanzador import SinConexion, Vencido, http_json


class _EjecutorCaido(Exception):
    """Se cortó la conexión con el nodo que ejecutaba la tarea."""


class Nodo:
    def __init__(self, config, logger, lanzador, http=http_json, bully=None, registro=None,
                 reloj=time.monotonic, dormir=time.sleep):
        self.mi_id = config.nodo_id
        self._config = config
        self._log = logger
        self._lanzador = lanzador
        self._http = http
        self._reloj = reloj
        self._dormir = dormir
        self.registro = registro or RegistroNodos(config.vencimiento_nodo)
        self.bully = bully or Bully(config, logger, http=http, al_asumir=self._al_asumir)
        self._lock = threading.Lock()
        self._en_curso = 0
        self._detener = threading.Event()

    @property
    def tareas_en_curso(self):
        with self._lock:
            return self._en_curso

    # ---------------------------------------------------------------- ciclo de vida
    def iniciar(self):
        """Al arrancar: limpia huérfanos y lanza el hilo de elección + heartbeats."""
        self._lanzador.limpiar_huerfanos()
        threading.Thread(target=self._bucle, name="heartbeat", daemon=True).start()

    def detener(self):
        self._detener.set()

    def _bucle(self):
        # Todo nodo que arranca convoca una elección: si es el mayor, se queda con el puesto.
        self.bully.iniciar_eleccion("arranque")
        while not self._detener.wait(self._config.intervalo_heartbeat):
            try:
                self.latir()
            except Exception:  # el hilo no puede morir: sin él no se detectan caídas
                self._log.exception("cluster | error en el heartbeat")

    def latir(self):
        if self.bully.soy_coordinador():
            self.registro.actualizar(self.mi_id, self.tareas_en_curso)
        else:
            self.bully.latir(self.tareas_en_curso)

    def _al_asumir(self):
        self.registro.reiniciar()
        self.registro.actualizar(self.mi_id, self.tareas_en_curso)

    # ---------------------------------------------------------------- coordinador
    def recibir_heartbeat(self, de, tareas_en_curso):
        if not self.bully.soy_coordinador():
            return False
        self.registro.actualizar(de, tareas_en_curso)
        return True

    def asignar(self, excluir=()):
        """El nodo que va a ejecutar la próxima tarea; None si no hay ninguno vivo."""
        nodo = self.registro.elegir(excluir=set(excluir))
        self._log.info("cluster | coordinador %d asigna la tarea al nodo %s (excluidos: %s)",
                       self.mi_id, nodo, sorted(excluir) or "ninguno")
        return nodo

    # ---------------------------------------------------------------- worker
    def ejecutar_local(self, imagen, calculo, parametros, datos):
        with self._lock:
            self._en_curso += 1
        try:
            return self._lanzador.ejecutarTareaRemota(imagen, calculo, parametros, datos)
        finally:
            with self._lock:
                self._en_curso -= 1

    # ---------------------------------------------------------------- entrada
    def ejecutar(self, imagen, calculo, parametros, datos):
        """Corre la tarea en el nodo que asigne el coordinador → (resultado, nodo).

        Si el ejecutor se cae a mitad de camino, se pide otra asignación sin él:
        así se redistribuyen las tareas de un nodo caído. Se puede re-ejecutar
        porque las tareas son cálculos sin efectos secundarios."""
        excluidos = set()
        while True:
            nodo = self._pedir_asignacion(excluidos)
            if nodo == self.mi_id:
                return self.ejecutar_local(imagen, calculo, parametros, datos), nodo
            try:
                return self._ejecutar_en(nodo, imagen, calculo, parametros, datos), nodo
            except _EjecutorCaido as error:
                self._log.warning("cluster | se cayó el nodo %d con la tarea en curso (%s): se reasigna",
                                  nodo, error)
                excluidos.add(nodo)

    def _pedir_asignacion(self, excluidos):
        """Le pide un nodo al coordinador. Si el coordinador no contesta, convoca
        una elección y reintenta con el nuevo, hasta `timeout_asignacion`."""
        limite = self._reloj() + self._config.timeout_asignacion
        while (restante := limite - self._reloj()) > 0:
            coordinador = self.bully.esperar_coordinador(timeout=restante)
            if coordinador is None:
                break
            if coordinador == self.mi_id:
                nodo = self.asignar(excluidos)
                if nodo is None:
                    break
                return nodo
            try:
                estado, respuesta = self._http("POST", self.bully.url(coordinador) + "/cluster/asignar",
                                               {"de": self.mi_id, "excluir": sorted(excluidos)},
                                               timeout=self._config.timeout_mensaje)
            except (SinConexion, Vencido) as error:
                self._log.warning("cluster | el coordinador %d no responde al pedir asignación (%s)",
                                  coordinador, error)
                self.bully.iniciar_eleccion(f"el coordinador {coordinador} no asigna")
                continue
            contenido = respuesta.get("contenido") if isinstance(respuesta, dict) else None
            if estado == 200 and isinstance(contenido, dict) and isinstance(contenido.get("nodo"), int):
                return contenido["nodo"]
            if estado == 503:
                break  # hay coordinador, pero ningún nodo vivo para la tarea
            # 409: ya no es coordinador (hubo otra elección). Se espera un poco y se reintenta.
            self._dormir(0.1)
        raise ErrorApi(TipoError.CLUSTER_NO_DISPONIBLE,
                       "no hay coordinador ni nodos disponibles para ejecutar la tarea")

    def _ejecutar_en(self, nodo, imagen, calculo, parametros, datos):
        cuerpo = {"calculo": calculo, "parametros": parametros, "datos": datos, "imagen": imagen.referencia}
        # El ejecutor puede tener que bajar la imagen y esperar al contenedor: se le da
        # el tiempo de la tarea más un margen para el pull.
        timeout = self._config.timeout_arranque + self._config.timeout_ejecucion + 60
        try:
            estado, respuesta = self._http("POST", self.bully.url(nodo) + "/cluster/ejecutar", cuerpo,
                                           timeout=timeout)
        except SinConexion as error:
            raise _EjecutorCaido(str(error)) from None
        except Vencido:
            raise ErrorApi(TipoError.TAREA_SIN_RESPUESTA, "la tarea no respondió a tiempo",
                           interno=f"el nodo {nodo} no contestó en {timeout:g} s") from None

        # El ejecutor contesta con el sobre de siempre: el error se devuelve tal cual.
        contenido = respuesta.get("contenido") if isinstance(respuesta, dict) else None
        if estado == 200 and isinstance(contenido, dict) and "resultado" in contenido:
            return contenido["resultado"]
        error = contenido.get("error") if isinstance(contenido, dict) else None
        if isinstance(error, dict) and error.get("tipo") in TipoError.__members__:
            raise ErrorApi(TipoError[error["tipo"]], error.get("mensaje") or "", error.get("detalles"),
                           interno=f"desde el nodo {nodo}")
        raise ErrorApi(TipoError.ERROR_INTERNO, "error interno del servidor",
                       interno=f"el nodo {nodo} contestó HTTP {estado}: {str(respuesta)[:300]}")
