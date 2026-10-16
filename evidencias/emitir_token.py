#!/usr/bin/env python3
"""Emite un JWT HS256 para los servidores del TP2 (R4). Sólo usa la librería estándar.

    TP2_JWT_SECRET=... python3 evidencias/emitir_token.py --usuario ana --rol usuario
    TP2_JWT_SECRET=... python3 evidencias/emitir_token.py --usuario root --rol admin --horas 8
    TP2_JWT_SECRET=... python3 evidencias/emitir_token.py --usuario ana --segundos -5      # ya vencido

El secreto es el mismo que tiene el servidor (TP2_JWT_SECRET); sale del entorno, nunca de
un argumento, para que no quede en el historial del shell. `--secreto-erroneo` firma con otro
secreto (para probar que el servidor lo rechaza). Imprime sólo el token.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time


def _b64(datos):
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode()


def emitir(secreto, usuario, rol, segundos, ahora=None):
    ahora = int(time.time() if ahora is None else ahora)
    cabecera = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    cuerpo = _b64(json.dumps({"sub": usuario, "role": rol, "iat": ahora, "exp": ahora + segundos},
                             separators=(",", ":")).encode())
    firma = hmac.new(secreto.encode(), f"{cabecera}.{cuerpo}".encode(), hashlib.sha256).digest()
    return f"{cabecera}.{cuerpo}.{_b64(firma)}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--usuario", required=True)
    parser.add_argument("--rol", choices=["usuario", "admin"], default="usuario")
    parser.add_argument("--horas", type=float, default=1.0, help="cuánto dura el token (por defecto 1 hora)")
    parser.add_argument("--segundos", type=int, help="duración en segundos (pisa --horas; negativo = ya vencido)")
    parser.add_argument("--secreto-erroneo", action="store_true", help="firmar con un secreto que no es el del servidor")
    args = parser.parse_args()

    secreto = os.environ.get("TP2_JWT_SECRET", "")
    if len(secreto) < 32:
        sys.exit("TP2_JWT_SECRET falta o tiene menos de 32 caracteres")
    if args.secreto_erroneo:
        secreto = "secreto-erroneo-" + secreto[::-1]
    duracion = args.segundos if args.segundos is not None else int(args.horas * 3600)
    print(emitir(secreto, args.usuario, args.rol, duracion))


if __name__ == "__main__":
    main()
