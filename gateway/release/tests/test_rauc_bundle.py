"""rauc_bundle.py: Manifest, Bundle-Verzeichnis, Versionspruefung im Image und die Verdrahtung des rauc-Containers. `docker`
und `openssl` sind Attrappen im PATH (rauc_attrappe.py); die Attrappe von `docker run` fuehrt das Programm im Container nach
und prueft dabei, was der echte Container braucht: jeder Pfad, den er bekommt, liegt im gemounteten Ordner. Ob echtes rauc
das Ergebnis annimmt, zeigt tests/test_gateway_rauc.sh (Signieren) und der Lauf mit echten Images (Bericht Task 5).
"""
import configparser
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
import rauc_attrappe

import rauc_bundle
import rauc_signieren
from rauc_bundle import BundleFehler
from signieren import SigniereFehler
from smartheat_host import bundles

VERSION = "1.2.3"
URI = "pkcs11:token=Setwell%20Release;object=release-key;type=private?pin-value=GEHEIME-PIN-4711"



@pytest.fixture
def umgebung(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("docker", "openssl"):
        (bin_dir / name).write_text(rauc_attrappe.skript())
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("ATTRAPPE_LOG", str(tmp_path / "aufrufe.log"))
    monkeypatch.setenv("ATTRAPPE_IMAGE", json.dumps({rauc_bundle.IMAGE_VERSION_PY: f'GATEWAY_VERSION = "{VERSION}"\n',
                                                     rauc_bundle.IMAGE_VERSION_DATEI: f"{VERSION}\n"}))
    for name in ("ATTRAPPE_FEHLER", "ATTRAPPE_MELDUNG"):
        monkeypatch.delenv(name, raising=False)
    for name, inhalt in (("system.ext4", b"S" * 4096), ("boot.vfat", b"B" * 2048), ("release.crt", b"cert"),
                         ("release.key", b"key"), ("roots.pem", b"roots")):
        (tmp_path / name).write_bytes(inhalt)

    class Umgebung:
        system, boot, zertifikat = tmp_path / "system.ext4", tmp_path / "boot.vfat", tmp_path / "release.crt"
        schluessel, roots = str(tmp_path / "release.key"), tmp_path / "roots.pem"
        ziel, arbeit = tmp_path / "out" / "system.raucb", tmp_path / "out" / "arbeit"

        @staticmethod
        def aufrufe(prog: str | None = None) -> list[dict]:
            pfad = tmp_path / "aufrufe.log"
            alle = [json.loads(zeile) for zeile in pfad.read_text().splitlines()] if pfad.exists() else []
            return [a for a in alle if prog is None or a["prog"] == prog]

        @classmethod
        def baue(cls, schluessel: str | None = None, version: str = VERSION) -> None:
            cls.ziel.parent.mkdir(exist_ok=True)
            rauc_bundle.baue(version, cls.system, cls.boot, cls.ziel, zertifikat=cls.zertifikat,
                             schluessel=schluessel or cls.schluessel, roots=cls.roots, arbeit=cls.arbeit)

    return Umgebung


def test_manifest_des_bundles():
    text = rauc_bundle.manifest_text(VERSION)
    assert text == ("[update]\ncompatible=smartheat-gateway-pi5\nversion=1.2.3\n\n[bundle]\nformat=verity\n\n"
                    "[image.rootfs]\nfilename=system-1.2.3.ext4\n\n[image.boot]\nfilename=boot-1.2.3.vfat\n")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(text)
    assert dict(parser["update"]) == {"compatible": "smartheat-gateway-pi5", "version": VERSION}
    assert dict(parser["bundle"]) == {"format": "verity"}
    # die Slot-Klassen heissen wie die der system.conf des Geraets
    conf = configparser.ConfigParser(interpolation=None)
    conf.read_string(rauc_signieren.SYSTEM_CONF_IN.read_text())
    klassen = {s.split(".")[1] for s in conf.sections() if s.startswith("slot.")}
    assert klassen == {s.split(".")[1] for s in parser.sections() if s.startswith("image.")} == {"rootfs", "boot"}


@pytest.mark.parametrize("version", ["1.2", "v1.2.3", "1.2.3-rc1", "../x", ""])
def test_manifest_lehnt_eine_ungueltige_version_ab(version):
    with pytest.raises(bundles.ManifestError, match="X.Y.Z"):
        rauc_bundle.manifest_text(version)


def test_compatible_ist_das_des_manifests_der_bundles():
    assert f"compatible={bundles.SYSTEM_COMPATIBLE}\n" in rauc_bundle.manifest_text(VERSION)


def test_ablauf_und_dateien_im_bundle(umgebung):
    u = umgebung
    u.baue()
    assert u.ziel.read_bytes() == b"SYSTEM-BUNDLE" and stat.S_IMODE(u.ziel.stat().st_mode) == 0o644
    (bundle,) = (a for a in u.aufrufe("docker-run") if a["programm"][:2] == ["rauc", "bundle"])
    assert [a["programm"][0] for a in u.aufrufe("docker-run")] == ["debugfs", "debugfs", "rauc", "rauc", "rauc", "rauc"]
    assert bundle["raucm"] == rauc_bundle.manifest_text(VERSION)
    assert sorted(bundle["inhalt"]) == ["boot-1.2.3.vfat", "manifest.raucm", "system-1.2.3.ext4"]
    # harte Verknuepfung: dieselben Bytes ohne Kopie von mehreren GB
    assert bundle["inhalt"]["system-1.2.3.ext4"] == [u.system.stat().st_ino, 4096]
    assert bundle["inhalt"]["boot-1.2.3.vfat"] == [u.boot.stat().st_ino, 2048]
    assert bundle["kette_da"] is True  # die Wegwerf-Kette lag beim Bau da
    assert (u.system.read_bytes(), u.boot.read_bytes()) == (b"S" * 4096, b"B" * 2048)  # die Quellen bleiben


def test_container_sieht_genau_die_buehne_unter_demselben_pfad(umgebung):
    """Review Task 1 (a): rauc in einem Container sieht nur gemountete Pfade. Alle Arbeitsdateien (system.conf, sig.cms,
    sig.neu), das Rohbundle, das Ergebnis, die Roots und die Wegwerf-CA liegen in der Buehne, die 1:1 gemountet ist."""
    u = umgebung
    u.baue()
    buehne = str(u.arbeit.resolve())
    laeufe = u.aufrufe("docker-run")
    assert len(laeufe) == 6
    for lauf in laeufe:
        o = lauf["optionen"]
        assert o[:5] == ["--rm", "--network", "none", "--security-opt", "label=disable"], o
        assert o[o.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
        assert o[o.index("-v") + 1] == f"{buehne}:{buehne}" and o.count("-v") == 1  # nur die Buehne, sonst nichts
        assert o[o.index("-w") + 1] == buehne
        assert lauf["bild"] == rauc_bundle.RAUC_IMAGE
        assert lauf["ausserhalb"] == [], lauf
    # die Schritte der Signierung nennen die Buehne wirklich (Gegenprobe: der Test sieht die Pfade)
    sigs = [eintrag["programm"] for eintrag in laeufe if "extract-signature" in eintrag["programm"]]
    assert sigs and all(a.startswith(f"{buehne}/") for a in sigs[0] if a.startswith("/"))
    assert any(a.startswith(f"{buehne}/arbeit/rauc-signieren-") for a in sigs[0])
    assert any(a.endswith(f"{buehne}/kette/root.crt") for a in sigs[0])
    ersetzen = next(eintrag["programm"] for eintrag in laeufe if "replace-signature" in eintrag["programm"])
    assert f"{buehne}/roots.pem" in ersetzen  # die Roots als Kopie in der Buehne, nicht der Pfad des Aufrufers
    assert str(u.roots) not in " ".join(sum((eintrag["programm"] for eintrag in laeufe), []))


def test_buehne_ist_danach_weg_samt_wegwerf_schluessel(umgebung):
    u = umgebung
    u.baue()
    assert not u.arbeit.exists()
    assert sorted(p.name for p in u.ziel.parent.iterdir()) == ["system.raucb"]


def test_rauc_bild_wird_gebaut_mit_digest_gepinnt_und_tag_nach_inhalt(umgebung):
    u = umgebung
    u.baue()
    (bau,) = u.aufrufe("docker")
    assert bau["argv"] == ["build", "--quiet", "-t", rauc_bundle.RAUC_IMAGE, "-"]
    assert bau["stdin"] == rauc_bundle.DOCKERFILE
    assert bau["stdin"].startswith(f"FROM {rauc_bundle.BASIS_IMAGE}\n")
    assert rauc_bundle.BASIS_IMAGE.split("@")[1] == "sha256:913f6706df59a68922d1dd08f78c2476560a8d367897200a6005b00e5f67c2d5"
    assert all(p in bau["stdin"] for p in ("rauc", "squashfs-tools", "e2fsprogs", "1\\.13", "install -d -m 1777 /var/lib/rauc"))
    assert "setwell-rauc:" + hashlib.sha256(rauc_bundle.DOCKERFILE.encode()).hexdigest()[:12] == rauc_bundle.RAUC_IMAGE


def test_basis_image_ist_in_den_docker_tests_derselbe_digest():
    tests = Path(__file__).resolve().parents[3] / "tests"
    for skript in ("test_gateway_install.sh", "test_gateway_rauc.sh"):
        assert rauc_bundle.BASIS_IMAGE in (tests / skript).read_text(), skript


def test_openssl_laeuft_auf_dem_rechner_der_schluessel_kommt_nie_in_den_container(umgebung):
    u = umgebung
    u.baue(URI)
    signiert = [a["argv"] for a in u.aufrufe("openssl") if "-sign" in a["argv"]]
    assert len(signiert) == 1 and signiert[0][signiert[0].index("-inkey") + 1] == URI
    assert signiert[0][:6] == ["cms", "-sign", "-provider", "pkcs11", "-provider", "default"]
    container = u.aufrufe("docker-run") + u.aufrufe("docker")
    assert len(container) == 7  # Gegenprobe: es gibt Aufrufe, die hier geprueft werden
    for aufruf in container:
        assert "GEHEIME-PIN" not in json.dumps(aufruf) and "pkcs11" not in json.dumps(aufruf)


@pytest.mark.parametrize("antwort", [
    {rauc_bundle.IMAGE_VERSION_PY: 'GATEWAY_VERSION = "1.2.4"\n', rauc_bundle.IMAGE_VERSION_DATEI: "1.2.3\n"},
    {rauc_bundle.IMAGE_VERSION_PY: 'GATEWAY_VERSION = "1.2.3"\n', rauc_bundle.IMAGE_VERSION_DATEI: "1.2.4\n"},
    {rauc_bundle.IMAGE_VERSION_PY: 'GATEWAY_VERSION = "0.9.0"\n', rauc_bundle.IMAGE_VERSION_DATEI: "0.9.0\n"},
    {rauc_bundle.IMAGE_VERSION_PY: "nichts\n", rauc_bundle.IMAGE_VERSION_DATEI: "1.2.3\n"},
    {rauc_bundle.IMAGE_VERSION_PY: 'GATEWAY_VERSION = "1.2.3"\n', rauc_bundle.IMAGE_VERSION_DATEI: "1.2.3.4\n"},
    {rauc_bundle.IMAGE_VERSION_PY: 'GATEWAY_VERSION = "1.2.3"\n', rauc_bundle.IMAGE_VERSION_DATEI: ""},
], ids=["version.py", "VERSION", "beide-anders", "version.py-leer", "VERSION-vier-teile", "VERSION-leer"])
def test_image_mit_anderer_version_wird_abgelehnt_bevor_gebaut_wird(umgebung, monkeypatch, antwort):
    """Review Task 3 (uebergreifend): traegt das Host-Paket im Image nicht die Release-Version, fiele jedes Geraet mit
    `start_fehlgeschlagen` zurueck. Gelesen wird die Datei im Image, vor `rauc bundle`."""
    u = umgebung
    monkeypatch.setenv("ATTRAPPE_IMAGE", json.dumps(antwort))
    with pytest.raises(BundleFehler, match="Image"):
        u.baue()
    assert not u.ziel.exists() and not u.arbeit.exists()
    assert all(a["programm"][0] == "debugfs" for a in u.aufrufe("docker-run"))  # kein rauc bundle, kein openssl
    assert u.aufrufe("openssl") == []


def test_version_wird_in_der_datei_des_images_gelesen_nicht_im_namen(umgebung):
    u = umgebung
    u.baue()
    gelesen = [(a["programm"][2], a["image"]) for a in u.aufrufe("docker-run") if a["programm"][0] == "debugfs"]
    buehne = u.arbeit.resolve()
    assert gelesen == [(f"cat {rauc_bundle.IMAGE_VERSION_PY}", f"{buehne}/inhalt/system-1.2.3.ext4"),
                       (f"cat {rauc_bundle.IMAGE_VERSION_DATEI}", f"{buehne}/inhalt/system-1.2.3.ext4")]
    assert [a["programm"][:2] for a in u.aufrufe("docker-run") if a["programm"][0] == "debugfs"] == [["debugfs", "-R"]] * 2


@pytest.mark.parametrize("schritt", ["build", "debugfs", "bundle", "extract-signature", "verify", "sign",
                                     "replace-signature", "info"])
def test_fehler_in_jedem_schritt_laesst_weder_ergebnis_noch_buehne_zurueck(umgebung, monkeypatch, capfd, schritt):
    u = umgebung
    monkeypatch.setenv("ATTRAPPE_FEHLER", schritt)
    monkeypatch.setenv("ATTRAPPE_MELDUNG", f"Grund {schritt} {URI}")
    with pytest.raises(SigniereFehler):
        u.baue(URI)
    assert not u.ziel.exists() and not u.arbeit.exists()
    ausgabe = capfd.readouterr()
    assert f"Grund {schritt} [Schluessel]" in ausgabe.err and ausgabe.out == ""  # die Meldung kommt geschwaerzt durch
    assert "GEHEIME-PIN" not in ausgabe.err and "pkcs11:" not in ausgabe.err


def test_vorhandenes_ergebnis_oder_vorhandene_buehne_wird_nie_ueberschrieben(umgebung):
    u = umgebung
    u.ziel.parent.mkdir()
    u.ziel.write_bytes(b"ALT")
    with pytest.raises(BundleFehler, match="existiert"):
        u.baue()
    assert u.ziel.read_bytes() == b"ALT" and u.aufrufe() == []
    u.ziel.unlink()
    u.arbeit.mkdir()
    (u.arbeit / "fremd").write_text("meins")
    with pytest.raises(BundleFehler, match="existiert"):
        u.baue()
    assert (u.arbeit / "fremd").read_text() == "meins" and u.aufrufe() == []


@pytest.mark.parametrize("fehlt", ["system", "boot", "zertifikat", "roots"])
def test_fehlende_eingabe_scheitert_ohne_aufruf(umgebung, fehlt):
    u = umgebung
    getattr(u, fehlt).unlink()
    with pytest.raises(BundleFehler, match="fehlt"):
        u.baue()
    assert u.aufrufe() == [] and not u.arbeit.exists()


def test_ungueltige_version_scheitert_ohne_aufruf(umgebung):
    u = umgebung
    with pytest.raises(bundles.ManifestError, match="X.Y.Z"):
        u.baue(version="1.2")
    assert u.aufrufe() == [] and not u.arbeit.exists()


def test_kopie_wenn_verknuepfen_nicht_geht(umgebung, monkeypatch):
    """Andere Dateisysteme (os.link scheitert): die Images werden kopiert, das Bundle entsteht trotzdem."""
    u = umgebung

    def kein_link(*_):
        raise OSError("Invalid cross-device link")

    monkeypatch.setattr(os, "link", kein_link)
    u.baue()
    (bundle,) = (a for a in u.aufrufe("docker-run") if a["programm"][:2] == ["rauc", "bundle"])
    assert bundle["inhalt"]["system-1.2.3.ext4"][0] != u.system.stat().st_ino
    assert bundle["inhalt"]["system-1.2.3.ext4"][1] == 4096 and bundle["inhalt"]["boot-1.2.3.vfat"][1] == 2048


def test_main_exit_0_und_1(umgebung, capfd, monkeypatch):
    u = umgebung
    u.ziel.parent.mkdir()
    args = ["--version", VERSION, "--system", str(u.system), "--boot", str(u.boot), "--zertifikat", str(u.zertifikat),
            "--schluessel", u.schluessel, "--roots", str(u.roots), "--arbeit", str(u.arbeit), "--out", str(u.ziel)]
    assert rauc_bundle.main(args) == 0
    assert u.ziel.read_bytes() == b"SYSTEM-BUNDLE"
    assert "OK" in capfd.readouterr().err
    assert rauc_bundle.main(args) == 1  # Ergebnis existiert: nichts ueberschrieben
    assert "existiert" in capfd.readouterr().err
    monkeypatch.setenv("ATTRAPPE_IMAGE", json.dumps({rauc_bundle.IMAGE_VERSION_PY: 'GATEWAY_VERSION = "9.9.9"\n',
                                                     rauc_bundle.IMAGE_VERSION_DATEI: "9.9.9\n"}))
    u.ziel.unlink()
    assert rauc_bundle.main(args) == 1
    assert "Release hat 1.2.3" in capfd.readouterr().err and not u.ziel.exists()


def test_main_falscher_aufruf_exit_2():
    with pytest.raises(SystemExit) as ende:
        rauc_bundle.main(["--version", VERSION])
    assert ende.value.code == 2
