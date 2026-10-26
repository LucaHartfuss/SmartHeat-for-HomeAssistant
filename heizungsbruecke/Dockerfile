FROM python:3.12-alpine@sha256:4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111
# Konvention des Supervisors fuer Add-on-Images: Version und Architektur kommen als Build-Argumente (BUILD_VERSION,
# BUILD_ARCH; release.yml und der Supervisor setzen sie) und stehen in den Labels io.hass.*.
ARG BUILD_VERSION
ARG BUILD_ARCH
LABEL io.hass.version="${BUILD_VERSION}" io.hass.type="addon" io.hass.arch="${BUILD_ARCH}"
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY src ./src
# Das Paket selbst wird nicht installiert: src/ liegt auf dem Suchpfad. So holt der Build kein
# Build-Backend (setuptools) ungelockt aus dem Netz (TP12e, AU-018).
ENV PYTHONPATH=/app/src
ENTRYPOINT ["python", "-m", "heizungsbruecke"]
