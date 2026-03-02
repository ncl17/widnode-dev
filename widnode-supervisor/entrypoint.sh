#!/usr/bin/env bash
set -euo pipefail

# DBus de host para NM/MM/BlueZ (requieres bind-mount del socket en compose)
export DBUS_SYSTEM_BUS_ADDRESS="${DBUS_SYSTEM_BUS_ADDRESS:-unix:path=/var/run/dbus/system_bus_socket}"

# --- asegurar directorio de logs persistente ---
mkdir -p /data/logs /home/widnode/dev/widnode
# reemplazar carpeta física por symlink si fuera necesario
if [ -e /home/widnode/dev/widnode/log ] && [ ! -L /home/widnode/dev/widnode/log ]; then
  rm -rf /home/widnode/dev/widnode/log
fi
ln -sfn /data/logs /home/widnode/dev/widnode/log
# ----------------------------------------------

CONFIG_PATH="${CONFIG_PATH:-/data/config.ini}"
mkdir -p "$(dirname "$CONFIG_PATH")"

# Si no existe o está vacío, generamos un config por defecto
if [ ! -s "$CONFIG_PATH" ]; then
  echo "[INIT] Generando $CONFIG_PATH por defecto"
  cat >"$CONFIG_PATH" <<'EOF'

[General]
failover = true              


[WiFi]
enabled = false
ssid = 
password = 
internet = false             
metric = 300                 
lan_routes =                 

[Ethernet]
enabled = true
mode = dhcp
ip_address = 
netmask = 
gateway = 
dns = 
internet = false            
metric = 200                
lan_routes = 



[LTE-4G]
enabled = false
apn = 
internet = true              
metric = 100                 

[API]
api_url = http://app.id-ingenieria.com:5011/api

[MODBUS_SERVER]
ENABLED = true
BIND = 0.0.0.0
PORT = 502
UNIT_ID = 1
BASE = 0
STRIDE = 16
MAX_INDEX = 256

[Tachometer]
enabled = true
ppr = 1
avg_window_sec = 2
log_period_sec = 10
keep_days = 14

[APP]
cmd = /opt/venv/bin/python /opt/userapp/app.py

[System]
timezone = America/Argentina/Buenos_Aires

EOF
  chmod 644 "$CONFIG_PATH"
fi

# === CONFIG & TZ bootstrap ===
CONFIG_FILE="${CONFIG_FILE:-/data/config.ini}"
mkdir -p "$(dirname "$CONFIG_FILE")"

# Crea / actualiza config.ini y garantiza [System].timezone
TZ_VALUE="$(
python3 - <<'PY'
import os, configparser, subprocess

cfgpath = os.environ.get("CONFIG_FILE", "/data/config.ini")
os.makedirs(os.path.dirname(cfgpath), exist_ok=True)

cfg = configparser.ConfigParser()
if os.path.exists(cfgpath):
    cfg.read(cfgpath)
else:
    # Plantilla mínima por defecto si no existe
    cfg["WiFi"] = {"enabled":"true", "ssid":"", "password":""}
    cfg["Ethernet"] = {"enabled":"true","mode":"dhcp",
                       "ip_address":"","netmask":"","gateway":"","dns":""}
    cfg["LTE-4G"] = {"enabled":"false","apn":""}
    cfg["API"] = {"api_url":"http://app.id-ingenieria.com:5011/api"}
    cfg["MODBUS"] = {"enabled":"false","host":"iface:end0","port":"502","unit_id":"1","base":"0"}
    cfg["AP"] = {"channel":"6"}
    cfg["APP"] = {"cmd":"/opt/venv/bin/python /opt/userapp/app.py"}

# Asegura sección System.timezone
if "System" not in cfg: cfg["System"] = {}
if not cfg["System"].get("timezone","").strip():
    # Intenta tomar la TZ del host si existe timedatectl, sino UTC
    tz = "UTC"
    try:
        tz = subprocess.check_output(
            ["timedatectl","show","--property=Timezone","--value"],
            text=True
        ).strip() or "UTC"
    except Exception:
        pass
    cfg["System"]["timezone"] = tz

with open(cfgpath,"w") as f:
    cfg.write(f)

print(cfg["System"]["timezone"])
PY
)"

# Exporta TZ en el contenedor (afecta todas las apps)
export TZ="$TZ_VALUE"
ln -sf "/usr/share/zoneinfo/$TZ_VALUE" /etc/localtime 2>/dev/null || true
echo "$TZ_VALUE" >/etc/timezone 2>/dev/null || true

echo "[BOOT] CONFIG_FILE=$CONFIG_FILE | TZ=$TZ"

# IMPORTANTE: No iniciar ModemManager en el contenedor.
# Usamos el ModemManager del host vía D-Bus. Si necesitás mmcli:
#  - mantené el paquete instalado, pero no el servicio.
#  - asegurá el mount del socket D-Bus del host en el compose

exec /opt/venv/bin/python /opt/app/supervisor.py
