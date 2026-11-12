#!/usr/bin/env python3
"""Signiert das Manifest eines Bundles am Betreiberrechner (Sicherheit, S-3, S-10) und prueft die Signatur sofort mit dem
Code des Geraets (smartheat_host.kette). Der Schluessel ist entweder eine PEM-Datei (Test-Kette, Labor) oder eine
`pkcs11:`-URI (Hardware-Token); signiert wird immer mit `openssl dgst -sha256 -sign`, bei `pkcs11:` mit
`-provider pkcs11 -provider default` (pkcs11-provider von OpenSSL 3, z. B. Paket pkcs11-provider; der Provider liest die
Token-Einstellungen aus seiner Konfiguration, etwa PKCS11_PROVIDER_MODULE). Die PIN gehoert nicht in die URI (Prozessliste):
openssl fragt sie am Terminal, oder die URI nennt `pin-source=file:<Pfad>`.

Der Signaturtext (manifest.json.sig) ist das JSON smartheat-kette-1: Format, Zertifikat (PEM) und die DER-ECDSA-Signatur
(Base64) ueber die Bytes von manifest.json. Der Schluessel erscheint nie in Ausgabe, Fehlermeldung oder Datei: die Meldungen
von openssl gibt das Werkzeug erst nach dem Lauf weiter, den Schluessel (Pfad oder URI) und jeden pin-value darin
geschwaerzt. Die PIN-Abfrage liest openssl am Terminal (/dev/tty), nicht ueber stderr.

Vor dem Signieren setzt das Werkzeug `ablauf` im Manifest neu: zwoelf Kalendermonate ab jetzt, nie nach dem Ende des
Zertifikats (ein Manifest ueberlebt sein Zertifikat nicht); alles andere im Manifest, auch `art`, bleibt. Die Signatur
deckt die neu geschriebenen Bytes, die das Werkzeug ins Bundle zurueckschreibt, erst nach bestandener Pruefung. Mit --aus
entsteht nur eine weitere Signatur ueber das unveraenderte Manifest (etwa mit einer fremden Kette fuer Ablehnungsproben).

Aufruf: signieren.py BUNDLE_DIR --zertifikat DATEI --schluessel PEM|pkcs11:URI --roots DATEI [--aus DATEI]
Exit 0 = signiert und geprueft, 1 = Fehler (es bleibt keine Signatur liegen), 2 = falscher Aufruf."""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))  # Aufruf als Skript ohne PYTHONPATH
from build_bundle import ABLAUF_MONATE, ablauf_text, manifest_bytes  # noqa: E402
from kalender import plus_monate  # noqa: E402
from smartheat_host import bundles, kette  # noqa: E402

FORMAT = "smartheat-kette-1"
PKCS11 = "pkcs11:"


class SigniereFehler(Exception):
    pass


def geschwaerzt(text: str, schluessel: str) -> str:
    """Meldung von openssl ohne den Schluessel (Pfad oder URI) und ohne jeden pin-value."""
    return re.sub(r"pin-value=[^;&?\s]*", "pin-value=[entfernt]", text.replace(schluessel, "[Schluessel]"))


def ablauf_setzen(manifest: bytes, zertifikat_pem: str, jetzt: datetime) -> bytes:
    """Das Manifest mit neuem `ablauf`: `jetzt` plus zwoelf Kalendermonate, hoechstens bis zum Ende des Zertifikats, auf
    Sekunden genau. Gleiche Serialisierung wie build_bundle.py. Ein Zertifikat, das `jetzt` schon beendet ist, signiert nicht."""
    try:
        daten = json.loads(manifest)
        bundles.parse_manifest(manifest)
    except (ValueError, bundles.ManifestError) as fehler:
        raise SigniereFehler(f"Manifest nicht lesbar: {fehler}") from None
    try:
        ende = x509.load_pem_x509_certificate(zertifikat_pem.encode()).not_valid_after_utc
    except ValueError:
        raise SigniereFehler("Zertifikat nicht lesbar") from None
    jetzt = jetzt.astimezone(UTC).replace(microsecond=0)
    if ende <= jetzt:
        raise SigniereFehler(f"Zertifikat ist seit {ende.isoformat()} abgelaufen")
    daten["ablauf"] = ablauf_text(min(plus_monate(jetzt, ABLAUF_MONATE), ende))
    return manifest_bytes(daten)


def signiere(manifest: bytes, zertifikat_pem: str, schluessel: str) -> str:
    """Signaturtext smartheat-kette-1 fuer `manifest`. `schluessel` ist ein PEM-Pfad oder eine `pkcs11:`-URI."""
    befehl = ["openssl", "dgst", "-sha256"]
    if schluessel.startswith(PKCS11):
        befehl += ["-provider", "pkcs11", "-provider", "default"]
    elif not os.path.isfile(schluessel):
        raise SigniereFehler("Schluesseldatei fehlt")
    befehl += ["-sign", schluessel]
    try:
        ergebnis = subprocess.run(befehl, input=manifest, capture_output=True, check=False)
    except OSError:
        raise SigniereFehler("openssl nicht aufrufbar") from None
    meldung = geschwaerzt(ergebnis.stderr.decode(errors="replace"), schluessel)
    if meldung:
        print(meldung, end="" if meldung.endswith("\n") else "\n", file=sys.stderr)
    if ergebnis.returncode != 0 or not ergebnis.stdout:
        raise SigniereFehler(f"openssl hat nicht signiert (Exit {ergebnis.returncode})")
    return json.dumps({"format": FORMAT, "zertifikat": zertifikat_pem,
                       "signatur": base64.b64encode(ergebnis.stdout).decode()})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle", type=Path, metavar="BUNDLE_DIR")
    parser.add_argument("--zertifikat", type=Path, required=True)
    parser.add_argument("--schluessel", required=True, metavar="PEM|pkcs11:URI")
    parser.add_argument("--roots", type=Path, required=True)
    parser.add_argument("--aus", type=Path, help=f"Ausgabedatei (Standard: BUNDLE_DIR/{bundles.SIGNATURE_NAME})")
    args = parser.parse_args(argv)
    ziel = args.aus or args.bundle / bundles.SIGNATURE_NAME
    try:
        ziel.unlink(missing_ok=True)
        zertifikat = args.zertifikat.read_text()
        manifest = (args.bundle / bundles.MANIFEST_NAME).read_bytes()
        if not args.aus:  # eine zusaetzliche Signatur (--aus) deckt dieselben Bytes wie die im Bundle
            manifest = ablauf_setzen(manifest, zertifikat, datetime.now(UTC))
        text = signiere(manifest, zertifikat, args.schluessel)
        # Dieselbe Pruefung wie auf dem Geraet, vor dem Schreiben: eine Signatur, die sie nicht besteht, liegt nie herum.
        cert = kette.verify(kette.load_roots(args.roots.read_text()), manifest, text, datetime.now(UTC))
        if not args.aus:
            (args.bundle / bundles.MANIFEST_NAME).write_bytes(manifest)
        ziel.write_text(text)
        ziel.chmod(0o644)
    except (OSError, SigniereFehler) as fehler:
        print(f"FEHLER: {fehler}", file=sys.stderr)
        return 1
    except (ValueError, kette.SignatureError) as fehler:
        print(f"UNGUELTIG: {fehler}", file=sys.stderr)
        return 1
    print(f"OK: {ziel.name} signiert von {cert.subject.rfc4514_string()} (Zertifikat gueltig bis "
          f"{cert.not_valid_after_utc.isoformat()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
