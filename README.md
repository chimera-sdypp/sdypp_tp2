# sdypp_tp2 — Sistemas Distribuidos y Concurrencia

TP 2 de Sistemas Distribuidos y Programación Paralela (UNLu, 2C 2026). Grupo Cerberus: Salvador
Baez, Mateo Nomico y Tomás Resnik.

## Qué es

Un servidor HTTP que ejecuta **tareas remotas**: el cliente le manda un cálculo y la imagen Docker
que sabe resolverlo, el servidor levanta esa imagen como contenedor temporal, le pasa el trabajo,
devuelve el resultado y borra el contenedor. El mismo servidor crece en tres pasos:

| Hit | Qué agrega | Desplegado en |
|---|---|---|
| [Hit 1](hit1/) | El servidor y el servicio tarea. La imagen de la tarea es privada: el servidor la baja con su propio token, nunca con credenciales del cliente | `http://18.231.127.74:8081` |
| [Hit 2](hit2/) | Concurrencia: un pool de N workers, una cola con exclusión mutua y relojes de Lamport. Mediciones de throughput con 1, 2, 4 y 8 workers, contra la ley de Amdahl | `http://18.231.127.74:8082` |
| [Hit 3](hit3/) | Tolerancia a fallos: 3 instancias detrás de nginx y un coordinador elegido con Bully. Si se cae el coordinador o un nodo, las tareas siguen | `http://18.231.127.74:8083` |

**Qué se le puede pedir a cada uno, y qué contesta, está en el [contrato público](contrato-publico.md).**

## Cómo está organizado

Cada carpeta `hitN/` es independiente y tiene su `README.md` con cómo correrlo, el diagrama de
arquitectura y las decisiones de diseño, y su `contrato.md` con el detalle interno: reglas de
validación, el contrato con el servicio tarea y, en el Hit 3, los mensajes entre nodos.

| Carpeta | Qué hay |
|---|---|
| `servidor/` | El servidor (Python, FastAPI), con sus tests unitarios y de integración |
| `tarea/` | El servicio tarea (`suma`, `resta`, `multiplicacion`, `division`), publicado en Docker Hub como `cerberusdistribuido/tarea` |
| `cliente/` | Un cliente de línea de comandos |
| `despliegue/` | El script que instala el hit en la VM |
| `mediciones/` | (Hits 2 y 3) Los datos y gráficos del informe |

## Correrlo en local

Hace falta Docker con Compose v2 y Python 3 para el cliente. Para cualquier hit:

```bash
cd hit1                       # o hit2, hit3
cp .env.example .env          # y completarlo, ver el README del hit
docker compose up --build -d
```

## CI/CD

Cada push corre en GitHub Actions `gitleaks` (falla si hay un secreto en el código) y los tests
unitarios y de integración de los tres hits. En `main`, además, publica la imagen de cada servidor en
GHCR. La VM baja la imagen nueva en hasta 5 minutos, sin credenciales en GitHub, y un último
job prueba el servidor desplegado desde Internet.

## Contrato público y Swagger

- [`contrato-publico.md`](contrato-publico.md): la API de los tres hits, con ejemplos.
- [`openapi.yaml`](openapi.yaml): el mismo contrato en OpenAPI 3.1, para abrir en Swagger.
