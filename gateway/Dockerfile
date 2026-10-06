# SmartHeat-Gateway (Spec SHG G2 7.1): ein Image fuer Agent, Laufzeit und Tunnel. Build-Kontext ist die Repo-Wurzel:
#   docker build -f gateway/Dockerfile .
# cloudflared kommt wie im Add-on cloudflared_access_mqtt aus dem per Digest gepinnten offiziellen Image (Plan G2a,
# Praezisierung 6). src/ liegt auf dem Suchpfad, das Paket wird nicht installiert (kein ungelocktes Build-Backend).
FROM cloudflare/cloudflared:2025.8.1@sha256:b77d84e8704db38db22c22661cf7e56468c526e3a6a5fe9c8b7c151452fa1472 AS cloudflared

FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
WORKDIR /app
COPY gateway/requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY --from=cloudflared /usr/local/bin/cloudflared /usr/local/bin/cloudflared
COPY heizungsbruecke/src/smartheat_core ./src/smartheat_core
COPY heizungsbruecke/src/smartheat_transport ./src/smartheat_transport
COPY heizungsbruecke/src/smartheat_runtime ./src/smartheat_runtime
COPY gateway/src/smartheat_gateway ./src/smartheat_gateway
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
USER 1000:1000
CMD ["python", "-m", "smartheat_gateway.agent"]
