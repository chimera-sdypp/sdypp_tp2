"""Elección de Líder con Algoritmo Bully (García-Molina 1982) & Tolerancia a Fallos.

Principios del Protocolo Distribuido:
1. CONSENSO POR JERARQUÍA: El nodo activo con el identificador numérico más alto
   se proclama legítimo coordinador del cluster.
2. MENSAJES ENTRE PARES (Red Privada Virtual):
   - `ELECCION` (POST /cluster/eleccion): Enviado a los nodos con ID mayor ("¿Hay alguien mayor vivo?").
     El receptor responde `OK` (HTTP 200) y toma la posta iniciando su propia elección.
   - `COORDINADOR` (POST /cluster/coordinador): El ganador se proclama líder mediante broadcast a todos.
     Si un nodo recibe un COORDINADOR de un ID menor al suyo, lo rechaza (409) y convoca nueva elección.
   - `HEARTBEAT` (POST /cluster/heartbeat): Los workers envían periódicamente su latido al líder.
     Si el líder no responde o rechaza la conexión, se detecta la caída y se dispara la re-elección.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

from app.lanzador import SinConexion, Vencido, http_json


def _en_un_hilo(funcion):
    """Ejecuta una función en un hilo demonio asincrónico para no bloquear el transporte HTTP."""
    threading.Thread(target=funcion, daemon=True).start()


class Bully:
    """Implementación hilo-segura del algoritmo de elección Bully."""

    def __init__(self, config, logger, http=http_json, en_segundo_plano=_en_un_hilo, al_asumir=None,
                 al_anunciarse=None):
        self.mi_id = config.nodo_id
        self._pares = dict(config.pares)
        self._config = config
        self._log = logger
        self._http = http
        self._en_segundo_plano = en_segundo_plano
        self._al_asumir = al_asumir or (lambda: None)
        # Callback invocado con los nodos que aceptaron la victoria (se sabe que están vivos)
        self._al_anunciarse = al_anunciarse or (lambda aceptaron: None)
        
        # Variables de estado y condición de sincronización
        self._cond = threading.Condition()
        self._coordinador = None
        self._en_eleccion = False
        self._anuncios = 0

    @property
    def coordinador(self):
        """Retorna el ID del coordinador actual bajo lock."""
        with self._cond:
            return self._coordinador

    def soy_coordinador(self) -> bool:
        """Indica si este nodo es el coordinador activo."""
        return self.coordinador == self.mi_id

    def url(self, nodo: int) -> str:
        """URL base de red interna para hablarle al nodo especificado."""
        return self._pares[nodo]

    def esperar_coordinador(self, timeout: float):
        """Espera a que finalice cualquier elección en curso y retorna el líder."""
        with self._cond:
            self._cond.wait_for(lambda: self._coordinador is not None and not self._en_eleccion, timeout)
            return self._coordinador

    # --------------------------------------------------------------------------
    # 1. Proceso de Elección Activa (García-Molina)
    # --------------------------------------------------------------------------
    def iniciar_eleccion(self, motivo: str, si_sigue=None):
        """Dispara una ronda de elección de Bully.
        
        Parámetro `si_sigue`: Valida que la sospecha de caída siga siendo válida
        antes de iniciar el proceso, evitando tormentas de elecciones espurias.
        """
        with self._cond:
            if self._en_eleccion:
                return
            if si_sigue is not None and self._coordinador != si_sigue:
                return
            self._en_eleccion = True
            self._coordinador = None
            anuncios_antes = self._anuncios
            
        reintentar = False
        try:
            # 1. Identificar nodos con ID estrictamente superior
            mayores = {n: u for n, u in self._pares.items() if n > self.mi_id}
            self._log.info(
                "bully | nodo %d inicia elección (%s) | ELECCION → %s",
                self.mi_id, motivo, sorted(mayores) or "nadie"
            )
            
            # 2. Enviar mensaje ELECCION en paralelo a todos los mayores
            respondieron = self._mandar_a_todos(mayores, "/cluster/eleccion")
            
            # 3. Si ningún nodo mayor responde, este nodo se proclama GANADOR
            if not respondieron:
                self._proclamarse()
                return
                
            # 4. Si respondieron, se cede el liderazgo y se espera el mensaje COORDINADOR
            self._log.info("bully | nodo %d recibió OK de %s: espera COORDINADOR", self.mi_id, respondieron)
            with self._cond:
                llego = self._cond.wait_for(
                    lambda: self._anuncios > anuncios_antes,
                    self._config.timeout_coordinador
                )
            if not llego:
                self._log.warning(
                    "bully | nodo %d: no llegó COORDINADOR en %g s",
                    self.mi_id, self._config.timeout_coordinador
                )
                reintentar = True
        finally:
            with self._cond:
                self._en_eleccion = False
                self._cond.notify_all()
                
        if reintentar:
            self.iniciar_eleccion("no llegó COORDINADOR de los nodos mayores")

    # --------------------------------------------------------------------------
    # 2. Proclamación de Victoria y Broadcast
    # --------------------------------------------------------------------------
    def _proclamarse(self):
        """Asume el rol de coordinador y notifica a todos los nodos del cluster."""
        with self._cond:
            self._al_asumir()
            self._coordinador = self.mi_id
            self._anuncios += 1
            self._cond.notify_all()
            
        self._log.info(
            "bully | nodo %d es el nuevo COORDINADOR | COORDINADOR → %s",
            self.mi_id, sorted(self._pares) or "nadie"
        )
        self._al_anunciarse(self._mandar_a_todos(self._pares, "/cluster/coordinador"))

    def _mandar_a_todos(self, nodos: dict, ruta: str) -> list[int]:
        """Envía un mensaje HTTP en paralelo a múltiples nodos y recopila los que respondieron 200."""
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

        # Envío concurrente para evitar que un nodo caído ralentice la elección
        with ThreadPoolExecutor(max_workers=len(nodos)) as hilos:
            return sorted(n for n in hilos.map(mandar, nodos.items()) if n is not None)

    # --------------------------------------------------------------------------
    # 3. Manejo de Mensajes Recibidos de Pares
    # --------------------------------------------------------------------------
    def recibir_eleccion(self, de: int):
        """Mensaje ELECCION recibido de un nodo con ID menor.
        
        Responde OK de inmediato y arranca su propia elección en background.
        """
        self._log.info("bully | nodo %d recibió ELECCION de %d | OK → %d", self.mi_id, de, de)
        self._en_segundo_plano(lambda: self.iniciar_eleccion(f"ELECCION de {de}"))

    def recibir_coordinador(self, de: int) -> bool:
        """Mensaje COORDINADOR recibido anunciando un nuevo líder.
        
        Si el emisor tiene un ID menor, se rechaza pacíficamente y se convoca elección.
        """
        if de < self.mi_id:
            self._log.info("bully | nodo %d rechaza COORDINADOR de %d (menor que mi ID)", self.mi_id, de)
            self._en_segundo_plano(lambda: self.iniciar_eleccion(f"COORDINADOR de {de}, que es menor"))
            return False
            
        with self._cond:
            self._coordinador = de
            self._anuncios += 1
            self._cond.notify_all()
            
        self._log.info("bully | nodo %d acepta COORDINADOR %d", self.mi_id, de)
        return True

    # --------------------------------------------------------------------------
    # 4. Latidos (Heartbeats) y Detección de Caídas
    # --------------------------------------------------------------------------
    def latir(self, tareas_en_curso: int):
        """Envía un latido periódico al coordinador. Si no responde, convoca a elección."""
        coordinador = self.coordinador
        if coordinador == self.mi_id:
            return  # El coordinador no se envía heartbeats a sí mismo
        if coordinador is None:
            self.iniciar_eleccion("no hay coordinador activo conocido")
            return
            
        try:
            estado, _ = self._http(
                "POST",
                self.url(coordinador) + "/cluster/heartbeat",
                {"de": self.mi_id, "tareas_en_curso": tareas_en_curso},
                timeout=self._config.timeout_mensaje
            )
        except (SinConexion, Vencido) as error:
            self._log.warning(
                "bully | nodo %d: el coordinador %d no responde (%s)",
                self.mi_id, coordinador, error
            )
            self.iniciar_eleccion(f"el coordinador {coordinador} no responde", si_sigue=coordinador)
            return
            
        if estado != 200:
            self.iniciar_eleccion(f"el nodo {coordinador} dice que no es coordinador", si_sigue=coordinador)
