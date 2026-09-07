# Wrapt den offiziellen cloudflared-Client im 'access tcp'-Modus als HA-Add-on.
FROM cloudflare/cloudflared:2025.8.1

USER root
RUN apk add --no-cache jq

COPY run.sh /run.sh
RUN chmod +x /run.sh

ENTRYPOINT ["/run.sh"]
