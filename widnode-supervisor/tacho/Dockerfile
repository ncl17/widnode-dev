# Base universal y liviana para arm64
FROM --platform=linux/arm64 debian:bookworm-slim

# Evitar prompts
ENV DEBIAN_FRONTEND=noninteractive

# Paquetes necesarios: Python + libgpiod
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-libgpiod ca-certificates tzdata \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY tachometer.py /app/
# (si tenés requirements.txt, descomentá)
# COPY requirements.txt /app/
# RUN pip3 install --no-cache-dir -r requirements.txt

ENTRYPOINT ["python3", "tachometer.py"]
