#!/bin/bash
# Export der Container-Images fuer den Erststart ohne Pull (Plan G2b-2 Task 7/9). Laeuft in einem debian:trixie-
# Container mit skopeo, damit der Export unabhaengig vom Docker des Build-Hosts ist. Aufruf:
#   export_images.sh PLATTFORM ZIELORDNER REF...
# Je REF entsteht ZIELORDNER/NN.tar (OCI-Archiv des GANZEN Multi-Arch-Index samt Digests, skopeo copy --all
# --preserve-digests). Der Docker des Geraets (Speicherweg containerd, gateway/host/docker-daemon.json) loest daraus
# spaeter name@sha256:<Index-Digest> ohne Netz auf (Spike Plan G2b-2 Task 7). PLATTFORM ist fuer die Aufrufer
# gleichfoermig und hier ohne Wirkung: das Archiv enthaelt alle Plattformen.
set -euo pipefail
out=$2; shift 2
mkdir -p "$out"
n=0
for ref in "$@"; do
  n=$((n + 1))
  # skopeo kennt keine Referenz mit Tag UND Digest: der Tag entfaellt (der Digest bestimmt das Image).
  skopeo copy -q --all --preserve-digests "docker://$(sed -E 's/:[^:@/]+@/@/' <<<"$ref")" \
    "oci-archive:$out/$(printf '%02d' "$n").tar:$ref"
done
