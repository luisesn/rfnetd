#!/bin/bash
# Instala rfnetd en /opt/rfnetd y registra la unidad systemd. No arranca el servicio.
# Uso: sudo ./install.sh [--enable] [--audio-share]
#   --audio-share  crea /etc/asound.conf (dsnoop) si no existe, para escuchar a la vez que Direwolf
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "ejecutar como root (sudo)"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"

mkdir -p /opt/rfnetd /etc/rfnetd /var/log/rfnetd /var/lib/rfnetd
rm -rf /opt/rfnetd/rfnetd
cp -r "$SRC/rfnetd" /opt/rfnetd/
find /opt/rfnetd -name __pycache__ -type d -prune -exec rm -rf {} +
if [ ! -e /etc/rfnetd/rfnetd.conf ]; then
  cp "$SRC/etc/rfnetd.conf.example" /etc/rfnetd/rfnetd.conf
  echo "Creado /etc/rfnetd/rfnetd.conf (revísalo)"
else
  echo "Se conserva /etc/rfnetd/rfnetd.conf existente"
fi
cp "$SRC/systemd/rfnetd.service" /etc/systemd/system/rfnetd.service
systemctl daemon-reload
for a in "$@"; do
  case "$a" in
    --enable) systemctl enable rfnetd ;;
    --audio-share)
      if [ -e /etc/asound.conf ]; then echo "Ya existe /etc/asound.conf: no se toca (ver etc/asound.conf.example)"
      else cp "$SRC/etc/asound.conf.example" /etc/asound.conf; echo "Creado /etc/asound.conf (revisa hw:X,Y)"; fi ;;
  esac
done
chmod 600 /etc/rfnetd/rfnetd.conf

for p in direwolf tncattach; do
  if ! systemctl is-active --quiet rfnetd && pgrep -x "$p" >/dev/null; then
    echo "AVISO: ya hay un '$p' en marcha (lanzado a mano). Páralo antes de 'systemctl start rfnetd'."
  fi
done
echo "Instalado. Arranque:  sudo systemctl start rfnetd   |  logs: journalctl -u rfnetd -f"
