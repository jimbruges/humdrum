#!/usr/bin/env bash
# Humdrum captive-portal hotspot for the UNO Q (run once, on the board, as root).
#
#   sudo ./install.sh                       # credentials from wifi.local.env, else wifi.env
#   sudo SSID=MY-PADS PASSWORD=secret123 ./install.sh
#
# What it sets up:
#   * a NetworkManager Wi-Fi access point (shared mode: NM runs DHCP via dnsmasq)
#   * wildcard DNS: every name resolves to the board, so phones detect a captive portal
#   * DHCP option 114 (RFC 8910) pointing at the portal for modern iOS / Android
#   * a tiny redirector on port 80 that sends every request to the app's web UI
#   * `humdrum-portal on|off|status` to switch between hotspot and your normal Wi-Fi
#
# The UNO Q has one Wi-Fi radio: while the hotspot is on, the board is not on your
# home network. Use USB (or `humdrum-portal off`) to reach it from App Lab.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# The lid's QR code is generated from the same file: wifi.local.env (your own,
# gitignored) if present, otherwise the public demo credentials in wifi.env.
ENV_FILE="$HERE/wifi.local.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$HERE/wifi.env"
if [[ -f "$ENV_FILE" ]]; then
  ENV_SSID=$(sed -n 's/^SSID=//p' "$ENV_FILE")
  ENV_PASSWORD=$(sed -n 's/^PASSWORD=//p' "$ENV_FILE")
fi
SSID="${SSID:-${ENV_SSID:-HUMDRUM}}"
PASSWORD="${PASSWORD:-${ENV_PASSWORD:-$(tr -dc 'a-km-z2-9' </dev/urandom | head -c 10)}}"
IFACE="${IFACE:-wlan0}"
AP_IP="${AP_IP:-10.42.0.1}"
APP_PORT="${APP_PORT:-7000}"
CON="humdrum-ap"

[[ $EUID -eq 0 ]] || { echo "run as root (sudo $0)"; exit 1; }
[[ ${#PASSWORD} -ge 8 ]] || { echo "PASSWORD must be at least 8 characters"; exit 1; }
command -v nmcli >/dev/null || { echo "NetworkManager (nmcli) not found"; exit 1; }

if ss -ltnH 'sport = :80' | grep -q .; then
  echo "warning: something already listens on port 80:"; ss -ltnp 'sport = :80'
  echo "the redirector will fail to start until that is stopped"
fi

echo "==> access point '$SSID' on $IFACE ($AP_IP)"
nmcli -t -f NAME con show | grep -qx "$CON" && nmcli con delete "$CON" >/dev/null
nmcli con add type wifi ifname "$IFACE" con-name "$CON" autoconnect no ssid "$SSID" \
  802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
  ipv4.method shared ipv4.addresses "$AP_IP/24" ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASSWORD" wifi-sec.proto rsn \
  wifi-sec.pairwise ccmp wifi-sec.group ccmp >/dev/null

echo "==> wildcard DNS + captive portal DHCP option"
install -d /etc/NetworkManager/dnsmasq-shared.d
sed -e "s/@AP_IP@/$AP_IP/g" "$HERE/dnsmasq-humdrum.conf" > /etc/NetworkManager/dnsmasq-shared.d/humdrum.conf

echo "==> port 80 redirector"
install -d /usr/local/lib/humdrum
install -m 0755 "$HERE/portal-redirect.py" /usr/local/lib/humdrum/portal-redirect.py
sed -e "s/@AP_IP@/$AP_IP/g" -e "s/@APP_PORT@/$APP_PORT/g" "$HERE/humdrum-redirect.service" \
  > /etc/systemd/system/humdrum-redirect.service
install -m 0755 "$HERE/humdrum-portal" /usr/local/bin/humdrum-portal
systemctl daemon-reload
systemctl enable --now humdrum-redirect.service

install -d /etc/humdrum
cat > /etc/humdrum/portal.env <<EOF
SSID=$SSID
PASSWORD=$PASSWORD
AP_IP=$AP_IP
APP_PORT=$APP_PORT
CON=$CON
EOF
chmod 600 /etc/humdrum/portal.env

cat <<EOF

Done. Hotspot is configured but not started.

  SSID      $SSID
  password  $PASSWORD
  portal    http://$AP_IP/   (any http:// address works while connected)

Start it now and at every boot:   sudo humdrum-portal on
Back to your normal Wi-Fi:        sudo humdrum-portal off

Wi-Fi QR payload (must match the QR embossed on your lid):
  WIFI:T:WPA;S:$SSID;P:$PASSWORD;;
EOF
