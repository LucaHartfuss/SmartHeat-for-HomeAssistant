#!/bin/bash
# Host-Installer im Debian-13-Container pruefen (Spec G2b-1 6/10). Plattform: SHG_INSTALL_PLATFORM (Standard
# linux/amd64; die CI faehrt zusaetzlich linux/arm64 per QEMU). Docker-Pakete laesst der Test aus (--no-docker).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PLATFORM="${SHG_INSTALL_PLATFORM:-linux/amd64}"
# Multi-Arch-Index-Digest von debian:trixie (docker buildx imagetools inspect debian:trixie, 2026-10-06).
DEBIAN_IMAGE="debian:trixie@sha256:913f6706df59a68922d1dd08f78c2476560a8d367897200a6005b00e5f67c2d5"
docker run --rm --platform "$PLATFORM" --security-opt label=disable --cap-add NET_ADMIN -v "$REPO:/src:ro" "$DEBIAN_IMAGE" \
  bash -c 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
             systemd udev nftables shellcheck python3 >/dev/null && bash /src/gateway/host/tests/install_checks.sh' \
  || { echo "FAIL: install.sh ($PLATFORM)"; exit 1; }
echo "PASS: install.sh ($PLATFORM)"
