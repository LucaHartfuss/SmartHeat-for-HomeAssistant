#!/usr/bin/env python3
"""Baut das signierte RAUC-System-Bundle (Format verity) am Betreiberrechner aus den Slot-Images des Werks-Images
(make_image.sh: system-<v>.ext4 und boot-<v>.vfat) und signiert es mit der Release-Kette (rauc_signieren.py, S-3, S-10).

Ablauf:
  1. Das Programm rauc (1.13, plus squashfs-tools und e2fsprogs) laeuft in einem Container aus dem per Digest gepinnten
     debian:trixie; das Abbild baut `docker build` einmal und haelt es im Cache. Als Benutzer des Aufrufers (nicht root)
     braucht rauc ein beschreibbares data-directory der system.conf (/var/lib/rauc, im Abbild mit Modus 1777). Der Container sieht genau einen Ordner
     (die Buehne unter --arbeit), unter demselben Pfad wie auf dem Rechner (-v P:P): so gelten alle Pfade in den Befehlen
     von rauc und rauc_signieren.py hier wie dort. Er laeuft ohne Netz und als der Benutzer des Aufrufers. Der Schluessel
     und das Hardware-Token bleiben ausserhalb: openssl signiert auf dem Rechner (rauc_signieren.py).
  2. Das Bundle-Verzeichnis enthaelt die beiden Images (als harte Verknuepfung, sonst Kopie) und das Manifest
     (`manifest_text`: [update] compatible, version; [bundle] format=verity; Slots rootfs und boot).
  3. Die Version im Image muss die des Releases sein (`pruefe_version`): das Host-Paket im Image traegt GATEWAY_VERSION, und
     der Updater meldet sie; ein Image einer anderen Version fiele auf jedem Geraet mit `start_fehlgeschlagen` zurueck.
     Gelesen wird die Datei im Image selbst (debugfs, nur lesend), nicht der Name der Datei.
  4. `rauc bundle` mit einer Wegwerf-Kette (ca.py test-kette) baut das Rohbundle, rauc_signieren.signiere_bundle ersetzt
     ihre Signatur durch die der Release-Kette und prueft das Ergebnis wie das Geraet (rauc info mit system.conf.in).
  5. Die Buehne samt Wegwerf-Schluessel wird in jedem Fall geloescht.

Aufruf: rauc_bundle.py --version V --system DATEI --boot DATEI --zertifikat DATEI --schluessel PEM|pkcs11:URI --roots DATEI
                        --arbeit ORDNER --out DATEI
  --arbeit: neuer Ordner (darf nicht existieren) auf demselben Dateisystem wie --system und --boot, wenn die Images nicht
            kopiert werden sollen; hier liegt die Buehne.
Exit 0 = signiert und geprueft, 1 = Fehler (es bleibt kein Ergebnis liegen), 2 = falscher Aufruf."""
import argparse
import hashlib
import os
import re
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

import ca
from rauc_signieren import lauf, signiere_bundle
from signieren import SigniereFehler

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))  # Aufruf als Skript ohne PYTHONPATH
from smartheat_host import bundles  # noqa: E402

# Wie tests/test_gateway_install.sh und tests/test_gateway_rauc.sh: der Multi-Arch-Index-Digest von debian:trixie.
BASIS_IMAGE = "debian:trixie@sha256:913f6706df59a68922d1dd08f78c2476560a8d367897200a6005b00e5f67c2d5"
DOCKERFILE = f"""FROM {BASIS_IMAGE}
RUN apt-get update -qq \\
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends rauc squashfs-tools e2fsprogs \\
 && rm -rf /var/lib/apt/lists/* \\
 && install -d -m 1777 /var/lib/rauc \\
 && rauc --version | grep -q ' 1\\.13'
"""
# Der Tag folgt dem Inhalt des Dockerfiles: wer es aendert, baut neu, sonst trifft `docker build` den Cache.
RAUC_IMAGE = f"setwell-rauc:{hashlib.sha256(DOCKERFILE.encode()).hexdigest()[:12]}"
IMAGE_VERSION_PY = "/opt/smartheat/host/smartheat_gateway/version.py"
IMAGE_VERSION_DATEI = "/opt/smartheat/host/VERSION"
_GATEWAY_VERSION = re.compile(r'^GATEWAY_VERSION = "(\d+\.\d+\.\d+)"$', re.MULTILINE)
_VERSION_DATEI = re.compile(r"^(\d+\.\d+\.\d+)\n?$")


class BundleFehler(Exception):
    pass


def system_name(version: str) -> str:
    return f"system-{version}.ext4"


def boot_name(version: str) -> str:
    return f"boot-{version}.vfat"


def manifest_text(version: str) -> str:
    """manifest.raucm des Bundles: das Geraet nimmt es nur mit `compatible` seiner system.conf an; die Slot-Klassen
    rootfs und boot sind die der system.conf (rootfs.0/1 auf system_a/b, boot.0/1 auf boot_a/b mit parent)."""
    bundles.parse_version(version)
    return (f"[update]\ncompatible={bundles.SYSTEM_COMPATIBLE}\nversion={version}\n\n[bundle]\nformat=verity\n\n"
            f"[image.rootfs]\nfilename={system_name(version)}\n\n[image.boot]\nfilename={boot_name(version)}\n")


def container_befehl(buehne: Path) -> list[str]:
    """Befehlszeile bis vor das Programm im Container: nur `buehne` ist sichtbar, unter demselben Pfad."""
    return ["docker", "run", "--rm", "--network", "none", "--security-opt", "label=disable",
            "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{buehne}:{buehne}", "-w", str(buehne), RAUC_IMAGE]


def abbild_bauen(schluessel: str) -> None:
    lauf(["docker", "build", "--quiet", "-t", RAUC_IMAGE, "-"], schluessel, "docker build (rauc)", DOCKERFILE.encode())


def _verknuepfen(quelle: Path, ziel: Path) -> None:
    """Harte Verknuepfung (kostet nichts, die Images sind mehrere GB gross), sonst eine Kopie."""
    try:
        os.link(quelle, ziel)
    except OSError:
        shutil.copyfile(quelle, ziel)


def pruefe_version(befehl: Sequence[str], image: Path, version: str, schluessel: str) -> None:
    """Das Host-Paket im Image (version.py und VERSION) traegt `version`; sonst BundleFehler."""
    for pfad, muster in ((IMAGE_VERSION_PY, _GATEWAY_VERSION), (IMAGE_VERSION_DATEI, _VERSION_DATEI)):
        text = lauf([*befehl, "debugfs", "-R", f"cat {pfad}", str(image)], schluessel, f"debugfs {pfad}").decode(errors="replace")
        treffer = muster.search(text)
        if treffer is None:
            raise BundleFehler(f"Image: {pfad} nennt keine Version (Image von make_image.sh?)")
        if treffer.group(1) != version:
            raise BundleFehler(f"Image: {pfad} traegt Version {treffer.group(1)}, das Release hat {version} "
                               "(gateway/VERSION des Tags und GATEWAY_VERSION muessen die Release-Version sein)")


def baue(version: str, system: Path, boot: Path, ziel: Path, *, zertifikat: Path, schluessel: str, roots: Path,
         arbeit: Path) -> None:
    """Baut und signiert das System-Bundle nach `ziel`. `arbeit` ist ein neuer Ordner (die Buehne), er wird am Ende
    geloescht. Wirft BundleFehler oder SigniereFehler; dann liegt kein `ziel` da und keine Buehne."""
    bundles.parse_version(version)
    if ziel.exists() or arbeit.exists():
        raise BundleFehler(f"{ziel} oder {arbeit} existiert schon, nichts ueberschrieben")
    for datei in (system, boot, zertifikat, roots):
        if not datei.is_file():
            raise BundleFehler(f"Datei fehlt: {datei}")
    buehne = arbeit.resolve()
    ziel = ziel.absolute()
    try:
        buehne.mkdir(parents=True)
        inhalt, kette, ordner_arbeit = buehne / "inhalt", buehne / "kette", buehne / "arbeit"
        inhalt.mkdir()
        ordner_arbeit.mkdir()
        _verknuepfen(system, inhalt / system_name(version))
        _verknuepfen(boot, inhalt / boot_name(version))
        (inhalt / "manifest.raucm").write_text(manifest_text(version))
        shutil.copyfile(roots, buehne / "roots.pem")
        ca.test_kette_anlegen(kette)
        abbild_bauen(schluessel)
        befehl = container_befehl(buehne)
        pruefe_version(befehl, inhalt / system_name(version), version, schluessel)
        roh = buehne / "roh.raucb"
        lauf([*befehl, "rauc", "bundle", "--cert", str(kette / "release.crt"), "--key", str(kette / "release.key"),
              str(inhalt), str(roh)], schluessel, "rauc bundle")
        fertig = buehne / bundles.SYSTEM_NAME
        signiere_bundle(roh, fertig, zertifikat, schluessel, roots=buehne / "roots.pem", wegwerf_ca=kette / "root.crt",
                        rauc=[*befehl, "rauc"], arbeit=ordner_arbeit)
        shutil.copyfile(fertig, ziel)
        ziel.chmod(0o644)
    except BaseException:
        ziel.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(buehne, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True)
    parser.add_argument("--system", type=Path, required=True, help="system-<v>.ext4 von make_image.sh")
    parser.add_argument("--boot", type=Path, required=True, help="boot-<v>.vfat von make_image.sh")
    parser.add_argument("--zertifikat", type=Path, required=True)
    parser.add_argument("--schluessel", required=True, metavar="PEM|pkcs11:URI")
    parser.add_argument("--roots", type=Path, required=True)
    parser.add_argument("--arbeit", type=Path, required=True, help="neuer Ordner fuer die Buehne (wird geloescht)")
    parser.add_argument("--out", type=Path, required=True, help="Ergebnis, wird nie ueberschrieben")
    args = parser.parse_args(argv)
    try:
        baue(args.version, args.system, args.boot, args.out, zertifikat=args.zertifikat, schluessel=args.schluessel,
             roots=args.roots, arbeit=args.arbeit)
    except (BundleFehler, SigniereFehler, bundles.ManifestError, OSError) as fehler:
        print(f"FEHLER: {fehler}", file=sys.stderr)
        return 1
    print(f"OK: {args.out.name} signiert und geprueft", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
