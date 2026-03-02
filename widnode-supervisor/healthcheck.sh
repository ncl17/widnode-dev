#!/usr/bin/env bash
set -e


# Estado GPIO decide modo (AP=1, CLIENT=0)
MODE="$(gpioget ${GPIOCHIP:-/dev/gpiochip3} ${GPIOLINE:-6} || echo 0)"

# --- LTE helper (solo si está habilitado en /data/config.ini) ---
: "${CONFIG_FILE:=/data/config.ini}"
: "${LTE_IF:=wwan0}"

lcheck_lte() {
  # si no hay seccion LTE/LTE-4G o no está enabled=true, no exigimos LTE
  if ! grep -qiE '^\s*\[(LTE|LTE-4G)\]\s*$' "$CONFIG_FILE" 2>/dev/null; then
    return 0
  fi

  # leer "enabled=" de la primera seccion LTE/LTE-4G
  local lte_enabled
  lte_enabled="$(awk '
    BEGIN{IGNORECASE=1; in=0}
    /^\[(LTE|LTE-4G)\]/{in=1; next}
    /^\[.*\]/{in=0}
    in && $1 ~ /^enabled[[:space:]]*=/ {
      gsub(/[[:space:]]/, "", $0);
      split($0, a, "=");
      print tolower(a[2]); exit
    }' "$CONFIG_FILE" 2>/dev/null)"

  [ "$lte_enabled" = "true" ] || return 0

  # 1) nmcli ve un dispositivo gsm conectado/connecting
  if nmcli -t -f DEVICE,STATE,TYPE dev 2>/dev/null \
     | awk -F: '$3=="gsm" && ($2 ~ /connected|connecting/){found=1} END{exit found?0:1}'; then
    return 0
  fi

  # 2) mmcli ve modem conectado
  if mmcli -L 2>/dev/null | grep -q "Modem"; then
    local MID
    MID="$(mmcli -L 2>/dev/null | sed -n 's|.*Modem \([0-9]\+\).*|\1|p' | head -1)"
    if [ -n "$MID" ] && mmcli -m "$MID" --simple-status 2>/dev/null | grep -qi 'state: connected'; then
      return 0
    fi
  fi

  # 3) interfaz LTE con IP (fallback)
  if ip -brief addr show "$LTE_IF" 2>/dev/null | grep -qE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+'; then
    return 0
  fi

  echo "LTE enabled en config pero SIN conectividad" >&2
  return 1
}


if [ "$MODE" = "1" ]; then
  # AP: hostapd + dnsmasq + UI en 8080 deben estar vivos y mlan0 unmanaged
  pgrep -x hostapd >/dev/null || exit 1
  pgrep -x dnsmasq >/dev/null || exit 1
  curl -fsS "http://${AP_ADDR:-192.168.17.1}:8080/healthz" >/dev/null || exit 1
else
  # CLIENTE: la app y al menos una interfaz con IP
  # pgrep -f "python .*app.py" >/dev/null || exit 1
  curl -fsS "http://127.0.0.1:${USERAPP_HEALTH_PORT:-9090}/healthz" >/dev/null || exit 1

  nmcli -t -f GENERAL.STATE device show ${ETH_IF:-end0} 2>/dev/null | grep -qE "30|100" \
    || nmcli -t -f IP4.ADDRESS device show ${WIFI_IF:-mlan0} 2>/dev/null | grep -q "IP4.ADDRESS" \
    || exit 1

  # si LTE está habilitado en config.ini, exigir conectividad LTE
  lcheck_lte || exit 1
fi
exit 0
