"""Elección de Líder con Algoritmo Bully (García-Molina 1982) & Tolerancia a Fallos.

Fundamento Teórico del Algoritmo del Matón (Bully Algorithm):
Propuesto por Hector García-Molina en 1982 ("Elections in a Distributed Computing System", IEEE Transactions on Computers).
En un cluster distribuido sin reloj físico global unificado, el algoritmo Bully resuelve el problema de
consenso para elección de líder bajo el modelo de fallas por parada (crash-stop):

1. Supuestos del Sistema:
   - Cada nodo posee un identificador numérico entero único y totalmente ordenado (1 < 2 < 3...).
   - Todo nodo conoce las direcciones de red de todos los demás pares del cluster.
   - Los mensajes llegan sin corrupción y las caídas de nodos se detectan mediante timeouts acotados.

2. Tipos de Mensajes del Protocolo:
   - `ELECCION` (POST /cluster/eleccion): Enviado por un nodo que sospecha una caída a todos los nodos con ID mayor.
     Pregunta: "¿Hay algún nodo superior vivo?".
   - `OK` (HTTP 200 en respuesta a ELECCION): Respuesta inmediata del receptor de mayor jerarquía indicando:
     "Estoy vivo, yo me hago cargo de la elección".
   - `COORDINADOR` (POST /cluster/coordinador): Mensaje de victoria emitido por el nodo de mayor ID a todos los pares
     del cluster, anunciando: "Yo soy el nuevo líder legítimo".
   - `HEARTBEAT` (POST /cluster/heartbeat): Latidos periódicos enviados por los workers al coordinador para
     monitorear su estado de salud (failure detector).

3. Regla Fundamental de Jerarquía:
   - El nodo vivo con el ID más alto siempre gana la elección ("el más fuerte se impone", de allí el nombre Bully).
   - Si un nodo recibe un mensaje `COORDINADOR` de un ID menor al suyo, lo rechaza con HTTP 409 y convoca de inmediato
     a una nueva elección para restaurar la jerarquía legítima.
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
        """Inicializa el módulo de elección Bully con inyección de dependencias para tests determinísticos."""
        self.mi_id = config.nodo_id
        self._pares = dict(config.pares)
        self._config = config
        self._log = logger
        self._http = http
        self._en_segundo_plano = en_segundo_plano
        self._al_asumir = al_asumir or (lambda: None)
        # Callback invocado con los nodos que aceptaron la victoria (se sabe que están vivos)
        self._al_anunciarse = al_anunciarse or (lambda aceptaron: None)
        
        # Variable de condición para sincronización entre hilos del nodo
        self._cond = threading.Condition()
        self._coordinador = None
        self._en_eleccion = False
        self._anuncios = 0

    @property
    def coordinador(self):
        """Retorna el ID del coordinador actual bajo exclusión mutua."""
        with self._cond:
            return self._coordinador

    def soy_coordinador(self) -> bool:
        """Indica si este nodo es el coordinador activo del cluster."""
        return self.coordinador == self.mi_id

    def url(self, nodo: int) -> str:
        """URL base de red interna para comunicarse con el nodo par especificado."""
        return self._pares[nodo]

    def esperar_coordinador(self, timeout: float):
        """Bloquea el hilo llamante hasta que finalice la elección y se establezca un líder.
        
        Retorna el ID del coordinador elegido o None si se agotó el timeout.
        """
        with self._cond:
            self._cond.wait_for(lambda: self._coordinador is not None and not self._en_eleccion, timeout)
            return self._coordinador

    # --------------------------------------------------------------------------
    # 1. Proceso de Elección Activa (García-Molina)
    # --------------------------------------------------------------------------
    def iniciar_eleccion(self, motivo: str, si_sigue=None):
        """Dispara una ronda de elección de Bully.
        
        Secuencia de Pasos:
        1. Comprueba si ya hay una elección en curso o si la sospecha de caída ya fue resuelta
           (parámetro `si_sigue`), evitando tormentas de elecciones espurias.
        2. Identifica a todos los nodos del cluster con ID estrictamente superior al propio.
        3. Envía en paralelo el mensaje ELECCION a todos los nodos mayores.
        4. Si NINGÚN nodo mayor responde, este nodo se proclama GANADOR y emite COORDINADOR.
        5. Si al menos un nodo mayor responde OK, este nodo cede el liderazgo y espera
           la llegada del mensaje COORDINADOR durante `timeout_coordinador` segundos.
        6. Si el timeout vence sin recibir la proclamación del nuevo líder, reintenta la elección.
        """
        with self._cond:
            # Si ya se está ejecutando una elección en este nodo, ignoramos la solicitud duplicada
            if self._en_eleccion:
                return
            # Si se especificó `si_sigue` y el coordinador cambió mientras tanto, abortamos
            if si_sigue is not None and self._coordinador != si_sigue:
                return
            self._en_eleccion = True
            self._coordinador = None
            anuncios_antes = self._anuncios
            
        reintentar = False
        try:
            # Paso 1: Filtrar los nodos pares que tienen un ID mayor
            mayores = {n: u for n, u in self._pares.items() if n > self.mi_id}
            self._log.info(
                "bully | nodo %d inicia elección (%s) | ELECCION → %s",
                self.mi_id, motivo, sorted(mayores) or "nadie"
            )
            
            # Paso 2: Emitir mensaje ELECCION en paralelo a los nodos superiores
            respondieron = self._mandar_a_todos(mayores, "/cluster/eleccion")
            
            # Paso 3: Proclamación si no hay superiores activos
            if not respondieron:
                self._proclamarse()
                return
                
            # Paso 4: Esperar a que el nodo superior proclamado envíe COORDINADOR
            self._log.info("bully | nodo %d recibió OK de %s: espera COORDINADOR", self.mi_id, respondieron)
            with self._cond:
                llego = self._cond.wait_for(
                    lambda: self._anuncios > anuncios_antes,
                    self._config.timeout_coordinador
                )
            # Paso 5: Detección de caída del candidato superior durante la elección
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
                
        # Reintento fuera del bloque de bloqueo para evitar deadlocks
        if reintentar:
            self.iniciar_eleccion("no llegó COORDINADOR de los nodos mayores")

    # --------------------------------------------------------------------------
    # 2. Proclamación de Victoria y Broadcast
    # --------------------------------------------------------------------------
    def _proclamarse(self):
        """Asume el rol de coordinador legítimo y notifica por broadcast a todos los pares."""
        with self._cond:
            # Invocamos el callback de asunción (ej. iniciar monitoreo de workers)
            self._al_asumir()
            self._coordinador = self.mi_id
            self._anuncios += 1
            self._cond.notify_all()
            
        self._log.info(
            "bully | nodo %d es el nuevo COORDINADOR | COORDINADOR → %s",
            self.mi_id, sorted(self._pares) or "nadie"
        )
        # Notificamos a todos los nodos pares mediante broadcast HTTP
        self._al_anunciarse(self._mandar_a_todos(self._pares, "/cluster/coordinador"))

    def _mandar_a_todos(self, nodos: dict, ruta: str) -> list[int]:
        """Envía un mensaje HTTP en paralelo a un conjunto de nodos y retorna los que respondieron 200.
        
        Utiliza un ThreadPoolExecutor con tantos workers como nodos destino para evitar
        que la falta de respuesta de un nodo caído bloquee secuencialmente la elección.
        """
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

        # Despacho paralelo de peticiones HTTP
        with ThreadPoolExecutor(max_workers=len(nodos)) as hilos:
            return sorted(n for n in hilos.map(mandar, nodos.items()) if n is not None)

    # --------------------------------------------------------------------------
    # 3. Manejo de Mensajes Recibidos de Pares
    # --------------------------------------------------------------------------
    def recibir_eleccion(self, de: int):
        """Maneja la recepción de un mensaje ELECCION proveniente de un nodo con ID menor.
        
        Regla de Bully: El nodo con ID mayor responde OK (HTTP 200 retornado por FastAPI)
        e inmediatamente arranca su propia elección en un hilo independiente para reclamar
        el liderazgo.
        """
        self._log.info("bully | nodo %d recibió ELECCION de %d | OK → %d", self.mi_id, de, de)
        self._en_segundo_plano(lambda: self.iniciar_eleccion(f"ELECCION de {de}"))

    def recibir_coordinador(self, de: int) -> bool:
        """Maneja la recepción de un mensaje COORDINADOR anunciando un nuevo líder.
        
        Comprobación Jerárquica:
        - Si el emisor tiene un ID menor al propio, el mensaje es ilegítimo: se rechaza
          (retornando False para responder HTTP 409) y se inicia una nueva elección.
        - Si el emisor tiene un ID mayor o igual, se acepta como legítimo líder del cluster.
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
        """Envía un latido periódico al coordinador activo para verificar su disponibilidad.
        
        Si el coordinador no responde (timeout o socket caído) o devuelve un código HTTP != 200,
        se infiere que colapsó y se convoca de inmediato a una nueva elección de Bully.
        """
        coordinador = self.coordinador
        if coordinador == self.mi_id:
            return  # El coordinador no necesita enviarse heartbeats a sí mismo
        if coordinador is None:
            self.iniciar_eleccion("no hay coordinador activo conocido")
            return
            
        try:
            # Enviamos el latido con el conteo de tareas locales en curso
            estado, _ = self._http(
                "POST",
                self.url(coordinador) + "/cluster/heartbeat",
                {"de": self.mi_id, "tareas_en_curso": tareas_en_curso},
                timeout=self._config.timeout_mensaje
            )
        except (SinConexion, Vencido) as error:
            # Detección de falla: el coordinador no respondió dentro del tiempo límite
            self._log.warning(
                "bully | nodo %d: el coordinador %d no responde (%s)",
                self.mi_id, coordinador, error
            )
            self.iniciar_eleccion(f"el coordinador {coordinador} no responde", si_sigue=coordinador)
            return
            
        # Si el nodo respondió con un código distinto de 200 (ej. 404 o 500)
        if estado != 200:
            self.iniciar_eleccion(f"el nodo {coordinador} dice que no es coordinador", si_sigue=coordinador)

