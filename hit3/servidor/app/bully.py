"""Elección de líder con el algoritmo Bully [GAR82].

Gana el nodo vivo de ID más alto. Los mensajes van por HTTP entre los nodos:

- `ELECCION` (POST /cluster/eleccion): a los de ID mayor. "¿Hay alguien más
  grande vivo?". El que la recibe contesta `OK` (un 200) y arranca su propia
  elección.
- `COORDINADOR` (POST /cluster/coordinador): el ganador se anuncia a todos. Si
  viene de uno de ID menor, se rechaza (409) y se arranca una elección: el más
  grande se queda con el puesto (por eso "bully").
- Heartbeat (POST /cluster/heartbeat): cada nodo le avisa al coordinador que
  está vivo. Si el coordinador no contesta, o contesta que no lo es, se arranca
  una elección. Así se detecta la caída.

Esta clase es sólo el algoritmo: los endpoints de main.py le pasan los mensajes
que llegan, y ella manda los suyos con `http` (inyectable en los tests).
"""

import threading
from concurrent.futures import ThreadPoolExecutor

from app.lanzador import SinConexion, Vencido, http_json


def _en_un_hilo(funcion):
    threading.Thread(target=funcion, daemon=True).start()


class Bully:
    def __init__(self, config, logger, http=http_json, en_segundo_plano=_en_un_hilo, al_asumir=None):
        self.mi_id = config.nodo_id
        self._pares = dict(config.pares)
        self._config = config
        self._log = logger
        self._http = http
        self._en_segundo_plano = en_segundo_plano
        self._al_asumir = al_asumir or (lambda: None)
        self._cond = threading.Condition()
        self._coordinador = None
        self._en_eleccion = False
        # Cuántos COORDINADOR se recibieron: para saber si llegó uno mientras se esperaba.
        self._anuncios = 0

    @property
    def coordinador(self):
        with self._cond:
            return self._coordinador

    def soy_coordinador(self):
        return self.coordinador == self.mi_id

    def url(self, nodo):
        return self._pares[nodo]

    def esperar_coordinador(self, timeout):
        """El ID del coordinador; si hay una elección en curso, espera a que termine."""
        with self._cond:
            self._cond.wait_for(lambda: self._coordinador is not None and not self._en_eleccion, timeout)
            return self._coordinador

    # ------------------------------------------------------------- la elección
    def iniciar_eleccion(self, motivo):
        with self._cond:
            if self._en_eleccion:
                return
            self._en_eleccion = True
            self._coordinador = None
            anuncios_antes = self._anuncios
        reintentar = False
        try:
            mayores = {n: u for n, u in self._pares.items() if n > self.mi_id}
            self._log.info("bully | nodo %d inicia elección (%s) | ELECCION → %s",
                           self.mi_id, motivo, sorted(mayores) or "nadie")
            respondieron = self._mandar_a_todos(mayores, "/cluster/eleccion")
            if not respondieron:
                self._proclamarse()
                return
            self._log.info("bully | nodo %d recibió OK de %s: espera COORDINADOR", self.mi_id, respondieron)
            with self._cond:
                llego = self._cond.wait_for(lambda: self._anuncios > anuncios_antes,
                                            self._config.timeout_coordinador)
            if not llego:
                self._log.warning("bully | nodo %d: no llegó COORDINADOR en %g s", self.mi_id,
                                  self._config.timeout_coordinador)
                reintentar = True
        finally:
            with self._cond:
                self._en_eleccion = False
                self._cond.notify_all()
        if reintentar:
            self.iniciar_eleccion("no llegó COORDINADOR")

    def _proclamarse(self):
        with self._cond:
            self._coordinador = self.mi_id
            self._anuncios += 1
            self._cond.notify_all()
        self._log.info("bully | nodo %d es el nuevo COORDINADOR | COORDINADOR → %s", self.mi_id,
                       sorted(self._pares) or "nadie")
        self._al_asumir()
        self._mandar_a_todos(self._pares, "/cluster/coordinador")

    def _mandar_a_todos(self, nodos, ruta):
        """Manda el mensaje a todos a la vez; devuelve los IDs que contestaron 200.

        En paralelo: con uno caído, esperar su timeout antes de hablarle al
        siguiente alargaría la elección."""
        if not nodos:
            return []
        cuerpo = {"de": self.mi_id}

        def mandar(item):
            nodo, url = item
            try:
                estado, _ = self._http("POST", url + ruta, cuerpo, timeout=self._config.timeout_mensaje)
                return nodo if estado == 200 else None
            except (SinConexion, Vencido):
                return None

        with ThreadPoolExecutor(max_workers=len(nodos)) as hilos:
            return sorted(n for n in hilos.map(mandar, nodos.items()) if n is not None)

    # ------------------------------------------------------ mensajes recibidos
    def recibir_eleccion(self, de):
        """ELECCION de un nodo menor: se le contesta OK (el llamador responde 200) y se
        arranca una elección propia, en segundo plano para no demorar el OK."""
        self._log.info("bully | nodo %d recibió ELECCION de %d | OK → %d", self.mi_id, de, de)
        self._en_segundo_plano(lambda: self.iniciar_eleccion(f"ELECCION de {de}"))

    def recibir_coordinador(self, de):
        """COORDINADOR de `de`. Devuelve si se lo acepta."""
        if de < self.mi_id:
            self._log.info("bully | nodo %d rechaza COORDINADOR de %d (menor)", self.mi_id, de)
            self._en_segundo_plano(lambda: self.iniciar_eleccion(f"COORDINADOR de {de}, que es menor"))
            return False
        with self._cond:
            self._coordinador = de
            self._anuncios += 1
            self._cond.notify_all()
        self._log.info("bully | nodo %d acepta COORDINADOR %d", self.mi_id, de)
        return True

    # --------------------------------------------------------------- heartbeat
    def latir(self, tareas_en_curso):
        """Un heartbeat al coordinador. Si no contesta, o dice que no lo es, elección."""
        coordinador = self.coordinador
        if coordinador == self.mi_id:
            return
        if coordinador is None:
            self.iniciar_eleccion("no hay coordinador")
            return
        try:
            estado, _ = self._http("POST", self.url(coordinador) + "/cluster/heartbeat",
                                   {"de": self.mi_id, "tareas_en_curso": tareas_en_curso},
                                   timeout=self._config.timeout_mensaje)
        except (SinConexion, Vencido) as error:
            self._log.warning("bully | nodo %d: el coordinador %d no responde (%s)", self.mi_id,
                              coordinador, error)
            self.iniciar_eleccion(f"el coordinador {coordinador} no responde")
            return
        if estado != 200:
            self.iniciar_eleccion(f"el nodo {coordinador} dice que no es coordinador")
