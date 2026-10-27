#!/bin/bash
# RAUC-Bundle mit der Release-Kette signieren, Debian 13 (trixie) mit rauc 1.13 (GP6, S-3): ein verity-Bundle, das mit einer
# Wegwerf-Kette gebaut ist, bekommt per gateway/release/rauc_signieren.py die Signatur der Release-Kette (ECDSA P-256, Test-
# Kette von ca.py test-kette). Das Geraet ist `rauc info --conf` mit system.conf.in (Keyring = Root-Datei, check-purpose=
# codesign, compatible). Angenommen wird nur die Signatur der richtigen Root; jede Ablehnungsart hat einen eigenen Fall mit
# Zaehler, und kein Fall darf fehlen. Der Quellbaum ist nur lesbar eingehaengt.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
# Multi-Arch-Index-Digest von debian:trixie, wie tests/test_gateway_install.sh.
DEBIAN_IMAGE="debian:trixie@sha256:913f6706df59a68922d1dd08f78c2476560a8d367897200a6005b00e5f67c2d5"
# shellcheck source=gateway_lib.sh
. "$HERE/gateway_lib.sh"
TMP="$(mktemp -d)" || exit 1
trap 'rm -rf "$TMP"' EXIT
# Bootstrap-Anker der Testhilfe (ca.pem/ca.key, wie die Fake-Bootstrap-Tests): er darf kein Bundle signieren.
make_pki "$TMP/pki" broker.test || { echo "FAIL: Test-PKI"; exit 1; }

docker run --rm -i --security-opt label=disable -e PYTHONDONTWRITEBYTECODE=1 -v "$REPO:/src:ro" -v "$TMP/pki:/pki:ro" \
  "$DEBIAN_IMAGE" bash -c 'apt-get update -qq </dev/null \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends rauc openssl squashfs-tools python3 \
         python3-cryptography </dev/null >/dev/null \
    && rauc --version && exec python3 -' <<'PYEOF' || { echo "FAIL: RAUC-Bundle mit der Release-Kette"; exit 1; }
import collections
import contextlib
import io
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path[:0] = ["/src/gateway/release", "/src/gateway/host"]
import ca
import rauc_signieren
import signieren
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from smartheat_host import kette

RAUC = ["rauc"]
W = Path("/work")
W.mkdir()
JETZT = datetime.now(UTC).replace(microsecond=0)
MANIFEST = b'{"version": "1.0.0"}\n'
ZAEHLER: collections.Counter = collections.Counter()
FEHLER: list[str] = []


def lauf(*args):
    return subprocess.run([str(a) for a in args], capture_output=True, text=True)


def fall(name: str, bedingung: bool, detail: str = "") -> None:
    """Ein Fall genau einmal: der Zaehler beweist, dass die Zusicherung gelaufen ist. Bei Ablehnungen steht die letzte
    Zeile der Meldung von rauc dabei."""
    ZAEHLER[name] += 1
    letzte = detail.strip().splitlines()[-1][:150] if detail.strip() and name.startswith("abgelehnt") else ""
    print(("ok   " if bedingung else "FAIL ") + name + (f"  [{letzte}]" if bedingung and letzte else "")
          + ("" if bedingung else f"\n{detail}"))
    if not bedingung:
        FEHLER.append(name)


def konf(roots: Path, name: str) -> Path:
    pfad = W / f"{name}.conf"
    pfad.write_text(rauc_signieren.SYSTEM_CONF_IN.read_text().replace(rauc_signieren.KEYRING_ZEILE, f"path={roots}"))
    return pfad


def info(bundle: Path, roots: Path):
    """Die Entscheidung des Geraets: Konfiguration aus system.conf.in, Keyring = roots."""
    return lauf(*RAUC, "--conf", konf(roots, "geraet"), "info", bundle)


def angenommen(r) -> bool:
    return r.returncode == 0 and "Compatible:     'smartheat-gateway-pi5'" in r.stdout


def abgelehnt(r, muster: str) -> bool:
    """rauc info scheitert aus dem erwarteten Grund (Muster in der Meldung) und nennt kein Bundle."""
    return r.returncode != 0 and "Compatible:" not in r.stdout and muster in r.stdout + r.stderr


def schluessel(pfad: Path, key) -> None:
    pfad.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))


def zertifikat(cn, key, aussteller_cert, aussteller_key, *, ca_flag=False, ku_ca=False, eku=True,
               von=-10, bis=365):
    """Zertifikat wie die Release-Kette (P-256, ecdsa-with-SHA256); die Abweichungen sind die Faelle."""
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    usage = x509.KeyUsage(digital_signature=not ku_ca, content_commitment=False, key_encipherment=False,
                          data_encipherment=False, key_agreement=False, key_cert_sign=ku_ca, crl_sign=False,
                          encipher_only=False, decipher_only=False)
    bauer = (x509.CertificateBuilder().subject_name(name)
             .issuer_name(aussteller_cert.subject if aussteller_cert else name).public_key(key.public_key())
             .serial_number(x509.random_serial_number())
             .not_valid_before(JETZT + timedelta(days=von)).not_valid_after(JETZT + timedelta(days=bis))
             .add_extension(x509.BasicConstraints(ca=ca_flag, path_length=None), critical=True)
             .add_extension(usage, critical=True))
    if eku:
        bauer = bauer.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=False)
    return bauer.sign(aussteller_key or key, hashes.SHA256())


def variante(name: str, *, aussteller=None, **abweichung):
    """Eigene Root (oder `aussteller` = (Zertifikat, Schluessel)) und ein Blatt mit `abweichung`; liefert
    (root.pem, blatt.crt, blatt.key)."""
    ordner = W / name
    ordner.mkdir()
    root_key = ec.generate_private_key(ec.SECP256R1())
    root = zertifikat(ca.TEST_ROOT_CN, root_key, None, None, ca_flag=True, ku_ca=True, eku=False, bis=3650)
    blatt_key = ec.generate_private_key(ec.SECP256R1())
    aus_cert, aus_key = aussteller or (root, root_key)
    blatt = zertifikat(ca.BLATT_CN, blatt_key, aus_cert, aus_key, **abweichung)
    (ordner / "root.pem").write_bytes(root.public_bytes(serialization.Encoding.PEM))
    (ordner / "blatt.crt").write_bytes(blatt.public_bytes(serialization.Encoding.PEM))
    schluessel(ordner / "blatt.key", blatt_key)
    return ordner / "root.pem", ordner / "blatt.crt", ordner / "blatt.key"


# --- Ausgangslage: Test-Kette der Release (ca.py), Wegwerf-Kette fuer `roh`, winziges verity-Bundle --------------------
for ordner in ("real", "wegwerf", "fremd"):
    assert subprocess.run([sys.executable, "/src/gateway/release/ca.py", "test-kette", "--aus", str(W / ordner)],
                          capture_output=True).returncode == 0
REAL, WEGWERF, FREMD = W / "real", W / "wegwerf", W / "fremd"
inhalt = W / "inhalt"
inhalt.mkdir()
(inhalt / "rootfs.img").write_bytes(os.urandom(65536))
(inhalt / "boot.img").write_bytes(os.urandom(65536))
(inhalt / "manifest.raucm").write_text("[update]\ncompatible=smartheat-gateway-pi5\nversion=1.0.0\n\n[bundle]\n"
                                       "format=verity\n\n[image.rootfs]\nfilename=rootfs.img\n\n[image.boot]\n"
                                       "filename=boot.img\n")
ROH = W / "roh.raucb"
bau = lauf(*RAUC, "bundle", "--cert", WEGWERF / "release.crt", "--key", WEGWERF / "release.key", inhalt, ROH)
assert bau.returncode == 0, bau.stderr


def signiere(name: str, cert: Path, key: Path, roots: Path, wegwerf_ca: Path = WEGWERF / "root.crt") -> Path:
    ziel = W / f"{name}.raucb"
    rauc_signieren.signiere_bundle(ROH, ziel, cert, str(key), roots=roots, wegwerf_ca=wegwerf_ca, rauc=RAUC)
    return ziel


def lehnt_ab(name: str, cert: Path, key: Path, roots: Path) -> None:
    """Auch das Werkzeug selbst liefert dieses Bundle nicht aus: Fehler und kein Ziel."""
    ziel = W / f"{name}.raucb"
    meldung = io.StringIO()
    try:
        with contextlib.redirect_stderr(meldung):
            signiere(name, cert, key, roots)
        fall(f"signieren-lehnt-ab:{name}", False, "signiere_bundle hat das Bundle ausgeliefert")
    except signieren.SigniereFehler:
        fall(f"signieren-lehnt-ab:{name}", not ziel.exists() and not Path(f"{ziel}.neu").exists(), meldung.getvalue())


def angreifer(name: str, cert: Path, key: Path) -> Path:
    """Ein Bundle, das ein Angreifer mit diesem Zertifikat baut: das Manifest von `roh` neu signiert und die Signatur von
    Hand ans Bundle gehaengt (Bundle = Nutzdaten + CMS-Signatur + 8 Byte Laenge der Signatur, big endian). Ohne jede
    Pruefung durch signiere_bundle oder rauc replace-signature: es entscheidet allein das Geraet."""
    sig_alt, manifest, sig_neu, ziel = (W / f"{name}.{endung}" for endung in ("cms", "raucm", "sig", "raucb"))
    roh = ROH.read_bytes()
    laenge = int.from_bytes(roh[-8:], "big")
    sig_alt.write_bytes(roh[-8 - laenge:-8])
    for schritt in (
        ["openssl", "cms", "-verify", "-noverify", "-inform", "DER", "-in", sig_alt, "-out", manifest],
        ["openssl", "cms", "-sign", "-binary", "-nodetach", "-outform", "DER", "-md", "sha256", "-nosmimecap",
         "-signer", cert, "-inkey", key, "-in", manifest, "-out", sig_neu],
    ):
        ergebnis = lauf(*schritt)
        assert ergebnis.returncode == 0, ergebnis.stderr
    neu = sig_neu.read_bytes()
    ziel.write_bytes(roh[:-8 - laenge] + neu + len(neu).to_bytes(8, "big"))
    return ziel


# --- angenommen -------------------------------------------------------------------------------------------------------
ROOTS = REAL / "root.crt"
GUT = signiere("gut", REAL / "release.crt", REAL / "release.key", ROOTS)
r = info(GUT, ROOTS)
fall("angenommen:richtige-root", angenommen(r) and "Setwell Gateway Release" in r.stdout, r.stdout + r.stderr)
b = angreifer("handgebaut", REAL / "release.crt", REAL / "release.key")
r = info(b, ROOTS)  # Kontrolle des Werkzeugs im Test: mit der richtigen Kette nimmt das Geraet das handgebaute Bundle an
fall("kontrolle:handgebaut-mit-richtiger-kette-angenommen", angenommen(r), r.stdout + r.stderr)
r = info(ROH, ROOTS)
fall("abgelehnt:nur-wegwerf-signatur", abgelehnt(r, "certificate signature failure"), r.stdout + r.stderr)  # "unsigniert" fuer das Geraet

# --- abgelehnt --------------------------------------------------------------------------------------------------------
b = angreifer("fremde-root", FREMD / "release.crt", FREMD / "release.key")
r = info(b, ROOTS)
fall("abgelehnt:fremde-root", abgelehnt(r, "certificate signature failure"), r.stdout + r.stderr)
lehnt_ab("fremde-root", FREMD / "release.crt", FREMD / "release.key", ROOTS)

root_c, cert_c, key_c = variante("ohne-codesigning", eku=False)
b = angreifer("ohne-codesigning", cert_c, key_c)
r = lauf(*RAUC, "--keyring", root_c, "info", b)  # ohne Konfiguration gibt es kein check-purpose: RAUC nimmt an
fall("kontrolle:ohne-codesigning-ohne-conf-angenommen", angenommen(r), r.stdout + r.stderr)
r = info(b, root_c)
fall("abgelehnt:ohne-codesigning", abgelehnt(r, "unsuitable certificate purpose"), r.stdout + r.stderr)
lehnt_ab("ohne-codesigning", cert_c, key_c, root_c)

root_a, cert_a, key_a = variante("abgelaufen", von=-10, bis=-1)
b = angreifer("abgelaufen", cert_a, key_a)
r = info(b, root_a)
fall("abgelehnt:abgelaufen", abgelehnt(r, "certificate has expired"), r.stdout + r.stderr)
lehnt_ab("abgelaufen", cert_a, key_a, root_a)

anker_cert = x509.load_pem_x509_certificate(Path("/pki/ca.pem").read_bytes())
anker_key = serialization.load_pem_private_key(Path("/pki/ca.key").read_bytes(), None)
_, cert_b, key_b = variante("bootstrap-anker", aussteller=(anker_cert, anker_key))
b = angreifer("bootstrap-anker", cert_b, key_b)  # codeSigning-Blatt des Bootstrap-Ankers: nur die Root unterscheidet es
r = info(b, ROOTS)
fall("abgelehnt:bootstrap-anker", abgelehnt(r, "unable to get local issuer certificate"), r.stdout + r.stderr)
lehnt_ab("bootstrap-anker", cert_b, key_b, ROOTS)

PLATZHALTER = Path("/src/gateway/host/release-roots.pem")
fall("platzhalter-ist-der-aus-dem-repo", "SMARTHEAT-PLATZHALTER" in PLATZHALTER.read_text())
r = info(GUT, PLATZHALTER)
fall("abgelehnt:platzhalter-schluesselbund", abgelehnt(r, "failed to load CA file"), r.stdout + r.stderr)
lehnt_ab("platzhalter", REAL / "release.crt", REAL / "release.key", PLATZHALTER)

r = info(GUT, W / "gibt-es-nicht.pem")
fall("abgelehnt:ohne-root-datei", abgelehnt(r, "failed to load CA file"), r.stdout + r.stderr)

abgeschnitten = W / "abgeschnitten.raucb"
abgeschnitten.write_bytes(GUT.read_bytes()[:-64])
r = info(abgeschnitten, ROOTS)
fall("abgelehnt:abgeschnittene-signatur", abgelehnt(r, "Invalid bundle format"), r.stdout + r.stderr)

daten = bytearray(GUT.read_bytes())
daten[-200] ^= 0xFF  # ein Byte in der Signatur (CMS liegt am Ende des Bundles)
veraendert = W / "veraendert.raucb"
veraendert.write_bytes(daten)
r = info(veraendert, ROOTS)
fall("abgelehnt:veraenderte-signatur", abgelehnt(r, "Invalid bundle format"), r.stdout + r.stderr)

# --- Blatt mit CA:TRUE: RAUC nimmt es an, die Barriere ist das Manifest (kette.verify) --------------------------------
root_t, cert_t, key_t = variante("ca-true", ca_flag=True)
b = signiere("ca-true", cert_t, key_t, root_t)
r = info(b, root_t)
fall("rauc-nimmt-blatt-mit-ca-true-an", angenommen(r), r.stdout + r.stderr)
text = signieren.signiere(MANIFEST, cert_t.read_text(), str(key_t))
try:
    kette.verify(kette.load_roots(root_t.read_text()), MANIFEST, text, datetime.now(UTC))
    fall("kette-verify-lehnt-blatt-mit-ca-true-ab", False, "kette.verify hat das Manifest angenommen")
except kette.SignatureError as fehler:
    fall("kette-verify-lehnt-blatt-mit-ca-true-ab", "CA-Zertifikat" in str(fehler), str(fehler))
text = signieren.signiere(MANIFEST, (REAL / "release.crt").read_text(), str(REAL / "release.key"))
kette.verify(kette.load_roots(ROOTS.read_text()), MANIFEST, text, datetime.now(UTC))  # Gegenprobe: das echte Blatt gilt
fall("kette-verify-nimmt-das-echte-blatt-an", True)

# --- Abschluss: jeder Fall genau einmal gelaufen -----------------------------------------------------------------------
ERWARTET = {"angenommen:richtige-root", "kontrolle:handgebaut-mit-richtiger-kette-angenommen", "abgelehnt:nur-wegwerf-signatur", "abgelehnt:fremde-root",
            "signieren-lehnt-ab:fremde-root", "kontrolle:ohne-codesigning-ohne-conf-angenommen",
            "abgelehnt:ohne-codesigning", "signieren-lehnt-ab:ohne-codesigning", "abgelehnt:abgelaufen",
            "signieren-lehnt-ab:abgelaufen", "abgelehnt:bootstrap-anker", "signieren-lehnt-ab:bootstrap-anker",
            "platzhalter-ist-der-aus-dem-repo", "abgelehnt:platzhalter-schluesselbund",
            "signieren-lehnt-ab:platzhalter", "abgelehnt:ohne-root-datei", "abgelehnt:abgeschnittene-signatur",
            "abgelehnt:veraenderte-signatur", "rauc-nimmt-blatt-mit-ca-true-an",
            "kette-verify-lehnt-blatt-mit-ca-true-ab", "kette-verify-nimmt-das-echte-blatt-an"}
if set(ZAEHLER) != ERWARTET or any(n != 1 for n in ZAEHLER.values()):
    print("FAIL Faelle:", sorted(set(ZAEHLER) ^ ERWARTET), {k: n for k, n in ZAEHLER.items() if n != 1})
    sys.exit(1)
if FEHLER:
    print("FAIL:", FEHLER)
    sys.exit(1)
print(f"{len(ZAEHLER)} Faelle")
PYEOF
echo "PASS: RAUC-Bundle mit der Release-Kette signiert (rauc 1.13, Debian 13)"
