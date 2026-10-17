"""Autenticación y Autorización por Request (Requisito R4).

Principios Fundamentales de Seguridad:
1. ARQUITECTURA STATELESS: El servidor no mantiene sesiones en memoria ni cookies;
   toda la identidad y los privilegios viajan de forma autónoma en cada petición HTTP
   mediante un token JWT firmado criptográficamente con algoritmo HS256.
2. ROLES DESACOPLADOS (RBAC - Role Based Access Control):
   - `usuario`: rol estándar para enviar tareas a `/getRemoteTask`.
   - `admin`: rol de administración exclusivo para auditar métricas en `/stats`.
3. POLÍTICA FAIL-CLOSED (Falla Segura):
   Si el servidor no recibe un secreto de al menos 32 caracteres (`TP2_JWT_SECRET`),
   aborta de inmediato en el arranque. Jamás permite operar de forma insegura.
4. ESTÁNDARES RFC:
   - RFC 7519 (JSON Web Token - JWT).
   - RFC 6750 (The OAuth 2.0 Authorization Framework: Bearer Token Usage).
"""

from typing import NamedTuple
import jwt

from app.errores import ErrorApi, TipoError

# Constantes de Seguridad y Roles
ROL_USUARIO = "usuario"
ROL_ADMIN = "admin"
ROLES = (ROL_USUARIO, ROL_ADMIN)

# Algoritmo de firma simétrica HMAC usando SHA-256
ALGORITMO = "HS256"

# 32 caracteres = 256 bits de entropía mínima recomendada para evitar ataques
# de fuerza bruta o diccionario sobre firmas HMAC-SHA256.
LARGO_MINIMO_SECRETO = 32


# ------------------------------------------------------------------------------
# 1. Modelo de Identidad del Request (Inmutable)
# ------------------------------------------------------------------------------
class Identidad(NamedTuple):
    """Representa al usuario autenticado y su rol tras validar la firma del JWT.
    
    Al ser NamedTuple, es inmutable y liviana, ideal para inyectar en `request.state`.
    """
    usuario: str  # Sujeto del token (claim estándar 'sub')
    rol: str      # Rol de autorización (claim 'role': 'usuario' o 'admin')


# ------------------------------------------------------------------------------
# 2. Validación de Configuración en el Arranque (Fail-Closed)
# ------------------------------------------------------------------------------
def validar_secreto(secreto: str) -> None:
    """Verifica que el secreto cumpla con la entropía mínima requerida (>= 32 caracteres).
    
    ¿Por qué se valida en el arranque?
    Para cumplir con el principio de seguridad 'Fail-Closed': si el secreto no fue
    configurado en el archivo `.env` o en los Docker Secrets, el servidor lanza
    una excepción y se apaga de inmediato antes de abrir el puerto de red.
    """
    if len(secreto or "") < LARGO_MINIMO_SECRETO:
        raise ValueError(
            f"TP2_JWT_SECRET falta o tiene menos de {LARGO_MINIMO_SECRETO} caracteres: "
            "el servidor no arranca sin autenticación segura."
        )


def _no_autorizado(mensaje: str) -> ErrorApi:
    """Construye un error HTTP 401 NO_AUTORIZADO según el estándar RFC 6750.
    
    La cabecera `WWW-Authenticate: Bearer` le indica formalmente al cliente HTTP
    que debe reintentar la solicitud incluyendo un token de autenticación Bearer válido.
    """
    return ErrorApi(
        TipoError.NO_AUTORIZADO,
        mensaje,
        cabeceras={"WWW-Authenticate": "Bearer"}
    )


# ------------------------------------------------------------------------------
# 3. Middleware / Función de Autenticación de Peticiones
# ------------------------------------------------------------------------------
def autenticar(cabecera_authorization: str | None, secreto: str) -> Identidad:
    """Valida el token Bearer del request y retorna la Identidad del usuario.
    
    Flujo de Validación Criptográfica y Contractual:
    1. Verificación sintáctica de cabecera HTTP `Authorization: Bearer <token>`.
    2. Decodificación y validación de firma con algoritmo HS256 y secreto del host.
    3. Exigencia estricta de claims RFC 7519: `sub` (sujeto) y `exp` (expiración temporal).
    4. Control de expiración: tokens vencidos son rechazados inmediatamente.
    5. Autorización de Rol: rechaza con 403 si el rol no es 'usuario' ni 'admin'.
    
    Lanza:
      - 401 NO_AUTORIZADO: si falta el token, venció, o la firma digital es inválida.
      - 403 ROL_INSUFICIENTE: si el token tiene firma válida pero el rol no está reconocido.
    """
    # Paso 1: Parsear el esquema de autorización 'Bearer <token>'
    # Usamos split(None, 1) para separar por cualquier espacio en blanco
    partes = (cabecera_authorization or "").split(None, 1)
    if len(partes) != 2 or partes[0].lower() != "bearer":
        raise _no_autorizado("falta el token: Authorization: Bearer <token>")
    
    token = partes[1].strip()

    # Paso 2: Validación criptográfica del JWT
    try:
        datos = jwt.decode(
            token,
            secreto,
            algorithms=[ALGORITMO],
            # Exigimos explícitamente 'exp' y 'sub': un token sin tiempo de vida
            # o sin usuario emisor es rechazado por diseño de seguridad.
            options={"require": ["exp", "sub"]}
        )
    except jwt.ExpiredSignatureError:
        # El token fue emitido legítimamente pero ya superó su timestamp 'exp'
        raise _no_autorizado("el token venció") from None
    except jwt.InvalidTokenError:
        # Firma HMAC alterada, secreto incorrecto, formato base64 corrupto, etc.
        raise _no_autorizado("token inválido") from None

    # Paso 3: Validación del Sujeto ('sub' según RFC 7519)
    usuario = datos.get("sub")
    if not isinstance(usuario, str) or not usuario.strip():
        raise _no_autorizado("token inválido: 'sub' debe ser un string no vacío")

    # Paso 4: Validación de Autorización por Rol (RBAC)
    rol = datos.get("role")
    if rol not in ROLES:
        # Si la firma es correcta pero el rol no es ni 'usuario' ni 'admin',
        # no es un 401 (la persona demostró quién es), sino un 403 (no tiene permisos suficientes).
        raise ErrorApi(TipoError.ROL_INSUFICIENTE, "el token no trae un rol válido")

    # Paso 5: Retornar la identidad autenticada lista para ser consumida por el servidor
    return Identidad(usuario=usuario.strip(), rol=rol)

