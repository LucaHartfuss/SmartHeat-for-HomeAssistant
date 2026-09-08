# Wrapt den offiziellen cloudflared-Client im 'access tcp'-Modus als HA-Add-on.
#
# Das offizielle cloudflared-Image ist seit einer fruehreren Version ein
# distroless-Image (User 65532:65532, kein Shell/kein apk) - run.sh (ein
# POSIX-sh-Skript, das jq braucht) kann darin nicht direkt laufen. Deshalb
# hier nur der cloudflared-Binary aus dem offiziellen Image uebernehmen und
# in ein normales Alpine-Image mit Shell+jq kopieren.
FROM cloudflare/cloudflared:2025.8.1 AS cloudflared

FROM alpine:3.20
RUN apk add --no-cache jq
COPY --from=cloudflared /usr/local/bin/cloudflared /usr/local/bin/cloudflared

COPY run.sh /run.sh
RUN chmod +x /run.sh

ENTRYPOINT ["/run.sh"]
