# Hit 1 — Tareas remotas en contenedores (servidor)

Servidor HTTP contenerizado que recibe una tarea por `POST` (JSON), levanta **temporalmente** el
servicio tarea como contenedor Docker, le pasa el trabajo, espera el resultado, se lo devuelve al
cliente y borra el contenedor.

- Contrato de la API: [`contrato.md`](contrato.md)
- `getRemoteTask()` → [`servidor/app/main.py`](servidor/app/main.py) ·
  `ejecutarTareaRemota()` → [`servidor/app/lanzador.py`](servidor/app/lanzador.py)

## Cómo correrlo

Requisitos: Docker con Compose v2, Linux (o WSL2). Todo desde la carpeta `hit1/`.

```bash
cd hit1
cp .env.example .env
# Completar en .env:
#   DOCKER_GID=<salida de: stat -c %g /var/run/docker.sock>
#   TP2_IMAGENES_PERMITIDAS=<repositorio de la imagen tarea, p. ej. cerberus/tarea>
docker compose up --build -d --wait
curl -s localhost:8080/health
```

Mandar una tarea:

```bash
curl -s -X POST localhost:8080/getRemoteTask \
  -H 'Content-Type: application/json' \
  -d '{"calculo": "suma", "parametros": {"a": 3, "b": 4}, "imagen": "cerberus/tarea:1.0.0"}'
```

Logs: `docker compose logs -f servidor` (consola) y el volumen `logs` (disco, archivo rotativo);
además el servidor guarda las últimas 500 líneas en memoria.

Bajar todo: `docker compose down` (con `-v` borra también los logs).

### Tests

```bash
cd hit1/servidor
python3 -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python -m pytest                   # unitarios y de API: no necesitan Docker
./tests/integracion.sh             # integración: levanta todo con Docker real, prueba y lo baja
```

Los de integración usan una **tarea de prueba** ([`servidor/tests/tarea_prueba/`](servidor/tests/tarea_prueba/))
que cumple el contrato del servicio tarea y permite forzar cada falla (división por cero, error
500, timeout).

## Arquitectura

```mermaid
flowchart LR
    C[Cliente] -- "POST /getRemoteTask<br/>JSON" --> S
    subgraph H[Host con Docker]
        S["Servidor<br/>FastAPI"] -- "SDK de Docker<br/>/var/run/docker.sock" --> D[(Daemon<br/>Docker)]
        D -. "crea / borra" .-> T["Contenedor tarea<br/>(efímero)"]
        S -- "GET /health<br/>POST /ejecutarTarea<br/>red tp2-tareas" --> T
    end
    D -- "pull con token<br/>de sólo lectura" --> R[(Docker Hub)]
```

```mermaid
sequenceDiagram
    participant C as Cliente
    participant S as Servidor
    participant D as Daemon Docker
    participant T as Contenedor tarea
    C->>S: POST /getRemoteTask {calculo, parametros, datos, imagen}
    S->>S: valida el payload (400/413/415/422) y la lista blanca (403)
    S->>D: ¿está la imagen? si no, pull (credenciales del servidor)
    S->>D: create + start en la red tp2-tareas
    loop hasta el timeout de arranque
        S->>T: GET /health
    end
    S->>T: POST /ejecutarTarea {calculo, parametros, datos}
    T-->>S: {codigo, contenido: {resultado}}
    S->>D: remove --force (siempre, también si falló)
    S-->>C: 200 {codigo, contenido: {calculo, resultado}}
```

## Decisiones de diseño

### Credenciales del registry (punto de seguridad)

**Qué hicimos: configuración previa en el host con un token de sólo lectura.** El cliente nunca
manda ni conoce credenciales: el payload no las acepta (un campo `password` es un `422`). El
servidor usa un **token de acceso de Docker Hub con permiso sólo de lectura** (no la contraseña de
la cuenta) que el operador pone en el `.env` del host; en el deploy lo inyecta el CD desde **GitHub
Secrets**. Compose lo monta como archivo en `/run/secrets/registry_token` y el servidor se lo pasa
al daemon en cada pull, **sólo si la imagen es de Docker Hub**.

Por qué es más seguro que mandar usuario y contraseña en el JSON:

| En el JSON | Configurado en el host |
|---|---|
| Viaja en cada request y queda en logs, proxies y el historial del cliente | No sale del host |
| Lo tiene que conocer cada cliente: se filtra con el primero que se filtre | No lo conoce ningún cliente |
| Suele ser la contraseña de la cuenta: permite escribir y borrar imágenes | Token revocable, **sólo lectura** |
| — | Es un archivo, no una variable de entorno: no aparece en `docker inspect` |
| — | Se manda sólo a Docker Hub: un pull de otro registry no se lleva el token |

Es el equivalente de un *image pull secret* sin Kubernetes: la plataforma le entrega el secreto a
quien hace el pull, y el cliente no interviene. Los *image pull secrets* y *Workload Identity /
OIDC* propiamente dichos son de Kubernetes y de la nube (TP3); los **tokens de corta duración** no
tienen un flujo directo en Docker Hub.

> ⚠️ **"Hacer `docker login` en el host" no alcanza** si el servidor corre en un contenedor: las
> credenciales de un pull las manda el **cliente** de Docker (acá, el SDK adentro del servidor) y el
> daemon no guarda ninguna. Un `docker login` en el host las deja en `~/.docker/config.json` del
> host, donde el servidor no las ve. Por eso el token le llega como secreto.

### Lista blanca de imágenes

El cliente elige la imagen y el servidor la corre con acceso al socket de Docker, que **equivale a
root en el host**. Sin control, cualquiera que llegue a `/getRemoteTask` ejecutaría lo que quisiera.
Por eso sólo se aceptan los repositorios de `TP2_IMAGENES_PERMITIDAS` (vacía = ninguno), y siempre
con una versión fija (tag o digest; `latest` no).

### API

- **Validar y rechazar, nunca "arreglar":** tipos estrictos, sin campos de más, claves duplicadas y
  `NaN` rechazados. Cada error dice qué campo falló y por qué.
- **Siempre JSON y el mismo sobre salga bien o mal:** `{"codigo", "contenido"}`; si es un error,
  `contenido` es `{"error": {tipo, mensaje, detalles}}`, también en 404 y 405. Los errores son un
  enum (`TipoError`) con su código HTTP. El servicio tarea responde con el mismo sobre.
- **Nada interno en las respuestas:** ni IPs, ni nombres de contenedores, ni errores de Docker. Eso
  va al log.
- **`/health` que no miente:** `503` si Docker no responde, aunque el proceso esté vivo.
- **Sigue atendiendo mientras corre una tarea:** la tarea se ejecuta en un hilo aparte
  (`run_in_threadpool`).

### Contenedor tarea

- **Se espera su `/health`** antes de mandarle el trabajo, en vez de un `sleep` fijo; con timeout.
- **Se borra siempre** (`finally`), salga bien o mal.
- **Se le habla por su IP en la red `tp2-tareas`**, sin publicar puertos en el host. La IP y no el
  nombre porque el nombre sólo resuelve desde adentro de la red de Docker; así el servidor también
  anda corriendo suelto en el host (desarrollo en Linux).
- **SDK de Docker y no el CLI:** le habla directo al socket, así la imagen no trae el binario de
  `docker`.

## Configuración

Variables de entorno (las principales están en [`.env.example`](.env.example)):
`TP2_IMAGENES_PERMITIDAS`, `TP2_TIMEOUT_EJECUCION` (60 s), `TP2_TIMEOUT_ARRANQUE` (30 s),
`TP2_TAREA_PUERTO` (8080), `TP2_RED_TAREAS` (`tp2-tareas`), `TP2_REGISTRY_USUARIO`,
`TP2_REGISTRY_TOKEN_ARCHIVO`, `TP2_DIR_LOGS`.

## Pendiente

- Acordar el contrato del servicio tarea ([`contrato.md` §5](contrato.md)) con la imagen real
  publicada en Docker Hub, y probar el pull privado con el token.
- Cliente, CD, despliegue público y tests contra lo desplegado.
