"""Startauswahl: Lage aus DT-Datei und autoboot.txt, atomares Schreiben von autoboot.txt, Befehlszeilen von RaucTryboot
(gegen ein Fake-run) und das FakeStartauswahl des Updater-Tests."""
import os
import stat
import subprocess
from pathlib import Path

import pytest
from startauswahl_fake import FakeStartauswahl

from smartheat_host import startauswahl
from smartheat_host.startauswahl import RaucTryboot, StartauswahlFehler

KANONISCH = "[all]\ntryboot_a_b=1\nboot_partition={p}\n[tryboot]\nboot_partition={t}\n"


def dt_ordner(tmp_path: Path, partition: int | None) -> Path:
    dt = tmp_path / "dt"
    dt.mkdir(exist_ok=True)
    if partition is not None:
        (dt / "partition").write_bytes(partition.to_bytes(4, "big"))
    return dt


def autoboot_datei(tmp_path: Path, primaer: int = 2) -> Path:
    pfad = tmp_path / "bootfs" / "autoboot.txt"
    pfad.parent.mkdir(exist_ok=True)
    pfad.write_text(KANONISCH.format(p=primaer, t=5 - primaer))
    return pfad


# --- Lage ---

@pytest.mark.parametrize(("partition", "slot"), [(2, "A"), (3, "B")])
def test_gestartet_ist_die_partition_aus_dem_device_tree(tmp_path, partition, slot):
    sa = RaucTryboot(autoboot=autoboot_datei(tmp_path), dt=dt_ordner(tmp_path, partition))
    assert sa.gestartet() == slot


@pytest.mark.parametrize(("inhalt"), [b"", b"\x00\x00\x02", (2).to_bytes(8, "big"), (1).to_bytes(4, "big"),
                                      (4).to_bytes(4, "big"), (2).to_bytes(4, "little")])
def test_unbrauchbare_dt_datei_ist_ein_fehler(tmp_path, inhalt):
    dt = dt_ordner(tmp_path, None)
    (dt / "partition").write_bytes(inhalt)
    with pytest.raises(StartauswahlFehler):
        RaucTryboot(autoboot=autoboot_datei(tmp_path), dt=dt).gestartet()


def test_fehlende_dt_datei_ist_ein_fehler(tmp_path):
    with pytest.raises(StartauswahlFehler, match="partition"):
        RaucTryboot(autoboot=autoboot_datei(tmp_path), dt=dt_ordner(tmp_path, None)).gestartet()


@pytest.mark.parametrize(("partition", "slot"), [(2, "A"), (3, "B")])
def test_primaer_ist_boot_partition_aus_all(tmp_path, partition, slot):
    sa = RaucTryboot(autoboot=autoboot_datei(tmp_path, partition), dt=dt_ordner(tmp_path, 2))
    assert sa.primaer() == slot


def test_primaer_ignoriert_tryboot_und_kommentare(tmp_path):
    pfad = tmp_path / "autoboot.txt"
    pfad.write_text("# Kommentar\r\n[all]\r\ntryboot_a_b=1\r\n boot_partition = 3 \r\n\r\n[tryboot]\r\nboot_partition=2\r\n")
    assert RaucTryboot(autoboot=pfad, dt=dt_ordner(tmp_path, 2)).primaer() == "B"


@pytest.mark.parametrize("text", [
    "",
    "[tryboot]\nboot_partition=3\n",
    "[all]\ntryboot_a_b=1\n[tryboot]\nboot_partition=3\n",
    "[all]\nboot_partition=1\n",
    "[all]\nboot_partition=4\n",
    "[all]\nboot_partition=zwei\n",
])
def test_unbrauchbare_autoboot_datei_ist_ein_fehler(tmp_path, text):
    pfad = tmp_path / "autoboot.txt"
    pfad.write_text(text)
    with pytest.raises(StartauswahlFehler):
        RaucTryboot(autoboot=pfad, dt=dt_ordner(tmp_path, 2)).primaer()


def test_fehlende_autoboot_datei_ist_ein_fehler(tmp_path):
    with pytest.raises(StartauswahlFehler, match="autoboot"):
        RaucTryboot(autoboot=tmp_path / "autoboot.txt", dt=dt_ordner(tmp_path, 2)).primaer()


@pytest.mark.parametrize(("partition", "primaer", "erwartet"), [(2, 2, False), (3, 3, False), (3, 2, True), (2, 3, True)])
def test_probestart_ist_gestartet_ungleich_primaer(tmp_path, partition, primaer, erwartet):
    dt = dt_ordner(tmp_path, partition)
    (dt / "tryboot").write_bytes((1).to_bytes(4, "big"))  # der Merker zaehlt nicht: er bleibt nach mark-good bei 1
    assert RaucTryboot(autoboot=autoboot_datei(tmp_path, primaer), dt=dt).im_probestart() is erwartet


# --- autoboot.txt schreiben ---

def test_schreibe_primaer_schreibt_die_kanonische_datei_ueber_neu(tmp_path):
    pfad = autoboot_datei(tmp_path, 2)
    startauswahl.schreibe_primaer(pfad, "B")
    assert pfad.read_text() == KANONISCH.format(p=3, t=2)
    assert sorted(p.name for p in pfad.parent.iterdir()) == ["autoboot.txt"]


def test_schreibe_primaer_raeumt_ein_altes_neu_nach_einem_stromausfall_weg(tmp_path):
    pfad = autoboot_datei(tmp_path, 2)
    (pfad.parent / "autoboot.txt.neu").write_text("halb")
    startauswahl.schreibe_primaer(pfad, "B")
    assert sorted(p.name for p in pfad.parent.iterdir()) == ["autoboot.txt"]
    assert pfad.read_text() == KANONISCH.format(p=3, t=2)


def test_schreibe_primaer_fsync_datei_dann_rename_dann_fsync_ordner(tmp_path, monkeypatch):
    pfad = autoboot_datei(tmp_path, 2)
    ereignisse: list[str] = []
    echt_fsync, echt_replace = os.fsync, os.replace

    def fsync(fd):
        art = "ordner" if stat.S_ISDIR(os.fstat(fd).st_mode) else "datei"
        ereignisse.append(f"fsync-{art}")
        echt_fsync(fd)

    def replace(quelle, ziel, **kw):
        ereignisse.append(f"replace {Path(quelle).name} -> {Path(ziel).name}")
        echt_replace(quelle, ziel, **kw)

    monkeypatch.setattr(startauswahl.os, "fsync", fsync)
    monkeypatch.setattr(startauswahl.os, "replace", replace)
    startauswahl.schreibe_primaer(pfad, "B")
    assert ereignisse == ["fsync-datei", "replace autoboot.txt.neu -> autoboot.txt", "fsync-ordner"]


def test_schreibe_primaer_liest_zurueck_und_meldet_abweichung(tmp_path, monkeypatch):
    pfad = autoboot_datei(tmp_path, 2)
    echt_replace = os.replace

    def replace(quelle, ziel, **kw):
        echt_replace(quelle, ziel, **kw)
        Path(ziel).write_text(KANONISCH.format(p=2, t=3))  # das Medium hat etwas anderes behalten

    monkeypatch.setattr(startauswahl.os, "replace", replace)
    with pytest.raises(StartauswahlFehler, match="Zurueck"):
        startauswahl.schreibe_primaer(pfad, "B")


def test_schreibe_primaer_ohne_ordner_ist_ein_fehler(tmp_path):
    with pytest.raises(StartauswahlFehler):
        startauswahl.schreibe_primaer(tmp_path / "gibt-es-nicht" / "autoboot.txt", "B")


# --- Befehlszeilen von RaucTryboot ---

class FakeRun:
    """Ersatz fuer subprocess.run: antwortet der Reihe nach, merkt sich Befehlszeile und Frist."""

    def __init__(self, *antworten) -> None:
        self.antworten = list(antworten)
        self.aufrufe: list[tuple[list[str], float | None]] = []

    def __call__(self, argv, *, timeout=None, **kw):
        self.aufrufe.append((list(argv), timeout))
        antwort = self.antworten.pop(0) if self.antworten else 0
        if isinstance(antwort, BaseException):
            raise antwort
        if isinstance(antwort, str):
            return subprocess.CompletedProcess(argv, 0, stdout=antwort, stderr="")
        return subprocess.CompletedProcess(argv, antwort, stdout="", stderr="Fehlermeldung")


class Uhr:
    """Zeit nur ueber sleep: monotonic zaehlt die Sekunden, die geschlafen wurden."""

    def __init__(self) -> None:
        self.jetzt = 0.0
        self.pausen: list[float] = []

    def monotonic(self) -> float:
        return self.jetzt

    def sleep(self, sekunden: float) -> None:
        self.pausen.append(sekunden)
        self.jetzt += sekunden


def echt(tmp_path, run, uhr=None):
    uhr = uhr or Uhr()
    return RaucTryboot(autoboot=autoboot_datei(tmp_path), dt=dt_ordner(tmp_path, 2), run=run, sleep=uhr.sleep,
                       monotonic=uhr.monotonic)


def test_probestart_ruft_systemctl_reboot_mit_dem_argument_0_tryboot(tmp_path):
    run = FakeRun()
    echt(tmp_path, run).probestart()
    assert run.aufrufe == [(["systemctl", "reboot", "--reboot-argument=0 tryboot"], 60)]


def test_bestaetigen_ruft_rauc_status_mark_good(tmp_path):
    run = FakeRun()
    echt(tmp_path, run).bestaetigen()
    assert run.aufrufe == [(["rauc", "status", "mark-good"], 60)]


def test_zurueckschalten_markiert_den_gestarteten_slot_bad_und_startet_neu(tmp_path):
    run = FakeRun()
    echt(tmp_path, run).zurueckschalten()
    assert run.aufrufe == [(["rauc", "status", "mark-bad", "booted"], 60), (["systemctl", "reboot"], 60)]


def test_zurueckschalten_startet_nicht_neu_wenn_mark_bad_scheitert(tmp_path):
    run = FakeRun(1)
    with pytest.raises(StartauswahlFehler, match="mark-bad"):
        echt(tmp_path, run).zurueckschalten()
    assert [argv[0] for argv, _ in run.aufrufe] == ["rauc"]


def test_normal_neustarten_ruft_nur_systemctl_reboot_ohne_mark_bad(tmp_path):
    run = FakeRun()
    echt(tmp_path, run).normal_neustarten()
    assert run.aufrufe == [(["systemctl", "reboot"], 60)]


def test_compatible_liest_rauc_status_im_shell_format(tmp_path):
    run = FakeRun("RAUC_SYSTEM_COMPATIBLE='smartheat-gateway-pi5'\nRAUC_SYSTEM_VARIANT=''\n"
                  "RAUC_SYSTEM_BOOTED_BOOTNAME='A'\n")
    assert echt(tmp_path, run).compatible() == "smartheat-gateway-pi5"
    assert run.aufrufe == [(["rauc", "status", "--output-format=shell"], 60)]


@pytest.mark.parametrize("ausgabe", ["", "RAUC_SYSTEM_VARIANT=''\n", "RAUC_SYSTEM_COMPATIBLE=\n"])
def test_compatible_ohne_wert_ist_ein_fehler(tmp_path, ausgabe):
    with pytest.raises(StartauswahlFehler, match="compatible"):
        echt(tmp_path, FakeRun(ausgabe)).compatible()


@pytest.mark.parametrize("aufruf", ["probestart", "bestaetigen", "zurueckschalten", "compatible", "normal_neustarten"])
def test_ein_fehlerhafter_befehl_ist_ein_startauswahlfehler(tmp_path, aufruf):
    with pytest.raises(StartauswahlFehler, match="Fehlermeldung"):
        getattr(echt(tmp_path, FakeRun(1)), aufruf)()


@pytest.mark.parametrize("aufruf", ["probestart", "bestaetigen", "zurueckschalten", "compatible", "normal_neustarten"])
def test_ein_fehlendes_programm_ist_ein_startauswahlfehler(tmp_path, aufruf):
    with pytest.raises(StartauswahlFehler):
        getattr(echt(tmp_path, FakeRun(FileNotFoundError("rauc"))), aufruf)()


@pytest.mark.parametrize("aufruf", ["probestart", "bestaetigen", "zurueckschalten", "compatible"])
def test_ueberschrittene_frist_von_60_s_ist_ein_startauswahlfehler(tmp_path, aufruf):
    run = FakeRun(subprocess.TimeoutExpired("x", 60))
    with pytest.raises(StartauswahlFehler, match="60"):
        getattr(echt(tmp_path, run), aufruf)()


BUNDLE = Path("/var/lib/smartheat/bundles/x.raucb")
BUSCTL = ["busctl", "get-property", "de.pengutronix.rauc", "/", "de.pengutronix.rauc.Installer", "Operation"]


def test_installieren_ruft_rauc_install_mit_frist_1800_s(tmp_path):
    run = FakeRun()
    echt(tmp_path, run).installieren(BUNDLE)
    assert run.aufrufe == [(["rauc", "install", str(BUNDLE)], 1800)]


def test_installieren_mit_exit_1_ist_ein_fehler_ohne_warten(tmp_path):
    run, uhr = FakeRun(1), Uhr()
    with pytest.raises(StartauswahlFehler, match="Fehlermeldung"):
        echt(tmp_path, run, uhr).installieren(BUNDLE)
    assert len(run.aufrufe) == 1
    assert uhr.pausen == []


def test_installieren_nach_abgelaufener_frist_wartet_auf_idle_und_meldet_dann_einen_fehler(tmp_path):
    run = FakeRun(subprocess.TimeoutExpired("rauc", 1800), 's "installing"\n', 's "installing"\n', 's "idle"\n')
    uhr = Uhr()
    with pytest.raises(StartauswahlFehler, match="Ergebnis"):
        echt(tmp_path, run, uhr).installieren(BUNDLE)
    assert run.aufrufe == [(["rauc", "install", str(BUNDLE)], 1800), (BUSCTL, 60), (BUSCTL, 60), (BUSCTL, 60)]
    assert len(uhr.pausen) == 2 and all(p > 0 for p in uhr.pausen)


def test_installieren_wartet_nach_der_frist_hoechstens_weitere_1800_s(tmp_path):
    antworten = [subprocess.TimeoutExpired("rauc", 1800)] + ['s "installing"\n'] * 1000
    run, uhr = FakeRun(*antworten), Uhr()
    with pytest.raises(StartauswahlFehler, match="1800"):
        echt(tmp_path, run, uhr).installieren(BUNDLE)
    assert 1800 <= uhr.jetzt < 1800 + max(uhr.pausen)
    assert len(run.aufrufe) > 10  # es wurde wirklich gefragt, nicht nach dem ersten Mal aufgegeben


def test_installieren_nach_abgelaufener_frist_ohne_dienst_ist_ein_fehler(tmp_path):
    run = FakeRun(subprocess.TimeoutExpired("rauc", 1800), 1)
    with pytest.raises(StartauswahlFehler, match="Operation"):
        echt(tmp_path, run).installieren(BUNDLE)


# --- FakeStartauswahl ---

def test_fake_probestart_startet_einmalig_den_anderen_slot_und_der_naechste_start_den_primaeren():
    fake = FakeStartauswahl(gestartet="A")
    assert not fake.im_probestart()
    fake.probestart()
    assert fake.neustart_angefordert
    fake.neustart()
    assert (fake.gestartet(), fake.primaer(), fake.im_probestart()) == ("B", "A", True)
    fake.neustart()  # Strom weg: tryboot war einmalig
    assert (fake.gestartet(), fake.primaer(), fake.im_probestart()) == ("A", "A", False)


def test_fake_bestaetigen_macht_den_gestarteten_slot_primaer():
    fake = FakeStartauswahl(gestartet="A")
    fake.probestart()
    fake.neustart()
    fake.bestaetigen()
    assert (fake.primaer(), fake.im_probestart()) == ("B", False)
    fake.neustart()
    assert fake.gestartet() == "B"


def test_fake_zurueckschalten_im_probestart_startet_den_alten_slot():
    fake = FakeStartauswahl(gestartet="A")
    fake.probestart()
    fake.neustart()
    fake.zurueckschalten()
    assert fake.neustart_angefordert and fake.primaer() == "A"
    fake.neustart()
    assert fake.gestartet() == "A"


def test_fake_zurueckschalten_nach_bestaetigung_faellt_auf_den_anderen_slot_zurueck():
    fake = FakeStartauswahl(gestartet="A")
    fake.probestart()
    fake.neustart()
    fake.bestaetigen()
    fake.zurueckschalten()
    fake.neustart()
    assert (fake.gestartet(), fake.primaer()) == ("A", "A")


def test_fake_normal_neustarten_startet_den_primaeren_slot_ohne_tryboot():
    fake = FakeStartauswahl(gestartet="A")
    fake.probestart()
    fake.neustart()  # Probestart in B
    fake.normal_neustarten()
    fake.neustart()
    assert (fake.gestartet(), fake.primaer()) == ("A", "A")


def test_fake_merkt_aufrufe_und_loest_fehler_aus():
    fake = FakeStartauswahl()
    fake.fehler["bestaetigen"] = StartauswahlFehler("kaputt")
    fake.installieren(BUNDLE)
    with pytest.raises(StartauswahlFehler):
        fake.bestaetigen()
    assert fake.aufrufe == [("installieren", BUNDLE), ("bestaetigen",)]
    fake.installieren_fehler = StartauswahlFehler("kaputt")
    with pytest.raises(StartauswahlFehler):
        fake.installieren(BUNDLE)
    fake.installieren(BUNDLE)  # der Fehler gilt einmal
