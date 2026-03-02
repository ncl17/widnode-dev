#!/usr/bin/env bash
set -euo pipefail

AP_IF="${AP_IF:-uap0}"
WIFI_IF="${WIFI_IF:-mlan0}"

AP_SSID="${AP_SSID:-WIDNODE_AP}"
AP_PSK="${AP_PSK:-widnode1234}"
AP_CC="${AP_CC:-AR}"
AP_CH="${AP_CH:-6}"

AP_ADDR="${AP_ADDR:-192.168.17.1}"
AP_MASK="${AP_MASK:-255.255.255.0}"
AP_DHCP_START="${AP_DHCP_START:-192.168.17.100}"
AP_DHCP_END="${AP_DHCP_END:-192.168.17.200}"
AP_DNS="${AP_DNS:-${AP_ADDR}}"
AP_NAT="0"                      # forzamos SIN NAT  # 0 = sin NAT (recomendado), 1 = con NAT
AP_LOG_DNS="${AP_LOG_DNS:-0}"  # 0 = sin log de consultas, 1 = con log

UI_CMD="${UI_CMD:-/opt/venv/bin/python /opt/ui/app.py}"
CONFIG_FILE="${CONFIG_FILE:-/data/config.ini}"
export CONFIG_FILE

mkdir -p /run /tmp /data/logs
UI_PID=""
DNSMASQ_PID=""
HOSTAPD_PID=""

cleanup() {
  set +e
  echo "[AP] cleanup: bajando servicios y red"
  if [[ -n "${DNSMASQ_PID}" ]] && kill -0 "${DNSMASQ_PID}" 2>/dev/null; then
    kill "${DNSMASQ_PID}" ; wait "${DNSMASQ_PID}" 2>/dev/null
  fi
  if [[ -f /run/hostapd_ap.pid ]]; then
    kill "$(cat /run/hostapd_ap.pid)" 2>/dev/null || true
    rm -f /run/hostapd_ap.pid
  else
    pkill -f "hostapd.*hostapd.conf" 2>/dev/null || true
  fi
  
  ip link set "${AP_IF}" down 2>/dev/null || true

  if [[ -n "${UI_PID}" ]] && kill -0 "${UI_PID}" 2>/dev/null; then
    kill "${UI_PID}" ; wait "${UI_PID}" 2>/dev/null
  fi
  # sin NAT: no tocamos iptables ni ip_forward
  ip addr flush dev "${AP_IF}" 2>/dev/null || true
  ip link set "${AP_IF}" down 2>/dev/null || true
  nmcli dev set "${WIFI_IF}" managed yes >/dev/null 2>&1 || true
  echo "[AP] cleanup: listo"
}
trap cleanup INT TERM EXIT

echo "[AP] AP en ${AP_IF} SSID=${AP_SSID} CH=${AP_CH} CC=${AP_CC} IP=${AP_ADDR}/24"
nmcli dev set "${WIFI_IF}" managed no >/dev/null 2>&1 || true
rfkill unblock all >/dev/null 2>&1 || true

ip link set "${AP_IF}" down || true
ip addr flush dev "${AP_IF}" || true
ip link set "${AP_IF}" up
ip addr add "${AP_ADDR}/24" dev "${AP_IF}"

# hostapd
cat >/tmp/hostapd.conf <<EOF
interface=${AP_IF}
driver=nl80211
ssid=${AP_SSID}
country_code=${AP_CC}
ieee80211d=1
hw_mode=g
channel=${AP_CH}
wmm_enabled=1
ieee80211n=1
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=${AP_PSK}
EOF

hostapd -B /tmp/hostapd.conf
HOSTAPD_PID="$(pgrep -xo hostapd || true)"
echo "${HOSTAPD_PID}" >/run/hostapd_ap.pid 2>/dev/null || true

# dnsmasq
cat >/tmp/dnsmasq.conf <<EOF
interface=${AP_IF}
bind-interfaces
dhcp-range=${AP_DHCP_START},${AP_DHCP_END},${AP_MASK},12h
dhcp-option=3,${AP_ADDR}
dhcp-option=6,${AP_DNS}
$( [[ "${AP_LOG_DNS}" = "1" ]] && echo "log-queries" )
log-facility=/data/logs/dnsmasq.log
EOF

echo "[AP] NAT deshabilitado. Portal local únicamente."

echo "[AP] Lanzando UI: ${UI_CMD}"
( cd /opt/ui && ${UI_CMD} ) >/data/logs/ap_ui.log 2>&1 &
UI_PID=$!

dnsmasq --conf-file=/tmp/dnsmasq.conf --keep-in-foreground --pid-file=/run/dnsmasq_ap.pid &
DNSMASQ_PID=$!

echo "[AP] Listo. Abrí http://${AP_ADDR}:8080"
wait "${DNSMASQ_PID}"
