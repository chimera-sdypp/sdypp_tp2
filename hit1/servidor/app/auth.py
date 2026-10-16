"""Autenticación y autorización por request (R4).

El servidor no guarda sesión: la identidad y el rol viajan en cada pedido, en un
JWT (HS256) en `Authorization: Bearer <token>`, y se validan en cada llamada. El
token lo firma quien lo emite con el secreto compartido (`TP2_JWT_SECRET`) y lleva:

    sub   el usuario
    role  `usuario` (ejecuta tareas) o `admin` (además, `/stats`)
    exp   cuándo vence (obligatorio)
"""

from typing import NamedTuple

import jwt

from app.errores import ErrorApi, TipoError

ROL_USUARIO = "usuario"
ROL_ADMIN = "admin"
ROLES = (ROL_USUARIO, ROL_ADMIN)
ALGORITMO = "HS256"
LARGO_MINIMO_SECRETO = 32


class Identidad(NamedTuple):
    usuario: str
    rol: str


def validar_secreto(secreto):
    """Sin un secreto de verdad el servidor no arranca: nunca queda abierto por defecto."""
    if len(secreto or "") < LARGO_MINIMO_SECRETO:
        raise ValueError(
            f"TP2_JWT_SECRET falta o tiene menos de {LARGO_MINIMO_SECRETO} caracteres: "
            "el servidor no arranca sin autenticación")


def _no_autorizado(mensaje):
    return ErrorApi(TipoError.NO_AUTORIZADO, mensaje, cabeceras={"WWW-Authenticate": "Bearer"})


def autenticar(cabecera_authorization, secreto):
    """La identidad del pedido, o `NO_AUTORIZADO` (401) si falta, no es válido o venció."""
    partes = (cabecera_authorization or "").split(None, 1)
    if len(partes) != 2 or partes[0].lower() != "bearer":
        raise _no_autorizado("falta el token: Authorization: Bearer <token>")
    try:
        datos = jwt.decode(partes[1].strip(), secreto, algorithms=[ALGORITMO],
                           options={"require": ["exp", "sub"]})
    except jwt.ExpiredSignatureError:
        raise _no_autorizado("el token venció") from None
    except jwt.InvalidTokenError:
        raise _no_autorizado("token inválido") from None
    usuario = datos["sub"]
    if not isinstance(usuario, str) or not usuario:
        raise _no_autorizado("token inválido")
    rol = datos.get("role")
    if rol not in ROLES:
        raise ErrorApi(TipoError.ROL_INSUFICIENTE, "el token no trae un rol válido")
    return Identidad(usuario, rol)
