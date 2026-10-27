import configparser
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

import rauc_signieren
from signieren import SigniereFehler

URI = "pkcs11:token=Setwell%20Release;object=release-key;type=private?pin-value=GEHEIME-PIN-4711"
SCHRITTE = ["extract-signature", "verify", "sign", "replace-signature", "info"]

ATTRAPPE = f'''#!{sys.executable}
import json, os, sys
argv = sys.argv[1:]
conf = None
if "--conf" in argv:
    conf = open(argv[argv.index("--conf") + 1]).read()
with open(os.environ["ATTRAPPE_LOG"], "a") as protokoll:
    protokoll.write(json.dumps({{"prog": os.path.basename(sys.argv[0]), "argv": argv, "conf": conf}}) + "\\n")
if "extract-signature" in argv:
    schritt = "extract-signature"
elif "replace-signature" in argv:
    schritt = "replace-signature"
elif "info" in argv:
    schritt = "info"
elif "-verify" in argv:
    schritt = "verify"
else:
    schritt = "sign"
if os.environ.get("ATTRAPPE_FEHLER") == schritt:
    sys.stderr.write(os.environ.get("ATTRAPPE_MELDUNG", "Fehler") + "\\n")
    sys.exit(1)
if schritt in ("extract-signature", "replace-signature"):
    Path = argv[-1]
    open(Path, "wb").write(b"cms" if schritt == "extract-signature" else b"BUNDLE-NEU")
elif schritt in ("verify", "sign"):
    open(argv[argv.index("-out") + 1], "wb").write(b"manifest" if schritt == "verify" else b"sig-neu")
'''


@pytest.fixture
def umgebung(tmp_path, monkeypatch):
    """Attrappen `rauc` (Befehlszeile) und `openssl` (im PATH), die ihre Aufrufe protokollieren, die Ausgabedatei des
    Schritts schreiben und beim Schritt ATTRAPPE_FEHLER scheitern; Arbeitsordner der Signierung in tmp_path/tmp."""
    bin_dir, arbeit = tmp_path / "bin", tmp_path / "tmp"
    bin_dir.mkdir()
    arbeit.mkdir()
    for name in ("rauc", "openssl"):
        (bin_dir / name).write_text(ATTRAPPE)
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("ATTRAPPE_LOG", str(tmp_path / "aufrufe.log"))
    monkeypatch.delenv("ATTRAPPE_FEHLER", raising=False)
    monkeypatch.delenv("ATTRAPPE_MELDUNG", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(arbeit))
    for name, inhalt in (("roh.raucb", b"ROH"), ("release.crt", b"cert"), ("release.key", b"key"),
                         ("wegwerf-root.pem", b"w"), ("roots.pem", b"r")):
        (tmp_path / name).write_bytes(inhalt)

    class Umgebung:
        roh, zertifikat, schluessel = tmp_path / "roh.raucb", tmp_path / "release.crt", str(tmp_path / "release.key")
        wegwerf, roots, ziel, tmp = tmp_path / "wegwerf-root.pem", tmp_path / "roots.pem", tmp_path / "ziel.raucb", arbeit
        rauc = [str(bin_dir / "rauc")]

        @staticmethod
        def aufrufe() -> list[dict]:
            pfad = tmp_path / "aufrufe.log"
            return [json.loads(zeile) for zeile in pfad.read_text().splitlines()] if pfad.exists() else []

        @classmethod
        def signiere(cls, schluessel: str | None = None) -> None:
            rauc_signieren.signiere_bundle(cls.roh, cls.ziel, cls.zertifikat, schluessel or cls.schluessel,
                                           roots=cls.roots, wegwerf_ca=cls.wegwerf, rauc=cls.rauc)

    return Umgebung


def _roh_argv(aufruf: dict, u) -> list[str]:
    """Argumente ohne den (zufaelligen) Arbeitsordner."""
    return [a.replace(os.path.dirname(a), "<arbeit>") if a.startswith(str(u.tmp)) else a for a in aufruf["argv"]]


def test_reihenfolge_und_argumente_mit_pem_schluessel(umgebung):
    u = umgebung
    u.signiere()
    aufrufe = u.aufrufe()
    assert [a["prog"] for a in aufrufe] == ["rauc", "openssl", "openssl", "rauc", "rauc"]
    conf = "<arbeit>/system.conf"
    assert [_roh_argv(a, u) for a in aufrufe] == [
        ["--conf", conf, "--keyring", str(u.wegwerf), "extract-signature", str(u.roh), "<arbeit>/sig.cms"],
        ["cms", "-verify", "-noverify", "-inform", "DER", "-in", "<arbeit>/sig.cms", "-out", "<arbeit>/manifest.raucm"],
        ["cms", "-sign", "-binary", "-nodetach", "-outform", "DER", "-md", "sha256", "-nosmimecap", "-signer",
         str(u.zertifikat), "-inkey", u.schluessel, "-in", "<arbeit>/manifest.raucm", "-out", "<arbeit>/sig.neu"],
        ["--conf", conf, "--keyring", str(u.wegwerf), "replace-signature", "--signing-keyring", str(u.roots), str(u.roh),
         "<arbeit>/sig.neu", f"{u.ziel}.neu"],
        ["--conf", conf, "info", f"{u.ziel}.neu"],
    ]


def test_konfiguration_ist_die_des_geraets_mit_den_roots_als_keyring(umgebung):
    u = umgebung
    u.signiere()
    erwartet = rauc_signieren.SYSTEM_CONF_IN.read_text().replace("path=/opt/smartheat/host/release-roots.pem",
                                                                  f"path={u.roots}")
    assert erwartet != rauc_signieren.SYSTEM_CONF_IN.read_text()
    # dieselbe Konfiguration in jedem rauc-Schritt (check-purpose=codesign, compatible), kein anderer Keyring
    konfigurationen = [a["conf"] for a in u.aufrufe() if a["prog"] == "rauc"]
    assert konfigurationen == [erwartet] * 3
    assert "check-purpose=codesign" in erwartet and "compatible=smartheat-gateway-pi5" in erwartet


def test_ergebnis_liegt_unter_ziel_ohne_reste(umgebung, capfd):
    u = umgebung
    u.signiere()
    assert capfd.readouterr() == ("", "")  # im Erfolgsfall bleibt die Ausgabe leer
    assert u.ziel.read_bytes() == b"BUNDLE-NEU"
    assert stat.S_IMODE(u.ziel.stat().st_mode) == 0o644
    assert sorted(p.name for p in u.ziel.parent.iterdir() if p.name.startswith("ziel")) == ["ziel.raucb"]
    assert list(u.tmp.iterdir()) == []  # die neue Signatur und das Manifest liegen nicht herum


@pytest.mark.parametrize("schritt", SCHRITTE)
def test_fehler_in_jedem_schritt_laesst_kein_ziel_zurueck(umgebung, monkeypatch, schritt):
    u = umgebung
    u.ziel.write_bytes(b"ALT")  # ein Bundle eines frueheren Laufs darf nach einem Fehler nicht als Ergebnis gelten
    monkeypatch.setenv("ATTRAPPE_FEHLER", schritt)
    with pytest.raises(SigniereFehler):
        u.signiere()
    assert not u.ziel.exists() and not Path(f"{u.ziel}.neu").exists()
    assert len(u.aufrufe()) == SCHRITTE.index(schritt) + 1  # nach dem Fehler laeuft nichts weiter
    assert list(u.tmp.iterdir()) == []


def test_fehlende_eingaben_scheitern_ohne_aufruf(umgebung):
    u = umgebung
    for fehlt in (u.roh, u.zertifikat, Path(u.schluessel)):
        inhalt = fehlt.read_bytes()
        fehlt.unlink()
        with pytest.raises(SigniereFehler):
            u.signiere()
        assert u.aufrufe() == [] and not u.ziel.exists()
        fehlt.write_bytes(inhalt)


def test_programm_nicht_aufrufbar(umgebung):
    u = umgebung
    with pytest.raises(SigniereFehler):
        rauc_signieren.signiere_bundle(u.roh, u.ziel, u.zertifikat, u.schluessel, roots=u.roots, wegwerf_ca=u.wegwerf,
                                       rauc=["/gibt/es/nicht"])
    assert not u.ziel.exists()


def test_konfigurationsvorlage_ohne_keyring_zeile_scheitert(umgebung, monkeypatch, tmp_path):
    u = umgebung
    vorlage = tmp_path / "system.conf.in"
    vorlage.write_text("[keyring]\npath=/anderswo.pem\n")
    monkeypatch.setattr(rauc_signieren, "SYSTEM_CONF_IN", vorlage)
    with pytest.raises(SigniereFehler):
        u.signiere()
    assert u.aufrufe() == [] and not u.ziel.exists()


def test_pkcs11_uri_geht_mit_provider_an_openssl(umgebung):
    u = umgebung
    u.signiere(URI)
    sign = next(a["argv"] for a in u.aufrufe() if a["prog"] == "openssl" and "-sign" in a["argv"])
    assert sign[:6] == ["cms", "-sign", "-provider", "pkcs11", "-provider", "default"]
    assert sign[sign.index("-inkey") + 1] == URI
    ohne = [a["argv"] for a in u.aufrufe() if a["prog"] != "openssl" or "-sign" not in a["argv"]]
    assert all("-provider" not in a for a in ohne)


def test_pem_schluessel_ohne_provider(umgebung):
    u = umgebung
    u.signiere()
    assert all("-provider" not in a["argv"] for a in u.aufrufe())


@pytest.mark.parametrize("schritt", ["sign", "info"])
def test_uri_und_pin_erscheinen_nie_in_ausgabe_oder_fehler(umgebung, monkeypatch, capfd, schritt):
    u = umgebung
    monkeypatch.setenv("ATTRAPPE_FEHLER", schritt)
    monkeypatch.setenv("ATTRAPPE_MELDUNG", f"kann {URI} nicht lesen (pin-value=GEHEIME-PIN-4711;x=1)")
    with pytest.raises(SigniereFehler) as info:
        u.signiere(URI)
    ausgabe = capfd.readouterr()
    for geheim in ("GEHEIME-PIN", "pkcs11:", "Setwell%20Release", "release-key"):
        assert geheim not in str(info.value) + ausgabe.out + ausgabe.err, geheim
    assert "[Schluessel]" in ausgabe.err and "kann" in ausgabe.err  # Gegenprobe: die Meldung kommt geschwaerzt durch


def test_pin_value_wird_auch_ohne_die_uri_geschwaerzt(umgebung, monkeypatch, capfd):
    u = umgebung
    monkeypatch.setenv("ATTRAPPE_FEHLER", "sign")
    monkeypatch.setenv("ATTRAPPE_MELDUNG", "Token: pin-value=GEHEIME-PIN-4711 abgelehnt")
    with pytest.raises(SigniereFehler):
        u.signiere(URI)
    fehler = capfd.readouterr().err
    assert "GEHEIME-PIN" not in fehler and "pin-value=[entfernt]" in fehler


def test_systemkonfiguration_des_geraets_hat_die_vorgaben():
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str  # pyright: ignore[reportAttributeAccessIssue]  RAUC-Schluessel unterscheiden Gross/Klein
    parser.read_string(rauc_signieren.SYSTEM_CONF_IN.read_text())
    assert dict(parser["system"]) == {"compatible": "smartheat-gateway-pi5", "bootloader": "custom",
                                      "activate-installed": "false", "data-directory": "/var/lib/rauc",
                                      "bundle-formats": "-plain"}
    assert dict(parser["keyring"]) == {"path": "/opt/smartheat/host/release-roots.pem", "check-purpose": "codesign"}
    assert dict(parser["handlers"]) == {"bootloader-custom-backend": "/opt/smartheat/rauc-backend"}
    assert dict(parser["slot.rootfs.0"]) == {"device": "/dev/disk/by-partlabel/system_a", "type": "ext4", "bootname": "A"}
    assert dict(parser["slot.rootfs.1"]) == {"device": "/dev/disk/by-partlabel/system_b", "type": "ext4", "bootname": "B"}
    assert dict(parser["slot.boot.0"]) == {"device": "/dev/disk/by-partlabel/boot_a", "type": "vfat", "parent": "rootfs.0"}
    assert dict(parser["slot.boot.1"]) == {"device": "/dev/disk/by-partlabel/boot_b", "type": "vfat", "parent": "rootfs.1"}
    assert set(parser.sections()) == {"system", "keyring", "handlers", "slot.rootfs.0", "slot.rootfs.1", "slot.boot.0",
                                      "slot.boot.1"}
