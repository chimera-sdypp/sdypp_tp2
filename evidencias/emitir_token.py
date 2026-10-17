#!/usr/bin/env python3
"""Generador de Tokens JWT (JSON Web Tokens) — Requisito R4.

¿Para qué sirve este archivo?
Es la herramienta que utilizamos para fabricar tokens de prueba válidos (o inválidos)
para interactuar con el servidor de tareas remotas.

Principios Fundamentales de Implementación:
1. SÓLO BIBLIOTECA ESTÁNDAR: No requiere instalar paquetes externos como `PyJWT` o `cryptography`.
   Utiliza exclusivamente módulos nativos de Python (`hmac`, `hashlib`, `base64`, `json`),
   permitiendo generar tokens en cualquier máquina o contenedor sin dependencias previas.
2. ESTÁNDARES RFC CUMPLIDOS:
   - RFC 7519 (JSON Web Token - JWT).
   - RFC 7515 (JSON Web Signature - JWS con codificación Base64URL sin padding '=').
   - RFC 2104 (HMAC: Keyed-Hashing for Message Authentication).
3. SEGURIDAD DEL SECRETO:
   El secreto criptográfico se lee EXCLUSIVAMENTE de la variable de entorno `TP2_JWT_SECRET`.
   Nunca se pasa como argumento de línea de comandos (`--secreto ...`) para evitar que
   quede registrado en el historial de comandos de la terminal (`.bash_history`) o visible
   para otros usuarios en la tabla de procesos (`ps aux`).

Ejemplos de Uso:
    # 1. Exportar la clave secreta compartida (mínimo 32 caracteres)
    export TP2_JWT_SECRET="secreto-de-prueba-de-al-menos-32-caracteres"

    # 2. Emitir un token estándar para un usuario regular (duración 1 hora por defecto)
    python3 evidencias/emitir_token.py --usuario ana --rol usuario

    # 3. Emitir un token de administrador para consultar el endpoint privilegiado /stats
    python3 evidencias/emitir_token.py --usuario root --rol admin --horas 8

    # 4. Emitir un token ya vencido (para verificar que el servidor responda 401)
    python3 evidencias/emitir_token.py --usuario ana --segundos -60

    # 5. Emitir un token con firma adulterada (para probar que el servidor detecte el fraude)
    python3 evidencias/emitir_token.py --usuario ana --secreto-erroneo
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time


# ------------------------------------------------------------------------------
# 1. Codificador Base64URL (RFC 7515 - JWS)
# ------------------------------------------------------------------------------
def _b64(datos: bytes) -> str:
    """Codifica bytes en formato Base64URL seguro para URLs y cabeceras HTTP.
    
    A diferencia del Base64 clásico:
    - Reemplaza '+' por '-' y '/' por '_' (evita conflictos con URLs y query params).
    - Remueve los caracteres de relleno '=' al final (rstrip), según exige el estándar JWT.
    """
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode()


# ------------------------------------------------------------------------------
# 2. Fabricación Criptográfica del Token (Header . Payload . Signature)
# ------------------------------------------------------------------------------
def emitir(secreto: str, usuario: str, rol: str, segundos: int, ahora: int | None = None) -> str:
    """Construye y firma un JWT completo con algoritmo HS256.
    
    Parámetros:
      - secreto: Clave simétrica de firma (compartida con el servidor).
      - usuario: Identidad del cliente (se almacena en el claim estándar 'sub').
      - rol: Permisos asignados ('usuario' o 'admin', almacenado en 'role').
      - segundos: Tiempo de vida del token a partir del instante de emisión.
      - ahora: Timestamp Unix base (opcional, por defecto time.time() actual).
    
    Retorna:
      String con formato 'Header.Payload.Firma' listo para la cabecera Authorization.
    """
    # Instante Unix actual en segundos enteros
    ahora = int(time.time() if ahora is None else ahora)

    # PARTE 1: Header (Cabecera)
    # Define el algoritmo simétrico HMAC-SHA256 y el tipo de token
    # Usamos separators=(',', ':') para eliminar espacios en blanco y compactar el JSON
    datos_cabecera = {"alg": "HS256", "typ": "JWT"}
    cabecera = _b64(json.dumps(datos_cabecera, separators=(",", ":")).encode("utf-8"))

    # PARTE 2: Payload (Cuerpo con Claims RFC 7519)
    # - sub (Subject): Identifica unívocamente al cliente (para el limitador de tasa).
    # - role: Perfil de acceso para autorización RBAC en los endpoints.
    # - iat (Issued At): Momento exacto de emisión.
    # - exp (Expiration Time): Momento en que caduca. Si segundos < 0, nace vencido.
    datos_cuerpo = {
        "sub": usuario,
        "role": rol,
        "iat": ahora,
        "exp": ahora + segundos
    }
    cuerpo = _b64(json.dumps(datos_cuerpo, separators=(",", ":")).encode("utf-8"))

    # PARTE 3: Signature (Firma Digital HMAC-SHA256)
    # Se calcula sobre la unión de la cabecera y el cuerpo separados por un punto:
    # Firma = HMAC_SHA256(cabecera + "." + cuerpo, secreto)
    mensaje_a_firmar = f"{cabecera}.{cuerpo}".encode("utf-8")
    firma_binaria = hmac.new(secreto.encode("utf-8"), mensaje_a_firmar, hashlib.sha256).digest()
    firma = _b64(firma_binaria)

    # Ensamblado final de las tres partes: A.B.C
    return f"{cabecera}.{cuerpo}.{firma}"


# ------------------------------------------------------------------------------
# 3. Interfaz de Línea de Comandos (CLI)
# ------------------------------------------------------------------------------
def main():
    """Punto de entrada de consola para la generación parametrizada de tokens."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    # Argumentos obligatorios y opcionales
    parser.add_argument(
        "--usuario",
        required=True,
        help="Nombre o identificador del sujeto (claim 'sub')"
    )
    parser.add_argument(
        "--rol",
        choices=["usuario", "admin"],
        default="usuario",
        help="Rol de autorización: 'usuario' (por defecto) o 'admin'"
    )
    parser.add_argument(
        "--horas",
        type=float,
        default=1.0,
        help="Tiempo de validez en horas (por defecto 1.0 hora)"
    )
    parser.add_argument(
        "--segundos",
        type=int,
        help="Tiempo de validez en segundos (tiene prioridad sobre --horas; negativo = nace vencido)"
    )
    parser.add_argument(
        "--secreto-erroneo",
        action="store_true",
        help="Firma deliberadamente con un secreto falso (para testing de rechazo 401)"
    )
    args = parser.parse_args()

    # Lectura y validación Fail-Closed del secreto del entorno
    secreto = os.environ.get("TP2_JWT_SECRET", "")
    if len(secreto) < 32:
        sys.exit(
            "ERROR: La variable TP2_JWT_SECRET falta o tiene menos de 32 caracteres.\n"
            "Exportá un secreto seguro antes de ejecutar: export TP2_JWT_SECRET='...'"
        )

    # Si se solicitó simular un ataque o firma falsa, alteramos la clave simétrica
    if args.secreto_erroneo:
        secreto = "secreto-erroneo-" + secreto[::-1]

    # Cálculo de la duración final en segundos
    duracion = args.segundos if args.segundos is not None else int(args.horas * 3600)

    # Imprime ÚNICAMENTE el token por salida estándar (stdout)
    # Esto permite capturarlo directamente en variables de bash: TOKEN=$(python3 emitir_token.py ...)
    token_generado = emitir(secreto, args.usuario, args.rol, duracion)
    print(token_generado)


if __name__ == "__main__":
    main()

