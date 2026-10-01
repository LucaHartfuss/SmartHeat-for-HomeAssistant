# Wrapt den offiziellen cloudflared-Client im 'access tcp'-Modus als HA-Add-on.
#
# Das offizielle cloudflared-Image ist seit einer fruehreren Version ein
# distroless-Image (User 65532:65532, kein Shell/kein apk) - run.sh (ein
# POSIX-sh-Skript, das jq braucht) kann darin nicht direkt laufen. Deshalb
# hier nur der cloudflared-Binary aus dem offiziellen Image uebernehmen und
# in ein normales Alpine-Image mit Shell+jq kopieren.
FROM cloudflare/cloudflared:2025.8.1@sha256:b77d84e8704db38db22c22661cf7e56468c526e3a6a5fe9c8b7c151452fa1472 AS cloudflared

FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
RUN apk add --no-cache jq
COPY --from=cloudflared /usr/local/bin/cloudflared /usr/local/bin/cloudflared

COPY run.sh /run.sh
RUN chmod +x /run.sh

ENTRYPOINT ["/run.sh"]
