#!/usr/bin/env bash
# Prepara una VM Ubuntu con Docker para servir el Hit 2. Se corre una sola vez, como
# root, con el docker-compose.yml del Hit 2 y el .env de la VM en el mismo directorio:
#
#   scp -i clave.pem hit2/despliegue/instalar_vm.sh hit2/docker-compose.yml ubuntu@<IP>:
#   (crear ~/.env en la VM, ver "Despliegue" en hit2/README.md)
#   ssh -i clave.pem ubuntu@<IP> sudo bash instalar_vm.sh
#
# Deja /opt/sdypp-tp2-hit2 y un timer de systemd (sdypp-tp2-hit2.timer) que cada minuto
# hace `docker compose pull && up -d`: la VM trae sola la imagen que el CI publica en
# GHCR, sin ninguna credencial en GitHub.
set -euo pipefail

DESTINO=/opt/sdypp-tp2-hit2
ORIGEN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "correr como root: sudo bash $0" >&2
    exit 1
fi
for archivo in docker-compose.yml .env; do
    if [ ! -f "$ORIGEN/$archivo" ]; then
        echo "falta $archivo junto a este script" >&2
        exit 1
    fi
done

mkdir -p "$DESTINO"
install -m 0644 "$ORIGEN/docker-compose.yml" "$DESTINO/docker-compose.yml"
install -m 0600 "$ORIGEN/.env" "$DESTINO/.env"   # lleva el token de Docker Hub

cat > /etc/systemd/system/sdypp-tp2-hit2.service <<'UNIT'
[Unit]
Description=TP2 Hit 2: traer la última imagen publicada y levantarla
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=/opt/sdypp-tp2-hit2
ExecStart=/usr/bin/docker compose pull --quiet servidor
ExecStart=/usr/bin/docker compose up -d --no-build --remove-orphans
# Las imágenes :latest reemplazadas no se acumulan en el disco.
ExecStart=/usr/bin/docker image prune -f
UNIT

cat > /etc/systemd/system/sdypp-tp2-hit2.timer <<'UNIT'
[Unit]
Description=TP2 Hit 2: buscar una imagen nueva cada minuto

[Timer]
OnBootSec=30s
OnUnitActiveSec=1min
AccuracySec=5s

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now sdypp-tp2-hit2.timer > /dev/null
echo "[instalar] listo. Comandos útiles:"
echo "  systemctl list-timers sdypp-tp2-hit2.timer    # próxima actualización"
echo "  journalctl -u sdypp-tp2-hit2.service -n 20    # qué hizo la última"
echo "  docker compose -f $DESTINO/docker-compose.yml logs servidor --tail 40"
