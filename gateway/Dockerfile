# SmartHeat-Gateway (Spec SHG G2 7.1, Spec 5b 5.3): ein Image fuer den Gateway-Container runtime (Geraetekern, Laufzeit,
# Diagnoseseite) und den Init-Schritt. Build-Kontext ist die Repo-Wurzel:
#   docker build -f gateway/Dockerfile .
# src/ liegt auf dem Suchpfad, das Paket wird nicht installiert (kein ungelocktes Build-Backend).
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
WORKDIR /app
COPY gateway/requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY heizungsbruecke/src/smartheat_core ./src/smartheat_core
COPY heizungsbruecke/src/smartheat_transport ./src/smartheat_transport
COPY heizungsbruecke/src/smartheat_runtime ./src/smartheat_runtime
COPY heizungsbruecke/src/smartheat_device ./src/smartheat_device
COPY gateway/src/smartheat_gateway ./src/smartheat_gateway
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
USER 1000:1000
CMD ["python", "-m", "smartheat_gateway.runtime_main"]
