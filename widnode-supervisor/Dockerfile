# FROM debian:bookworm-slim
FROM torizon/debian:3-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Paquetes del sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip python3-psutil python3-dateutil\
    network-manager modemmanager bluez curl \
    hostapd dnsmasq iproute2 iw rfkill \
    gpiod python3-libgpiod\
    procps psmisc \
    libqmi-utils libmbim-utils usb-modeswitch mobile-broadband-provider-info\
 && rm -rf /var/lib/apt/lists/*

# Venv compartiendo paquetes del sistema (para usar python3-psutil ya instalado)
RUN python3 -m venv /opt/venv --system-site-packages \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip

# UI (tu app de Hotspot)
COPY ui/ /opt/ui/
RUN if [ -f "/opt/ui/requirements.txt" ]; then \
      /opt/venv/bin/pip install --no-cache-dir -r /opt/ui/requirements.txt; \
    fi

# App cliente (tu gateway)
COPY userapp/ /opt/userapp/
RUN if [ -f "/opt/userapp/requirements.txt" ]; then \
      grep -vi '^psutil' /opt/userapp/requirements.txt > /tmp/req.userapp || true && \
      /opt/venv/bin/pip install --no-cache-dir -r /tmp/req.userapp; \
    fi

# Orquestador y scripts
COPY supervisor.py /opt/app/supervisor.py
COPY entrypoint.sh /opt/app/entrypoint.sh
COPY ap_stack.sh /opt/app/ap_stack.sh
COPY templates/ /opt/app/templates/
# COPY tacho_utils.py /opt/userapp/tacho_utils.py

RUN chmod +x /opt/app/entrypoint.sh /opt/app/ap_stack.sh

# copia el script
COPY tach_logger.py /opt/app/tach_logger.py
RUN chmod +x /opt/app/tach_logger.py



# Healthcheck script
COPY healthcheck.sh /opt/healthcheck.sh
RUN chmod +x /opt/healthcheck.sh



# activa el healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD /opt/healthcheck.sh || exit 1

# DBus del host para NM dentro del contenedor
ENV DBUS_SYSTEM_BUS_ADDRESS=unix:path=/var/run/dbus/system_bus_socket

ENTRYPOINT ["/opt/app/entrypoint.sh"]
