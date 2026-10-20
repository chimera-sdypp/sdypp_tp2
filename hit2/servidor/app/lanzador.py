"""Módulo Lanzador: Orquestador del Ciclo de Vida de Tareas y Contención de Recursos (R5 y Video).

Este componente es el responsable directo de interactuar con el Daemon de Docker a través
del socket Unix (`/var/run/docker.sock`). Cumple un rol crítico de aislamiento y seguridad
en la arquitectura distribuida del sistema:

1. Aislamiento Físico y de Procesos:
   - Cada tarea remota solicitada por un cliente se ejecuta dentro de un contenedor Docker
     estrictamente efímero e independiente.
   - Si la tarea colapsa, consume recursos excesivos o intenta un ataque, queda confinada
     en el espacio de nombres (namespaces) y grupos de control (cgroups) de su propio contenedor.

2. Contención de Recursos mediante Linux cgroups (Requisito R5):
   - CPU Quota (`nano_cpus`): Se traduce el requerimiento de núcleos de CPU a nanosegundos
     por período de scheduling del CFS (Completely Fair Scheduler) de Linux. 1 núcleo = 1e9 nano_cpus.
   - Memoria Límite (`mem_limit`): Limita la memoria física máxima (por ejemplo '256m' para tareas
     estándar o '2g' para transcodificación de video), impidiendo que una tarea sature la RAM
     del host y provoque que el OOM-Killer termine matando al servidor principal.

3. Comunicación Segura y Volúmenes Compartidos:
   - Comunicación de control vía HTTP JSON en red privada Docker (`red_tareas`).
   - Transferencia de datos pesados (video) mediante volúmenes montados (`/trabajos`), evitando
     la sobrecarga de serialización HTTP sobre sockets de red.
"""

import json
import time
import urllib.error
import urllib.request
from typing import Any, NamedTuple

import docker
from docker.errors import APIError, DockerException, NotFound
from requests.exceptions import RequestException

from app.errores import ErrorApi, TipoError

# Tupla de excepciones que indican que no hay conectividad con el daemon de Docker:
# socket ausente, falta de permisos en el grupo docker, o daemon caído/apagado.
_SIN_DOCKER = (DockerException, RequestException, OSError)


class Ejecucion(NamedTuple):
    """Estructura de datos que encapsula el resultado de `ejecutarTareaRemota()`.
    
    Campos:
    - resultado: Valor retornado por el cálculo (número, lista, etc.).
    - contenedor: Hash abreviado de 12 caracteres hexadecimales del contenedor ejecutor.
    - imagen_id: SHA256 de la imagen local con la que se instanció el contenedor.
    - digest: SHA256 inmutable del repositorio remoto (RepoDigests), garantizando inmutabilidad.
    - ejecucion_s: Tiempo exacto transcurrido desde la creación del contenedor hasta la recepción.
    """
    resultado: Any
    contenedor: str     # id corto (12 caracteres) del contenedor que ejecutó la tarea
    imagen_id: str      # sha256 de la imagen local con que se creó (siempre existe)
    digest: Any         # sha256 del repositorio (RepoDigests); None si la imagen no vino de un registry
    ejecucion_s: float  # de crear el contenedor a tener la respuesta de la tarea


def _digest_ejecutado(imagen, local):
    """Obtiene el digest criptográfico inmutable de la imagen en el registry.
    
    Si una imagen fue construida localmente sin haber sido subida a un registry,
    retorna None. Si tiene múltiples digests de repositorio, selecciona el que coincida
    con el repositorio solicitado en la configuración.
    """
    # Desglosamos las entradas RepoDigests en (repositorio, separador '@', hash sha256)
    candidatos = [d.partition("@") for d in local.attrs.get("RepoDigests") or []]
    # Filtramos los que coincidan con el nombre configurado (omitiendo el prefijo por defecto docker.io/)
    propios = [digest for repo, _, digest in candidatos
               if repo.removeprefix("docker.io/") == imagen.nombre.removeprefix("docker.io/")]
    # Si encontramos coincidencias exactas las usamos; de lo contrario usamos cualquier digest disponible
    elegidos = propios or [digest for _, _, digest in candidatos]
    # Retornamos el valor mínimo ordenado lexicográficamente como convención determinística
    return min(elegidos) if elegidos else None


class SinConexion(Exception):
    """Excepción interna lanzada cuando no se puede establecer conexión TCP con el contenedor."""


class Vencido(Exception):
    """Excepción interna lanzada cuando el contenedor agota el tiempo límite de respuesta (timeout)."""


class EjecucionVencida(ErrorApi):
    """TAREA_SIN_RESPUESTA porque la tarea arrancó pero no terminó a tiempo.

    Para /getRemoteTask es lo mismo que no haber arrancado (504), pero el servicio de video
    necesita distinguirlo: solo este caso es TIEMPO_AGOTADO (contrato-video.md §3).
    """


def http_json(metodo, url, cuerpo=None, timeout=5.0):
    """Cliente HTTP síncrono ultra-liviano basado en la biblioteca estándar `urllib.request`.
    
    Parámetros:
    - metodo: Verbo HTTP ('GET', 'POST', etc.).
    - url: Dirección URL absoluta del endpoint destino.
    - cuerpo: Diccionario serializable a JSON o None.
    - timeout: Tiempo máximo en segundos para conectar y leer datos.
    
    Retorna:
    - Tupla `(codigo_http, datos_parseados)`. Si la respuesta no es JSON válido, retorna `(codigo, None)`.
    
    Excepciones:
    - `Vencido`: Si se produce un Timeout en la red o procesamiento.
    - `SinConexion`: Si el socket fue rechazado o se produjo un error a nivel de transporte.
    """
    # Serializamos el cuerpo a bytes UTF-8 si existe
    datos = None if cuerpo is None else json.dumps(cuerpo).encode("utf-8")
    # Construimos la solicitud HTTP indicando el encabezado de contenido application/json
    pedido = urllib.request.Request(url, data=datos, method=metodo,
                                    headers={"Content-Type": "application/json"})
    try:
        # Abrimos el socket y leemos la respuesta completa
        with urllib.request.urlopen(pedido, timeout=timeout) as respuesta:
            estado, crudo = respuesta.status, respuesta.read()
    except urllib.error.HTTPError as error:
        # Errores a nivel de protocolo HTTP (códigos 4xx o 5xx) aún contienen cuerpo legible
        estado, crudo = error.code, error.read()
    except TimeoutError as error:
        # Expiración directa de tiempo de espera
        raise Vencido(str(error)) from None
    except urllib.error.URLError as error:
        # Si la causa raíz del URLError es un TimeoutError lo clasificamos como Vencido
        if isinstance(error.reason, TimeoutError):
            raise Vencido(str(error.reason)) from None
        # En cualquier otro caso de URLError (host unreachable, refused) es SinConexion
        raise SinConexion(str(error.reason)) from None
    except OSError as error:
        # Fallas a nivel de socket del sistema operativo
        raise SinConexion(str(error)) from None
    
    try:
        # Intentamos decodificar el payload JSON de la respuesta
        return estado, json.loads(crudo) if crudo else None
    except ValueError:
        # Si el cuerpo no era un JSON sintácticamente válido devolvemos None
        return estado, None


def _sin_docker(error):
    """Generador estandarizado de excepciones cuando Docker no está disponible."""
    return ErrorApi(TipoError.SERVICIO_NO_DISPONIBLE,
                    "el servicio no puede ejecutar tareas en este momento",
                    interno=f"Docker: {error}")


class Lanzador:
    """Administrador del ciclo de vida y despacho de tareas hacia contenedores Docker.
    
    Maneja la conexión perezosa (lazy connection) al daemon de Docker, verificación
    de imágenes en repositorios locales y remotos, instanciación con cgroups acotados,
    monitoreo de healthcheck HTTP y eliminación destructiva garantizada post-ejecución.
    """
    def __init__(self, config, logger, fabrica_docker=docker.from_env, http=http_json,
                 reloj=time.monotonic, dormir=time.sleep):
        """Inicializa el lanzador con inyección de dependencias para testeo determinístico."""
        self._config = config
        self._log = logger
        self._fabrica = fabrica_docker
        self._http = http
        self._reloj = reloj
        self._dormir = dormir
        self._cliente = None
        self._ultimo_ping_t = None
        self._ultimo_ping_ok = False

    def _docker(self):
        """Inicializa la conexión con Docker de forma diferida (lazy evaluation).
        
        Permite que el servidor web principal inicie correctamente aun si el daemon
        de Docker se encuentra caído o inaccesible temporalmente.
        """
        if self._cliente is None:
            try:
                self._cliente = self._fabrica()
            except _SIN_DOCKER as error:
                raise _sin_docker(error) from None
        return self._cliente

    def disponible(self, ttl: float = 1.0):
        """Comprueba si el Daemon de Docker responde correctamente a comandos ping.
        
        Utiliza una caché con Time-To-Live (`ttl`) para evitar saturar el socket Unix
        `/var/run/docker.sock` cuando múltiples hilos concurrentes consultan `/health`.
        """
        ahora = self._reloj()
        # Si la consulta previa ocurrió hace menos tiempo que el TTL, reutilizamos el estado
        if self._ultimo_ping_t is not None and (ahora - self._ultimo_ping_t) < ttl:
            return self._ultimo_ping_ok
        try:
            # Enviamos un ping ligero a través del Docker SDK
            ok = bool(self._docker().ping())
            self._ultimo_ping_t = ahora
            self._ultimo_ping_ok = ok
            return ok
        except (ErrorApi, *_SIN_DOCKER):
            # Si el ping falla registramos el fallo y devolvemos False
            self._ultimo_ping_t = ahora
            self._ultimo_ping_ok = False
            return False

    def preparar_imagen(self, imagen):
        """Verifica la existencia de la imagen antes de intentar ejecutarla.
        
        Si la imagen no reside en el almacén local del host, intenta descargarla
        automáticamente del registry oficial configurado. Retorna el conjunto
        de digests criptográficos asociados a la misma.
        """
        local = self._asegurar_imagen(self._docker(), imagen)
        return {d.partition("@")[2] for d in local.attrs.get("RepoDigests") or []}

    def ejecutarTareaRemota(self, imagen, calculo, parametros, datos, volumenes=None, mem_limit=None, cpus=None, timeout=None):  # noqa: N802 (nombre del enunciado)
        """Ejecuta una tarea remota instanciando un contenedor efímero aislado (Requisito R5 y Video).
        
        Principios de Aislamiento y Contención de Recursos:
        1. LÍMITES DE HARDWARE ESTRICTOS (cgroups):
           - `nano_cpus`: Convierte el valor en cores a nanosegundos de CPU para el kernel Linux.
             Si `tarea_cpus=1.0`, se fija `nano_cpus=1_000_000_000`. Esto previene que una tarea
             intensiva en cómputo monopolice los núcleos del procesador físico del host.
           - `mem_limit`: Acota la memoria física máxima (ej. '256m' o '2g' en videos) evitando que
             una tarea dispare el OOM-Killer sobre el servidor principal.
        2. VOLÚMENES COMPARTIDOS:
           - Permite mapear volúmenes del host o Docker (ej. volumen 'tp2-videos' montado en '/trabajos')
             para intercambio eficiente de streams de video sin transportarlos por sockets HTTP.
        3. CREACIÓN ATÓMICA Y LIMPIEZA GARANTIZADA:
           - Se ejecuta `containers.create()` seguido de `start()`: si la inicialización falla,
             tenemos la referencia al objeto para destruirlo en el bloque `finally:`.
           - El bloque `finally:` asegura la destrucción inmediata del contenedor (`remove(force=True)`),
             impidiendo la acumulación de contenedores zombie y fugas de descriptores en Docker.
        """
        cliente = self._docker()
        # Aseguramos que la imagen exista localmente (o la descargamos del registry autorizado)
        local = self._asegurar_imagen(cliente, imagen)
        contenedor = None
        try:
            inicio = time.perf_counter()
            # Creamos y arrancamos por separado: `containers.create()` + `contenedor.start()`.
            # Esta separación es crucial: si `containers.run()` fallara a mitad de camino,
            # no tendríamos la referencia del contenedor para destruirlo en el bloque finally.
            kwargs_crear = {
                "network": self._config.red_tareas,
            }
            # Montaje opcional de volúmenes compartidos (ej. volumen 'tp2-videos' mapeado en '/trabajos')
            # Permite procesamiento de videos pesados en disco sin transportarlos serializados por HTTP
            if volumenes:
                kwargs_crear["volumes"] = volumenes

            # Requisito R5 y Video: Inyección de límites de hardware mediante Linux cgroups en Docker
            # 1. Límite de memoria RAM (acota el consumo y protege al host contra OOM-Killer)
            limite_mem = mem_limit or getattr(self._config, "tarea_mem_limit", None)
            if limite_mem:
                kwargs_crear["mem_limit"] = limite_mem

            # 2. Límite de procesamiento de CPU (CFS quota en nanosegundos):
            # Convierte núcleos (ej. 0.5 o 1.0) a nanosegundos por segundo (1 núcleo = 1_000_000_000 nano_cpus)
            limite_cpus = cpus if cpus is not None else getattr(self._config, "tarea_cpus", None)
            if limite_cpus and limite_cpus > 0:
                kwargs_crear["nano_cpus"] = int(limite_cpus * 1_000_000_000)

            # Instanciamos el contenedor con la imagen y las restricciones de recursos
            contenedor = cliente.containers.create(imagen.referencia, **kwargs_crear)
            # Iniciamos el proceso del contenedor en segundo plano dentro de Docker
            contenedor.start()
            
            # Realizamos polling activo sobre el endpoint /health del contenedor hasta que esté listo
            base = self._esperar_listo(contenedor)
            # Enviamos el payload JSON con la operación matemática solicitada
            resultado = self._enviar(base, calculo, parametros, datos, timeout=timeout)
            
            # Retornamos la tupla con el resultado, el hash corto del contenedor, SHA256 de imagen y duración
            return Ejecucion(resultado, str(contenedor.id)[:12], local.id, _digest_ejecutado(imagen, local),
                             round(time.perf_counter() - inicio, 4))
        except APIError as error:
            # Error retornado por la API del daemon de Docker (fallo al crear o iniciar)
            raise ErrorApi(TipoError.TAREA_FALLIDA, "no se pudo levantar el contenedor de la tarea",
                           interno=str(error)) from None
        except _SIN_DOCKER as error:
            # Fallo de socket o daemon inaccesible
            raise _sin_docker(error) from None
        finally:
            # GARANTÍA DE LIMPIEZA ABSOLUTA (prevención de contenedores zombie):
            # Tanto si la tarea finalizó exitosamente como si falló o dio timeout,
            # forzamos la detención y borrado inmediato del contenedor efímero.
            if contenedor is not None:
                try:
                    contenedor.remove(force=True)
                except _SIN_DOCKER as error:
                    self._log.warning("no se pudo borrar el contenedor %s: %s", contenedor.id, error)


    def _asegurar_imagen(self, cliente, imagen):
        """Verifica si la imagen existe en el host local; si no, la descarga del registry.
        
        Asegura que la descarga se realice con las credenciales del servidor
        y únicamente si el registry coincide con el registry oficial configurado.
        """
        try:
            # Intentamos obtener la imagen del daemon local de Docker
            return cliente.images.get(imagen.referencia)
        except NotFound:
            # La imagen no reside aún en el almacenamiento local del host
            pass
        except _SIN_DOCKER as error:
            raise _sin_docker(error) from None

        # Configuramos credenciales de autenticación si el registry coincide con el autorizado
        credenciales = None
        if (self._config.registry_usuario and self._config.registry_token
                and imagen.registry == self._config.registry):
            credenciales = {"username": self._config.registry_usuario,
                            "password": self._config.registry_token}
        self._log.info("descargando %s (autenticado: %s)", imagen.referencia,
                       "sí" if credenciales else "no")
        try:
            # Descargamos la imagen oficial desde el Docker Registry remoto
            return cliente.images.pull(imagen.nombre, tag=imagen.version, auth_config=credenciales)
        except NotFound as error:
            # Si el registry responde 404, la imagen o versión solicitada no existe
            raise ErrorApi(TipoError.IMAGEN_INEXISTENTE,
                           f"la imagen {imagen.referencia} no existe o no es accesible",
                           interno=str(error)) from None
        except APIError as error:
            # Error de red o autenticación contra el registry de contenedores
            raise ErrorApi(TipoError.REGISTRY_NO_DISPONIBLE, "no se pudo descargar la imagen",
                           interno=str(error)) from None
        except _SIN_DOCKER as error:
            raise _sin_docker(error) from None

    def _ip(self, contenedor):
        """Extrae la dirección IP asignada al contenedor dentro de la red privada Docker.
        
        Se utiliza la dirección IP directa en lugar del nombre DNS del contenedor porque
        el nombre sólo resuelve dentro de la red Docker, permitiendo así que el servidor
        pueda correr de forma nativa en el host durante fases de desarrollo y pruebas.
        """
        return contenedor.attrs["NetworkSettings"]["Networks"][self._config.red_tareas]["IPAddress"]

    def _esperar_listo(self, contenedor):
        """Monitorea el inicio del servicio tarea realizando sondeo activo (healthcheck).
        
        En lugar de usar un retardo arbitrario (`sleep`), consulta periódicamente el
        endpoint `/health` del contenedor hasta que responda HTTP 200.
        
        Si el contenedor colapsa o finaliza prematuramente (código de salida != 0 o estado 'exited'),
        aborta de inmediato sin agotar innecesariamente el timeout de arranque.
        """
        limite = self._reloj() + self._config.timeout_arranque
        while self._reloj() < limite:
            # Refrescamos los metadatos del estado del contenedor desde el daemon de Docker
            contenedor.reload()
            # Si el contenedor murió, detectamos tempranamente que la imagen no es un servicio tarea válido
            if contenedor.status in ("exited", "dead"):
                raise ErrorApi(TipoError.TAREA_FALLIDA,
                               "el contenedor terminó antes de quedar listo: la imagen no es un "
                               f"servicio tarea (HTTP en el puerto {self._config.tarea_puerto})",
                               interno=f"código de salida {contenedor.attrs['State']['ExitCode']}")
            
            # Construimos la URL del healthcheck con la IP y el puerto de tareas
            base = f"http://{self._ip(contenedor)}:{self._config.tarea_puerto}"
            try:
                # Enviamos una petición GET /health con timeout corto (1 segundo)
                estado, _ = self._http("GET", base + "/health", timeout=1.0)
                if estado == 200:
                    # El microservicio tarea ya está inicializado y listo para aceptar cómputos
                    return base
            except (SinConexion, Vencido):
                # El servicio aún se encuentra arrancando; esperamos brevemente antes de reintentar
                pass
            self._dormir(0.2)
        
        # Si se agotó el tiempo límite de espera sin recibir respuesta HTTP 200
        raise ErrorApi(TipoError.TAREA_SIN_RESPUESTA,
                       f"la tarea no estuvo lista en {self._config.timeout_arranque:g} s")

    def _enviar(self, base, calculo, parametros, datos, timeout=None):
        """Envía la solicitud de cálculo a la tarea remota y desempaqueta el resultado.
        
        Valida que la respuesta cumpla con el sobre estándar del protocolo (contrato §5)
        y mapea adecuadamente los códigos de error HTTP a excepciones `ErrorApi`.
        """
        # Estructuramos el payload de la tarea remota
        cuerpo = {"calculo": calculo, "parametros": parametros, "datos": datos}
        t_espera = self._config.timeout_ejecucion if timeout is None else timeout
        try:
            # Enviamos el POST con el cálculo al endpoint /ejecutarTarea
            estado, respuesta = self._http("POST", base + "/ejecutarTarea", cuerpo,
                                            timeout=t_espera)
        except Vencido:
            # Excedió el tiempo límite máximo de procesamiento de la tarea
            raise EjecucionVencida(TipoError.TAREA_SIN_RESPUESTA,
                                   f"la tarea no respondió en {t_espera:g} s") from None
        except SinConexion as error:
            # Conexión reseteada o contenedor caído durante la ejecución
            raise ErrorApi(TipoError.TAREA_FALLIDA, "se perdió la conexión con la tarea",
                           interno=str(error)) from None

        # La tarea responde con el mismo sobre estructurado que el servidor (contrato §5)
        contenido = respuesta.get("contenido") if isinstance(respuesta, dict) else None
        
        # Caso de Éxito: HTTP 200 y presencia del campo "resultado" en el contenido
        if estado == 200 and isinstance(contenido, dict) and "resultado" in contenido:
            return contenido["resultado"]
        
        # Caso de Rechazo por Parámetros Inválidos (HTTP 4xx devuelto por el contenedor tarea)
        if 400 <= estado < 500:
            error = contenido.get("error") if isinstance(contenido, dict) else None
            mensaje = error.get("mensaje") if isinstance(error, dict) else None
            raise ErrorApi(TipoError.TAREA_RECHAZADA, mensaje or "la tarea rechazó los parámetros",
                           interno=f"HTTP {estado}")
        
        # Caso de Falla Interna o respuesta no estructurada (HTTP 5xx u otro)
        raise ErrorApi(TipoError.TAREA_FALLIDA, "la tarea falló o devolvió una respuesta inválida",
                       interno=f"HTTP {estado}, cuerpo={str(respuesta)[:300]}")
