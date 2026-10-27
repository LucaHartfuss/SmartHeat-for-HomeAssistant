#!/usr/bin/env python3
"""Signiert ein RAUC-Bundle (Format verity) am Betreiberrechner mit der Release-Kette (Sicherheit, S-3, S-10): dieselbe
X.509-Kette (ECDSA P-256) und derselbe Schluessel wie das Manifest (signieren.py), damit das Geraet beides gegen eine
einzige Root prueft. Das Werkzeug `rauc bundle` signiert nicht mit dem Hardware-Token, daher der Weg ueber externes
Signieren (RAUC-Referenz "External Signing and PKI"; RAUC 1.13: src/main.c, src/bundle.c):

  1. `roh` ist mit einer Wegwerf-Kette gebaut (rauc bundle). `rauc extract-signature` liest die Signatur (CMS, DER) und
     prueft sie gegen `wegwerf_ca`.
  2. `openssl cms -verify -noverify` holt das signierte Manifest (manifest.raucm) aus der Signatur.
  3. `openssl cms -sign -nodetach` signiert dasselbe Manifest neu mit dem Release-Zertifikat und -Schluessel; der Schluessel
     ist eine PEM-Datei oder eine `pkcs11:`-URI (dann mit `-provider pkcs11 -provider default`, wie in signieren.py).
  4. `rauc replace-signature` setzt die neue Signatur ein und prueft sie gegen `roots` (--signing-keyring).
  5. `rauc info` prueft das fertige Bundle mit der Konfiguration des Geraets (system.conf.in, Keyring = `roots`, also
     check-purpose=codesign und compatible), erst dann liegt `ziel` da.

Alle Schritte laufen mit derselben Konfiguration (--conf), weil RAUC ohne sie das Zertifikat-Zweck-Pruefmuster von OpenSSL
(smimesign) anlegt und ein reines Code-Signing-Blatt ablehnt ("unsuitable certificate purpose"): auch das Wegwerf-Blatt muss
daher codeSigning tragen. Der Schluessel erscheint nie in Ausgabe oder Fehlermeldung: Meldungen der Programme gibt das
Werkzeug bei einem Fehler erst nach dem Lauf weiter, den Schluessel (Pfad oder URI) und jeden pin-value geschwaerzt. Bei jedem Fehler bleibt
kein `ziel` liegen."""
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from signieren import PKCS11, SigniereFehler, geschwaerzt

SYSTEM_CONF_IN = Path(__file__).resolve().parents[1] / "host" / "rauc" / "system.conf.in"
KEYRING_ZEILE = "path=/opt/smartheat/host/release-roots.pem"


def _conf(roots: Path) -> str:
    """system.conf.in mit `roots` statt des Keyring-Pfads im Image."""
    text = SYSTEM_CONF_IN.read_text()
    if text.count(KEYRING_ZEILE) != 1 or "\n" in str(roots):
        raise SigniereFehler("system.conf.in: Keyring-Zeile fehlt oder Pfad unbrauchbar")
    return text.replace(KEYRING_ZEILE, f"path={roots}")


def _lauf(befehl: Sequence[str], schluessel: str, name: str) -> None:
    """Fuehrt einen Schritt aus; scheitert er, gehen die Meldungen des Programms geschwaerzt nach stderr (rauc meldet auch
    im Erfolgsfall viel)."""
    try:
        ergebnis = subprocess.run(list(befehl), capture_output=True, check=False)
    except OSError:
        raise SigniereFehler(f"{name}: Programm nicht aufrufbar") from None
    if ergebnis.returncode != 0:
        meldung = geschwaerzt(ergebnis.stderr.decode(errors="replace"), schluessel)
        if meldung:
            print(meldung, end="" if meldung.endswith("\n") else "\n", file=sys.stderr)
        raise SigniereFehler(f"{name} fehlgeschlagen (Exit {ergebnis.returncode})")


def signiere_bundle(roh: Path, ziel: Path, zertifikat_pem: Path, schluessel: str, *, roots: Path, wegwerf_ca: Path,
                    rauc: Sequence[str]) -> None:
    """Ersetzt die Wegwerf-Signatur von `roh` durch die der Release-Kette und schreibt das Ergebnis nach `ziel`.
    `rauc` ist die Befehlszeile des Programms (etwa ["rauc"]). Wirft SigniereFehler; dann liegt kein `ziel` da."""
    neu = ziel.with_name(ziel.name + ".neu")
    ziel.unlink(missing_ok=True)
    neu.unlink(missing_ok=True)
    try:
        if not roh.is_file() or not zertifikat_pem.is_file():
            raise SigniereFehler("Bundle oder Zertifikat fehlt")
        if not schluessel.startswith(PKCS11) and not os.path.isfile(schluessel):
            raise SigniereFehler("Schluesseldatei fehlt")
        provider = ["-provider", "pkcs11", "-provider", "default"] if schluessel.startswith(PKCS11) else []
        with tempfile.TemporaryDirectory(prefix="rauc-signieren-") as arbeit:
            sig_alt, manifest, sig_neu, conf = (Path(arbeit) / n for n in
                                                ("sig.cms", "manifest.raucm", "sig.neu", "system.conf"))
            conf.write_text(_conf(roots))
            basis = [*rauc, "--conf", str(conf)]
            _lauf([*basis, "--keyring", str(wegwerf_ca), "extract-signature", str(roh), str(sig_alt)], schluessel,
                  "rauc extract-signature")
            _lauf(["openssl", "cms", "-verify", "-noverify", "-inform", "DER", "-in", str(sig_alt), "-out", str(manifest)],
                  schluessel, "openssl cms -verify")
            _lauf(["openssl", "cms", "-sign", *provider, "-binary", "-nodetach", "-outform", "DER", "-md", "sha256",
                   "-nosmimecap", "-signer", str(zertifikat_pem), "-inkey", schluessel, "-in", str(manifest),
                   "-out", str(sig_neu)], schluessel, "openssl cms -sign")
            _lauf([*basis, "--keyring", str(wegwerf_ca), "replace-signature", "--signing-keyring", str(roots), str(roh),
                   str(sig_neu), str(neu)], schluessel, "rauc replace-signature")
            _lauf([*basis, "info", str(neu)], schluessel, "rauc info")
        neu.chmod(0o644)
        neu.replace(ziel)
    finally:
        neu.unlink(missing_ok=True)
