"""release_lokal.sh: Bau, Pruefung, Signatur und Upload am Betreiberrechner. `docker` (Image-Bau) und `scp` sind
Attrappen im PATH; Git, build_bundle.py, verify_bundle.py und signieren.py laufen echt. Das Skript liegt in einem
Wegwerf-Repo (`gateway` ist dort ein Symlink auf den Checkout), damit Tag und Arbeitsbaum echt geprueft werden, ohne dass
der Checkout selbst angefasst wird. Tags tragen eine SSH-Signatur (git tag -s mit gpg.format=ssh); die Schluesselliste
kommt aus origin/main (ein Wegwerf-Repo `ablage`), nicht aus dem Checkout und nicht aus der Git-Konfiguration."""
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import ca

GW = Path(__file__).resolve().parents[2]
VERSION = "9.9.9"
TAG = f"gateway-v{VERSION}"
ZIEL = "bundles@ablage.example.org:/srv/setwell/gateway"
BASIS = "https://download.example.org/gateway"

DOCKER = f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})
from oci_helper import make_oci_archive
logs = Path(os.environ["ATTRAPPE_LOGS"])
with (logs / "docker.log").open("a") as protokoll:
    protokoll.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("ATTRAPPE_DOCKER_FEHLER") and sys.argv[1] != "run":
    sys.exit(1)
args = sys.argv[1:]
if args[0] == "run":  # syft: CycloneDX-SBOM auf stdout
    if os.environ.get("ATTRAPPE_SYFT_FEHLER"):
        sys.exit(1)
    print(os.environ.get("ATTRAPPE_SBOM") or json.dumps(dict(bomFormat="CycloneDX", specVersion="1.7", components=[])))
    sys.exit(0)
ziel = [t.split("=", 1)[1] for t in args[args.index("--output") + 1].split(",") if t.startswith("dest=")][0]
make_oci_archive(Path(ziel))
'''
SCP = f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
quelle = Path(args[args.index("--") + 1])
with (Path(os.environ["ATTRAPPE_LOGS"]) / "scp.log").open("a") as protokoll:
    protokoll.write(json.dumps({{"argv": args, "dateien": sorted(p.name for p in quelle.iterdir())}}) + "\\n")
'''


def _git(repo: Path, *args: str, **env: str) -> str:
    umgebung = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", **env}
    return subprocess.run(["git", "-C", str(repo), *args], env=umgebung, capture_output=True, text=True, check=True).stdout


def _ssh_schluessel(pfad: Path) -> Path:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test@example.org", "-f", str(pfad)], check=True)
    return pfad


@pytest.fixture
def werk(tmp_path):
    """Wegwerf-Repo mit einem Commit; Attrappen docker und scp im PATH; erlaubte Signierschluessel in `signers`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "gateway").symlink_to(GW)
    erlaubt = _ssh_schluessel(tmp_path / "erlaubt")
    fremd = _ssh_schluessel(tmp_path / "fremd")
    zeile = 'test@example.org namespaces="git" {}'
    # Die Konfiguration des Betreibers traut hier beiden Schluesseln; massgeblich ist nur origin/main (M-5).
    signers = tmp_path / "signers"
    signers.write_text(zeile.format(erlaubt.with_suffix(".pub").read_text()) + zeile.format(fremd.with_suffix(".pub").read_text()))
    _git(repo, "init", "-q", "-b", "main")
    for schluessel, wert in (("user.name", "Test"), ("user.email", "test@example.org"), ("commit.gpgsign", "false"),
                             ("tag.gpgsign", "false"), ("gpg.format", "ssh"), ("user.signingkey", str(erlaubt)),
                             ("gpg.ssh.allowedSignersFile", str(signers))):
        _git(repo, "config", schluessel, wert)
    ablage = tmp_path / "ablage"  # der Remote origin: sein Zweig main haelt die Schluesselliste
    (ablage / ".github").mkdir(parents=True)
    (ablage / ".github" / "allowed_signers").write_text(zeile.format(erlaubt.with_suffix(".pub").read_text()))
    _git(ablage, "init", "-q", "-b", "main")
    _git(ablage, "add", ".github")
    _git(ablage, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-q", "-m", "Liste")
    _git(repo, "remote", "add", "origin", str(ablage))
    _git(repo, "add", "gateway")
    _git(repo, "commit", "-q", "-m", "Stand")
    bin_dir, logs = tmp_path / "bin", tmp_path / "logs"
    bin_dir.mkdir()
    logs.mkdir()
    for name, text in (("docker", DOCKER), ("scp", SCP)):
        datei = bin_dir / name
        datei.write_text(text)
        datei.chmod(datei.stat().st_mode | stat.S_IXUSR)
    kette = tmp_path / "kette"
    ca.test_kette_anlegen(kette)
    werk = type("Werk", (), {})()
    werk.repo, werk.bin, werk.logs, werk.kette, werk.erlaubt, werk.fremd = repo, bin_dir, logs, kette, erlaubt, fremd
    werk.ablage = ablage
    werk.aus = tmp_path / "aus"
    return werk


def _signierter_tag(werk, schluessel: Path | None = None):
    _git(werk.repo, "-c", f"user.signingkey={schluessel or werk.erlaubt}", "tag", "-s", "-m", "Release", TAG)


def _lauf(werk, *args, version=VERSION, umgebung=None, skript_args=None):
    argumente = args or ("--schluessel", str(werk.kette / "release.key"), "--zertifikat", str(werk.kette / "release.crt"),
                         "--roots", str(werk.kette / "root.crt"), "--aus", str(werk.aus))
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "PATH": f"{werk.bin}:{os.environ['PATH']}", "ATTRAPPE_LOGS": str(werk.logs), "PYTHON": sys.executable,
           "TMPDIR": str(werk.bin.parent)}
    env.pop("SHG_BUNDLE_ZIEL", None)
    env.pop("SHG_BUNDLE_BASIS", None)
    env.update(umgebung or {})
    return subprocess.run(["bash", str(werk.repo / "gateway" / "release" / "release_lokal.sh"), version, *argumente],
                          env=env, capture_output=True, text=True, timeout=300)


def _log(werk, name) -> list:
    pfad = werk.logs / name
    return [json.loads(zeile) for zeile in pfad.read_text().splitlines()] if pfad.exists() else []


def _pruefen(werk, bundle: Path) -> int:
    umgebung = {**os.environ, "PYTHONPATH": str(GW / "host")}
    return subprocess.run([sys.executable, "-m", "smartheat_host.kette", str(werk.kette / "root.crt"),
                           str(bundle / "manifest.json"), str(bundle / "manifest.json.sig")], env=umgebung,
                          capture_output=True, text=True).returncode


DATEIEN = ["docker-compose.yml", "gateway-image.oci.tar", "manifest.json", "manifest.json.sig", "mosquitto.conf"]


def test_release_lokal_probe_baut_und_signiert_ohne_upload(werk):
    (werk.repo / "unversioniert.txt").write_text("Probe darf einen unsauberen Baum haben, ohne Tag\n")
    ergebnis = _lauf(werk, "--probe", "--schluessel", str(werk.kette / "release.key"),
                     "--zertifikat", str(werk.kette / "release.crt"), "--roots", str(werk.kette / "root.crt"),
                     "--aus", str(werk.aus))
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert ergebnis.stdout == ""  # keine Werte fuer bundle add: nichts wurde hochgeladen
    assert _log(werk, "scp.log") == []
    bundle = werk.aus / VERSION
    assert sorted(p.name for p in bundle.iterdir()) == DATEIEN
    assert _pruefen(werk, bundle) == 0
    build, syft = _log(werk, "docker.log")
    assert build[:2] == ["buildx", "build"] and "--sbom=true" in build
    assert build[build.index("--platform") + 1] == "linux/arm64"
    assert build[build.index("-f") + 1] == str(werk.repo / "gateway" / "Dockerfile")
    assert build[build.index("--output") + 1] == f"type=oci,dest={werk.aus}/image.oci.tar,rewrite-timestamp=true"
    epoche = _git(werk.repo, "log", "-1", "--format=%ct").strip()
    assert f"SOURCE_DATE_EPOCH={epoche}" in build
    assert build[-1] == str(werk.repo)  # Kontext: die Repo-Wurzel, wie bei docker build -f gateway/Dockerfile .
    assert syft[0] == "run" and (werk.aus / f"gateway-{VERSION}.cdx.json").is_file()


def test_cyclonedx_sbom_per_gepinntem_syft_neben_dem_bundle(werk):
    """SG-16: CycloneDX aus dem OCI-Archiv per syft-Container (Digest gepinnt, ohne Netz, Archiv nur lesend); die Datei
    liegt neben dem Bundle, nicht darin, und geht nicht in die Ablage."""
    _signierter_tag(werk)
    ergebnis = _lauf(werk, "--schluessel", str(werk.kette / "release.key"), "--zertifikat", str(werk.kette / "release.crt"),
                     "--roots", str(werk.kette / "root.crt"), "--aus", str(werk.aus),
                     umgebung={"SHG_BUNDLE_ZIEL": ZIEL, "SHG_BUNDLE_BASIS": BASIS})
    assert ergebnis.returncode == 0, ergebnis.stderr
    build, syft = _log(werk, "docker.log")
    assert build[:2] == ["buildx", "build"]
    assert syft[0] == "run" and "--rm" in syft and syft[syft.index("--network") + 1] == "none"
    image = next(a for a in syft if a.startswith("anchore/syft:"))
    assert re.fullmatch(r"anchore/syft:v\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", image), image
    assert syft[syft.index(image) + 1:] == ["scan", "oci-archive:/image.oci.tar", "-o", "cyclonedx-json"]
    assert f"{werk.aus.resolve()}/image.oci.tar:/image.oci.tar:ro" in syft
    sbom = werk.aus / f"gateway-{VERSION}.cdx.json"
    assert json.loads(sbom.read_text())["bomFormat"] == "CycloneDX"
    assert sbom.name not in {p.name for p in (werk.aus / VERSION).iterdir()}
    (hochgeladen,) = _log(werk, "scp.log")
    assert sbom.name not in hochgeladen["dateien"]


@pytest.mark.parametrize("umgebung", [{"ATTRAPPE_SYFT_FEHLER": "1"}, {"ATTRAPPE_SBOM": "kein cyclonedx"}])
def test_fehlt_das_cyclonedx_sbom_wird_nichts_signiert_oder_hochgeladen(werk, umgebung):
    _signierter_tag(werk)
    ergebnis = _lauf(werk, "--schluessel", str(werk.kette / "release.key"), "--zertifikat", str(werk.kette / "release.crt"),
                     "--roots", str(werk.kette / "root.crt"), "--aus", str(werk.aus),
                     umgebung={"SHG_BUNDLE_ZIEL": ZIEL, "SHG_BUNDLE_BASIS": BASIS, **umgebung})
    assert ergebnis.returncode == 1 and "syft" in ergebnis.stderr and ergebnis.stdout == ""
    assert _log(werk, "scp.log") == []
    assert not (werk.aus / VERSION).exists() and not list(werk.aus.glob("*.json"))


def test_syft_image_ist_im_skript_und_in_der_ci_derselbe_digest():
    skript = (GW / "release" / "release_lokal.sh").read_text()
    release = (GW.parent / ".github" / "workflows" / "release.yml").read_text()
    (image,) = re.findall(r'^SYFT_IMAGE="([^"]+)"', skript, flags=re.MULTILINE)
    assert re.fullmatch(r"anchore/syft:v\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", image)
    assert image in release


def test_release_lokal_laedt_per_scp_und_gibt_die_vier_werte_aus(werk):
    _signierter_tag(werk)
    ergebnis = _lauf(werk, "--schluessel", str(werk.kette / "release.key"), "--zertifikat", str(werk.kette / "release.crt"),
                     "--roots", str(werk.kette / "root.crt"), "--aus", str(werk.aus),
                     umgebung={"SHG_BUNDLE_ZIEL": ZIEL + "/", "SHG_BUNDLE_BASIS": BASIS + "/"})
    assert ergebnis.returncode == 0, ergebnis.stderr
    bundle = werk.aus.resolve() / VERSION
    sha = hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest()
    assert ergebnis.stdout.splitlines() == [
        f"version={VERSION}",
        f"manifest_url={BASIS}/{VERSION}/manifest.json",
        f"sha256={sha}",
        f"signaturdatei={bundle}/manifest.json.sig",
    ]
    (hochgeladen,) = _log(werk, "scp.log")
    assert hochgeladen["argv"] == ["-r", "--", str(bundle), ZIEL + "/"]  # der Ordner <version> landet in der Ablage
    assert hochgeladen["dateien"] == DATEIEN
    assert _pruefen(werk, bundle) == 0
    assert [aufruf[0] for aufruf in _log(werk, "docker.log")] == ["buildx", "run"]


def test_release_lokal_ohne_signierten_tag_bricht_ab(werk):
    ergebnis = _lauf(werk, "--schluessel", str(werk.kette / "release.key"), "--zertifikat", str(werk.kette / "release.crt"),
                     "--roots", str(werk.kette / "root.crt"), "--aus", str(werk.aus),
                     umgebung={"SHG_BUNDLE_ZIEL": ZIEL, "SHG_BUNDLE_BASIS": BASIS})
    assert ergebnis.returncode != 0 and ergebnis.stdout == ""
    assert TAG in ergebnis.stderr
    assert _log(werk, "docker.log") == [] and _log(werk, "scp.log") == [] and not werk.aus.exists()


def _kein_tag(werk):
    pass


def _leichter_tag(werk):
    _git(werk.repo, "tag", TAG)


def _unsignierter_tag(werk):
    _git(werk.repo, "tag", "-a", "-m", "Release", TAG)


def _tag_eines_fremden_schluessels(werk):
    _signierter_tag(werk, werk.fremd)


def _tag_nur_mit_der_liste_im_eigenen_checkout(werk):
    """Der fremde Schluessel steht in .github/allowed_signers des getaggten Commits (und in der Git-Konfiguration des
    Betreibers), aber nicht auf origin/main: die Liste im Checkout zaehlt nicht (M-5)."""
    (werk.repo / ".github").mkdir()
    (werk.repo / ".github" / "allowed_signers").write_text(
        f'test@example.org namespaces="git" {werk.fremd.with_suffix(".pub").read_text()}')
    _git(werk.repo, "add", ".github")
    _git(werk.repo, "commit", "-q", "-m", "eigene Liste")
    _signierter_tag(werk, werk.fremd)


def _liste_auf_origin_main_fehlt(werk):
    _git(werk.ablage, "rm", "-q", ".github/allowed_signers")
    _git(werk.ablage, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-q", "-m", "ohne Liste")
    _signierter_tag(werk)


def _liste_auf_origin_main_ist_der_platzhalter(werk):
    (werk.ablage / ".github" / "allowed_signers").write_text('# Platzhalter\nSMARTHEAT-PLATZHALTER namespaces="git" ssh-ed25519 AAAA\n')
    _git(werk.ablage, "add", ".github")
    _git(werk.ablage, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-q", "-m", "Platzhalter")
    _signierter_tag(werk)


def _tag_auf_altem_commit(werk):
    _signierter_tag(werk)
    _git(werk.repo, "commit", "-q", "--allow-empty", "-m", "danach")


def _unsauberer_baum(werk):
    _signierter_tag(werk)
    (werk.repo / "neu.txt").write_text("nicht eingecheckt\n")


@pytest.mark.parametrize("vorbereitung", [_kein_tag, _leichter_tag, _unsignierter_tag, _tag_eines_fremden_schluessels,
                                          _tag_nur_mit_der_liste_im_eigenen_checkout, _liste_auf_origin_main_fehlt,
                                          _liste_auf_origin_main_ist_der_platzhalter, _tag_auf_altem_commit,
                                          _unsauberer_baum])
def test_release_lokal_lehnt_jeden_unsauberen_stand_ab_bevor_gebaut_wird(werk, vorbereitung):
    vorbereitung(werk)
    ergebnis = _lauf(werk, "--schluessel", str(werk.kette / "release.key"), "--zertifikat", str(werk.kette / "release.crt"),
                     "--roots", str(werk.kette / "root.crt"), "--aus", str(werk.aus),
                     umgebung={"SHG_BUNDLE_ZIEL": ZIEL, "SHG_BUNDLE_BASIS": BASIS})
    assert ergebnis.returncode == 1, ergebnis.stderr
    assert ergebnis.stdout == ""
    assert _log(werk, "docker.log") == [] and _log(werk, "scp.log") == [] and not werk.aus.exists()
    # Gegenprobe: derselbe Stand mit gueltigem Tag und sauberem Baum wird gebaut (siehe laedt_per_scp)


@pytest.mark.parametrize("umgebung", [
    {}, {"SHG_BUNDLE_ZIEL": ZIEL}, {"SHG_BUNDLE_BASIS": BASIS},
    {"SHG_BUNDLE_ZIEL": ZIEL, "SHG_BUNDLE_BASIS": "http://download.example.org/gateway"},
    {"SHG_BUNDLE_ZIEL": "ablage.example.org:/srv", "SHG_BUNDLE_BASIS": BASIS},
    {"SHG_BUNDLE_ZIEL": "-oProxyCommand=x@y:/z", "SHG_BUNDLE_BASIS": BASIS},
    {"SHG_BUNDLE_ZIEL": ZIEL + " /etc", "SHG_BUNDLE_BASIS": BASIS},
])
def test_release_lokal_ohne_ziel_und_basis_bricht_laut_ab(werk, umgebung):
    _signierter_tag(werk)
    ergebnis = _lauf(werk, "--schluessel", str(werk.kette / "release.key"), "--zertifikat", str(werk.kette / "release.crt"),
                     "--roots", str(werk.kette / "root.crt"), "--aus", str(werk.aus), umgebung=umgebung)
    assert ergebnis.returncode == 1 and "SHG_BUNDLE" in ergebnis.stderr
    assert _log(werk, "docker.log") == [] and _log(werk, "scp.log") == []


def test_probe_braucht_weder_ziel_noch_tag(werk):
    ergebnis = _lauf(werk, "--probe", "--schluessel", str(werk.kette / "release.key"),
                     "--zertifikat", str(werk.kette / "release.crt"), "--roots", str(werk.kette / "root.crt"))
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert _log(werk, "scp.log") == []
    assert not list(werk.bin.parent.glob("setwell-release.*"))  # der Wegwerf-Ordner ist weg


def test_docker_fehler_laesst_nichts_zurueck_und_laedt_nichts_hoch(werk):
    _signierter_tag(werk)
    ergebnis = _lauf(werk, "--schluessel", str(werk.kette / "release.key"), "--zertifikat", str(werk.kette / "release.crt"),
                     "--roots", str(werk.kette / "root.crt"), "--aus", str(werk.aus),
                     umgebung={"SHG_BUNDLE_ZIEL": ZIEL, "SHG_BUNDLE_BASIS": BASIS, "ATTRAPPE_DOCKER_FEHLER": "1"})
    assert ergebnis.returncode != 0 and ergebnis.stdout == ""
    assert _log(werk, "scp.log") == []
    assert not (werk.aus / VERSION).exists()


def test_signatur_gegen_fremde_roots_bricht_vor_dem_upload_ab(werk, tmp_path):
    _signierter_tag(werk)
    ca.test_kette_anlegen(tmp_path / "andere")
    ergebnis = _lauf(werk, "--schluessel", str(werk.kette / "release.key"), "--zertifikat", str(werk.kette / "release.crt"),
                     "--roots", str(tmp_path / "andere" / "root.crt"), "--aus", str(werk.aus),
                     umgebung={"SHG_BUNDLE_ZIEL": ZIEL, "SHG_BUNDLE_BASIS": BASIS})
    assert ergebnis.returncode != 0 and ergebnis.stdout == ""
    assert _log(werk, "scp.log") == []
    assert not (werk.aus / VERSION / "manifest.json.sig").exists()


def test_vorhandenes_ergebnis_wird_nie_ueberschrieben(werk):
    args = ("--probe", "--schluessel", str(werk.kette / "release.key"), "--zertifikat", str(werk.kette / "release.crt"),
            "--roots", str(werk.kette / "root.crt"), "--aus", str(werk.aus))
    assert _lauf(werk, *args).returncode == 0
    vorher = (werk.aus / VERSION / "manifest.json").read_bytes()
    zweiter = _lauf(werk, *args)
    assert zweiter.returncode == 1 and "existiert" in zweiter.stderr
    assert (werk.aus / VERSION / "manifest.json").read_bytes() == vorher


@pytest.mark.parametrize("args", [
    [],
    ["--probe"],
    ["--probe", "--schluessel", "k"],
    ["--probe", "--zertifikat", "z"],
    ["--probe", "--schluessel", "k", "--zertifikat", "z", "--unbekannt"],
    ["--probe", "--schluessel"],
])
def test_falscher_aufruf_exit_2_ohne_aufruf(werk, args):
    ergebnis = _lauf(werk, *args) if args else _lauf(werk, "--probe")
    assert ergebnis.returncode == 2
    assert _log(werk, "docker.log") == []


@pytest.mark.parametrize("version", ["1.2", "v1.2.3", "1.2.3-rc1", "../x", "1.2.3.4", ""])
def test_ungueltige_version_exit_2(werk, version):
    ergebnis = _lauf(werk, "--probe", "--schluessel", "k", "--zertifikat", "z", version=version)
    assert ergebnis.returncode == 2 and _log(werk, "docker.log") == []


def test_fehlende_schluesseldatei_oder_zertifikat_bricht_vor_dem_bau_ab(werk):
    ergebnis = _lauf(werk, "--probe", "--schluessel", str(werk.kette / "gibt-es-nicht.key"),
                     "--zertifikat", str(werk.kette / "release.crt"), "--roots", str(werk.kette / "root.crt"))
    assert ergebnis.returncode == 1 and _log(werk, "docker.log") == []
    ergebnis = _lauf(werk, "--probe", "--schluessel", str(werk.kette / "release.key"),
                     "--zertifikat", str(werk.kette / "fehlt.crt"), "--roots", str(werk.kette / "root.crt"))
    assert ergebnis.returncode == 1 and _log(werk, "docker.log") == []


def test_pkcs11_uri_wird_ohne_dateipruefung_durchgereicht(werk):
    """Eine pkcs11:-URI ist keine Datei: das Skript reicht sie an signieren.py durch (hier scheitert openssl ohne Token)."""
    _signierter_tag(werk)
    ergebnis = _lauf(werk, "--schluessel", "pkcs11:token=gibt-es-nicht;object=x?pin-value=GEHEIME-PIN",
                     "--zertifikat", str(werk.kette / "release.crt"), "--roots", str(werk.kette / "root.crt"),
                     "--aus", str(werk.aus), umgebung={"SHG_BUNDLE_ZIEL": ZIEL, "SHG_BUNDLE_BASIS": BASIS})
    assert ergebnis.returncode == 1
    assert "GEHEIME-PIN" not in ergebnis.stdout + ergebnis.stderr and "pkcs11:" not in ergebnis.stdout + ergebnis.stderr
    assert [aufruf[0] for aufruf in _log(werk, "docker.log")] == ["buildx", "run"]  # gebaut wurde, signiert nicht
    assert not (werk.aus / VERSION / "manifest.json.sig").exists() and _log(werk, "scp.log") == []


@pytest.mark.parametrize("schluessel", ["pkcs11:token=echt;object=release", "pkcs11:token=echt?pin-value=GEHEIME-PIN"])
def test_probe_lehnt_einen_pkcs11_schluessel_ab(werk, schluessel):
    """M-4: --probe baut und signiert ohne Tag- und Baum-Check, also nie mit dem echten Token."""
    ergebnis = _lauf(werk, "--probe", "--schluessel", schluessel, "--zertifikat", str(werk.kette / "release.crt"),
                     "--roots", str(werk.kette / "root.crt"), "--aus", str(werk.aus))
    assert ergebnis.returncode == 1 and "pkcs11" in ergebnis.stderr and ergebnis.stdout == ""
    assert "GEHEIME-PIN" not in ergebnis.stderr
    assert _log(werk, "docker.log") == [] and not werk.aus.exists()


@pytest.mark.parametrize("roots", [None, "gateway/host/release-roots.pem", "gateway/../gateway/host/release-roots.pem"])
def test_probe_lehnt_den_standard_schluesselbund_ab(werk, roots):
    argumente = ["--probe", "--schluessel", str(werk.kette / "release.key"), "--zertifikat", str(werk.kette / "release.crt"),
                 "--aus", str(werk.aus)]
    if roots:
        argumente += ["--roots", str(werk.repo / roots)]
    ergebnis = _lauf(werk, *argumente)
    assert ergebnis.returncode == 1 and "Schluesselbund" in ergebnis.stderr and ergebnis.stdout == ""
    assert _log(werk, "docker.log") == [] and not werk.aus.exists()
