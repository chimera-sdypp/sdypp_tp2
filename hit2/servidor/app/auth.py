"""Autenticación y Autorización por Request (Requisito R4).

Principios Fundamentales de Seguridad:
1. ARQUITECTURA STATELESS: El servidor no mantiene sesiones en memoria ni cookies;
   toda la identidad y los privilegios viajan de forma autónoma en cada petición HTTP
   mediante un token JWT firmado criptográficamente con algoritmo HS256.
2. ROLES DESACOPLADOS:
   - `usuario`: habilitado para solicitar ejecuciones de tareas en `/getRemoteTask`.
   - `admin`: rol privilegiado con acceso exclusivo al endpoint de observabilidad `/stats`.
3. POLÍTICA FAIL-CLOSED: Si el servidor no recibe un secreto de al menos 32 caracteres
   (`TP2_JWT_SECRET`), aborta el arranque de inmediato. Jamás queda abierto por defecto.
"""

from typing import NamedTuple
import jwt

from app.errores import ErrorApi, TipoError

ROL_USUARIO = "usuario"
ROL_ADMIN = "admin"
ROLES = (ROL_USUARIO, ROL_ADMIN)
ALGORITMO = "HS256"
LARGO_MINIMO_SECRETO = 32


# ------------------------------------------------------------------------------
# 1. Modelo de Identidad del Request
# ------------------------------------------------------------------------------
class Identidad(NamedTuple):
    """Representa al usuario autenticado y su rol tras validar la firma del JWT."""
    usuario: str
    rol: str


# ------------------------------------------------------------------------------
# 2. Validación de Configuración (Fail-Closed)
# ------------------------------------------------------------------------------
def validar_secreto(secreto: str) -> None:
    """Verifica que el secreto cumpla con la entropía mínima requerida (>= 32 caracteres).
    
    Lanza ValueError si el secreto es nulo o débil, impidiendo que el servidor inicie.
    """
    if len(secreto or "") < LARGO_MINIMO_SECRETO:
        raise ValueError(
            f"TP2_JWT_SECRET falta o tiene menos de {LARGO_MINIMO_SECRETO} caracteres: "
            "el servidor no arranca sin autenticación segura."
        )


def _no_autorizado(mensaje: str) -> ErrorApi:
    """Genera error HTTP 401 tipificado con cabecera WWW-Authenticate."""
    return ErrorApi(
        TipoError.NO_AUTORIZADO,
        mensaje,
        cabeceras={"WWW-Authenticate": "Bearer"}
    )


# ------------------------------------------------------------------------------
# 3. Middleware de Autenticación de Peticiones
# ------------------------------------------------------------------------------
def autenticar(cabecera_authorization: str | None, secreto: str) -> Identidad:
    """Valida el token Bearer del request y retorna la Identidad del usuario.
    
    Lanza:
      - 401 NO_AUTORIZADO: si falta la cabecera, el token está vencido o la firma es inválida.
      - 403 ROL_INSUFICIENTE: si el payload no contiene un rol conocido ('usuario' o 'admin').
    """
    partes = (cabecera_authorization or "").split(None, 1)
    if len(partes) != 2 or partes[0].lower() != "bearer":
        raise _no_autorizado("falta el token: Authorization: Bearer <token>")
    
    try:
        datos = jwt.decode(
            partes[1].strip(),
            secreto,
            algorithms=[ALGORITMO],
            options={"require": ["exp", "sub"]}
        )
    except jwt.ExpiredSignatureError:
        raise _no_autorizado("el token venció") from None
    except jwt.InvalidTokenError:
        raise _no_autorizado("token inválido") from None

    usuario = datos.get("sub")
    if not isinstance(usuario, str) or not usuario:
        raise _no_autorizado("token inválido: 'sub' debe ser un string no vacío")

    rol = datos.get("role")
    if rol not in ROLES:
        raise ErrorApi(TipoError.ROL_INSUFICIENTE, "el token no trae un rol válido")

    return Identidad(usuario, rol)
