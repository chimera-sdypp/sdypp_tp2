#!/usr/bin/env bash
# Prepara una VM Ubuntu con Docker para servir el Hit 3 (3 nodos + nginx). Se corre una
# sola vez, como root, con el docker-compose.yml, el nginx.conf y el .env de la VM en el
# mismo directorio:
#
#   scp -i clave.pem hit3/despliegue/instalar_vm.sh hit3/docker-compose.yml \
#       hit3/nginx/nginx.conf ubuntu@<IP>:
#   (crear ~/.env en la VM, ver "Despliegue" en hit3/README.md)
#   ssh -i clave.pem ubuntu@<IP> sudo bash instalar_vm.sh
#
# Deja /opt/sdypp-tp2-hit3 y un timer de systemd (sdypp-tp2-hit3.timer) que cada 5
# minutos hace `docker compose pull && up -d`: la VM trae sola la imagen que el CI publica en
# GHCR, sin ninguna credencial en GitHub. El mismo `up -d` vuelve a levantar un nodo al
# que se haya matado para probar la caída del coordinador.
set -euo pipefail

DESTINO=/opt/sdypp-tp2-hit3
ORIGEN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "correr como root: sudo bash $0" >&2
    exit 1
fi
for archivo in docker-compose.yml nginx.conf .env; do
    if [ ! -f "$ORIGEN/$archivo" ]; then
        echo "falta $archivo junto a este script" >&2
        exit 1
    fi
done

mkdir -p "$DESTINO/nginx"
install -m 0644 "$ORIGEN/docker-compose.yml" "$DESTINO/docker-compose.yml"
install -m 0644 "$ORIGEN/nginx.conf" "$DESTINO/nginx/nginx.conf"   # el compose lo monta de ./nginx/
install -m 0600 "$ORIGEN/.env" "$DESTINO/.env"   # lleva el token de Docker Hub

cat > /etc/systemd/system/sdypp-tp2-hit3.service <<'UNIT'
[Unit]
Description=TP2 Hit 3: traer la última imagen publicada y levantar el cluster
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=/opt/sdypp-tp2-hit3
# Un solo candado para los timers de todos los hits: si el `image prune` de uno corre
# mientras otro baja una imagen, Docker pierde la descarga ("lease does not exist").
ExecStart=/usr/bin/flock /run/lock/sdypp-tp2-despliegue.lock /usr/bin/docker compose pull --quiet
ExecStart=/usr/bin/flock /run/lock/sdypp-tp2-despliegue.lock /usr/bin/docker compose up -d --no-build --remove-orphans
# Las imágenes :latest reemplazadas no se acumulan en el disco.
ExecStart=/usr/bin/flock /run/lock/sdypp-tp2-despliegue.lock /usr/bin/docker image prune -f
UNIT

cat > /etc/systemd/system/sdypp-tp2-hit3.timer <<'UNIT'
[Unit]
Description=TP2 Hit 3: buscar una imagen nueva cada 5 minutos

[Timer]
OnBootSec=30s
OnUnitActiveSec=5min
AccuracySec=5s

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now sdypp-tp2-hit3.timer > /dev/null
echo "[instalar] listo. Comandos útiles:"
echo "  systemctl list-timers sdypp-tp2-hit3.timer    # próxima actualización"
echo "  journalctl -u sdypp-tp2-hit3.service -n 20    # qué hizo la última"
echo "  docker compose -f $DESTINO/docker-compose.yml logs nodo1 nodo2 nodo3 --tail 40"
echo "  docker compose -f $DESTINO/docker-compose.yml kill nodo3   # simular la caída del coordinador"
