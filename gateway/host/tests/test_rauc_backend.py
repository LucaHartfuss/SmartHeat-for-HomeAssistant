"""RAUC-Backend auf tryboot: je Zeile der Tabelle (Befehl, Bedingung, Wirkung) ein Test, dazu die Folgen eines
Systemwechsels ueber das Backend. Lage: gestartet = DT-Datei partition, primaer = [all] boot_partition."""
from pathlib import Path

import pytest

from smartheat_host import rauc_backend

KANONISCH = "[all]\ntryboot_a_b=1\nboot_partition={p}\n[tryboot]\nboot_partition={t}\n"
PARTITION = {"A": 2, "B": 3}


def autoboot_text(primaer: str) -> str:
    andere = "B" if primaer == "A" else "A"
    return KANONISCH.format(p=PARTITION[primaer], t=PARTITION[andere])


class Lage:
    """Ein Geraet im Temp-Ordner: autoboot.txt und die Device-Tree-Datei des gestarteten Slots."""

    def __init__(self, tmp_path: Path, gestartet: str, primaer: str) -> None:
        self.autoboot = tmp_path / "bootfs" / "autoboot.txt"
        self.dt = tmp_path / "dt"
        self.autoboot.parent.mkdir()
        self.autoboot.write_text(autoboot_text(primaer))
        self.starte(gestartet)

    def starte(self, gestartet: str) -> None:
        """Neustart: nur die gestartete Partition aendert sich (der DT-Merker tryboot bleibt unbeachtet)."""
        self.dt.mkdir(exist_ok=True)
        (self.dt / "partition").write_bytes(PARTITION[gestartet].to_bytes(4, "big"))
        (self.dt / "tryboot").write_bytes((1).to_bytes(4, "big"))

    def rauf(self, *argv: str) -> int:
        return rauc_backend.main(list(argv), autoboot=self.autoboot, dt=self.dt)

    @property
    def primaer(self) -> str:
        zeilen = self.autoboot.read_text().splitlines()
        return "A" if zeilen[2] == "boot_partition=2" else "B"


@pytest.fixture
def lage(tmp_path):
    def bauen(gestartet: str, primaer: str) -> Lage:
        return Lage(tmp_path, gestartet, primaer)
    return bauen


# --- get-* ---

@pytest.mark.parametrize("primaer", ["A", "B"])
def test_get_primary_nennt_den_slot_von_all(lage, capsys, primaer):
    assert lage("A", primaer).rauf("get-primary") == 0
    assert capsys.readouterr().out == f"{primaer}\n"


def test_get_primary_liest_all_und_nicht_tryboot(lage, capsys):
    """[tryboot] nennt immer den anderen Slot; wuerde das Backend ihn lesen, kaeme B statt A."""
    assert lage("B", "A").rauf("get-primary") == 0
    assert capsys.readouterr().out == "A\n"


@pytest.mark.parametrize("gestartet", ["A", "B"])
def test_get_current_nennt_den_gestarteten_slot(lage, capsys, gestartet):
    assert lage(gestartet, "A" if gestartet == "B" else "B").rauf("get-current") == 0
    assert capsys.readouterr().out == f"{gestartet}\n"


def test_get_state_ist_good_fuer_den_primaeren_slot(lage, capsys):
    assert lage("B", "A").rauf("get-state", "A") == 0
    assert capsys.readouterr().out == "good\n"


def test_get_state_ist_bad_fuer_den_anderen_slot(lage, capsys):
    """Auch der gestartete, noch nicht bestaetigte Slot ist bad (Probestart)."""
    assert lage("B", "A").rauf("get-state", "B") == 0
    assert capsys.readouterr().out == "bad\n"


# --- set-state ---

@pytest.mark.parametrize("slot", ["A", "B"])
def test_set_state_good_auf_dem_primaeren_slot_aendert_nichts(lage, slot):
    g = lage(slot, slot)
    g.autoboot.write_text(autoboot_text(slot) + "# Kommentar bleibt\n")
    vorher = g.autoboot.read_bytes()
    assert g.rauf("set-state", slot, "good") == 0
    assert g.autoboot.read_bytes() == vorher


@pytest.mark.parametrize("slot", ["A", "B"])
def test_set_state_good_im_probestart_bestaetigt(lage, slot):
    andere = "B" if slot == "A" else "A"
    g = lage(slot, andere)
    assert g.rauf("set-state", slot, "good") == 0
    assert g.autoboot.read_text() == autoboot_text(slot)


def test_set_state_good_fuer_einen_slot_weder_gestartet_noch_primaer_scheitert(lage, capsys):
    g = lage("A", "A")
    g.autoboot.write_text(autoboot_text("A") + "# bleibt\n")
    vorher = g.autoboot.read_bytes()
    assert g.rauf("set-state", "B", "good") == 1
    assert g.autoboot.read_bytes() == vorher
    assert capsys.readouterr().err.strip() != ""


@pytest.mark.parametrize(("gestartet", "primaer"), [("A", "A"), ("B", "A"), ("A", "B"), ("B", "B")])
def test_set_state_bad_auf_einem_nicht_primaeren_slot_aendert_nichts(lage, gestartet, primaer):
    """Folge von rauc install (set-state <Ziel> bad), egal ob bestaetigt oder im Probestart."""
    g = lage(gestartet, primaer)
    g.autoboot.write_text(autoboot_text(primaer) + "# bleibt\n")
    vorher = g.autoboot.read_bytes()
    nicht_primaer = "B" if primaer == "A" else "A"
    assert g.rauf("set-state", nicht_primaer, "bad") == 0
    assert g.autoboot.read_bytes() == vorher


@pytest.mark.parametrize("slot", ["A", "B"])
def test_set_state_bad_auf_dem_primaeren_gestarteten_slot_faellt_zurueck(lage, slot):
    andere = "B" if slot == "A" else "A"
    g = lage(slot, slot)
    assert g.rauf("set-state", slot, "bad") == 0
    assert g.autoboot.read_text() == autoboot_text(andere)


def test_set_state_bad_auf_dem_primaeren_nicht_gestarteten_slot_scheitert(lage):
    g = lage("B", "A")
    g.autoboot.write_text(autoboot_text("A") + "# bleibt\n")
    vorher = g.autoboot.read_bytes()
    assert g.rauf("set-state", "A", "bad") == 1
    assert g.autoboot.read_bytes() == vorher


# --- set-primary ---

def test_set_primary_auf_den_primaeren_slot_aendert_nichts(lage):
    g = lage("A", "A")
    g.autoboot.write_text(autoboot_text("A") + "# bleibt\n")
    vorher = g.autoboot.read_bytes()
    assert g.rauf("set-primary", "A") == 0
    assert g.autoboot.read_bytes() == vorher


@pytest.mark.parametrize(("gestartet", "slot"), [("A", "B"), ("B", "B"), ("B", "A")])
def test_set_primary_sonst_scheitert_ohne_zu_schreiben(lage, gestartet, slot):
    g = lage(gestartet, "A" if slot == "B" else "B")
    g.autoboot.write_text(g.autoboot.read_text() + "# bleibt\n")
    vorher = g.autoboot.read_bytes()
    assert g.rauf("set-primary", slot) == 1
    assert g.autoboot.read_bytes() == vorher


# --- Folgen eines Systemwechsels ---

def test_install_folge_set_state_bad_laesst_autoboot_unveraendert(lage):
    """Bestaetigt in A: rauc install schreibt B und ruft nur set-state B bad (activate-installed=false)."""
    g = lage("A", "A")
    g.autoboot.write_text(autoboot_text("A") + "# bleibt\n")
    vorher = g.autoboot.read_bytes()
    assert g.rauf("set-state", "B", "bad") == 0
    assert g.autoboot.read_bytes() == vorher


def test_probestart_bestaetigen_und_ruecktritt_im_selben_start(lage):
    g = lage("A", "A")
    assert g.rauf("set-state", "B", "bad") == 0  # rauc install
    g.starte("B")  # Neustart mit tryboot: Firmware startet [tryboot] = B, der DT-Merker tryboot bleibt 1
    assert g.rauf("get-state", "B") == 0
    assert g.rauf("set-state", "B", "good") == 0  # mark-good
    assert g.autoboot.read_text() == autoboot_text("B")
    # Netzprobe scheitert im selben Start (der DT-Merker tryboot ist noch 1): mark-bad
    assert g.rauf("set-state", "B", "bad") == 0
    assert g.autoboot.read_text() == autoboot_text("A")


def test_zweites_mark_good_nach_stromausfall_ist_idempotent(lage):
    g = lage("B", "A")
    assert g.rauf("set-state", "B", "good") == 0
    nach_dem_ersten = g.autoboot.read_bytes()
    assert g.rauf("set-state", "B", "good") == 0  # Strom weg zwischen mark-good und Speichern: nochmal
    assert g.autoboot.read_bytes() == nach_dem_ersten
    assert g.primaer == "B"


def test_rueckschalten_im_probestart_mark_bad_aendert_autoboot_nicht(lage):
    """mark-bad im Probestart (B gestartet, A primaer): set-state B bad; der anschliessende Neustart ohne tryboot
    startet [all] = A."""
    g = lage("B", "A")
    vorher = g.autoboot.read_bytes()
    assert g.rauf("set-state", "B", "bad") == 0
    assert g.autoboot.read_bytes() == vorher


# --- Fehlerfaelle: kein Schreiben ---

@pytest.fixture
def schreibt_nie(monkeypatch):
    """Jede Schreibfunktion des Backends wuerde hier auffallen."""
    def verboten(*args, **kwargs):
        raise AssertionError("autoboot.txt darf nicht geschrieben werden")
    monkeypatch.setattr(rauc_backend.startauswahl, "schreibe_primaer", verboten)


def test_fehlende_autoboot_datei_gibt_exit_1(lage, schreibt_nie, capsys):
    g = lage("B", "A")
    g.autoboot.unlink()
    assert g.rauf("set-state", "B", "good") == 1
    assert "autoboot" in capsys.readouterr().err
    assert not g.autoboot.exists()


@pytest.mark.parametrize("text", [
    "[all]\ntryboot_a_b=1\nboot_partition=4\n[tryboot]\nboot_partition=3\n",  # Partition 4
    "[all]\ntryboot_a_b=1\nboot_partition=zwei\n[tryboot]\nboot_partition=3\n",
    "[tryboot]\nboot_partition=3\n",  # kein [all]
    "[all]\ntryboot_a_b=1\n[tryboot]\nboot_partition=3\n",  # kein boot_partition in [all]
    "",
])
def test_unbrauchbare_autoboot_datei_gibt_exit_1_ohne_schreiben(lage, schreibt_nie, text):
    g = lage("B", "A")
    g.autoboot.write_text(text)
    assert g.rauf("set-state", "B", "good") == 1
    assert g.autoboot.read_text() == text


@pytest.mark.parametrize("inhalt", [(1).to_bytes(4, "big"), (4).to_bytes(4, "big"), b"\x00\x00\x02", b""])
def test_unbrauchbare_dt_datei_gibt_exit_1_ohne_schreiben(lage, schreibt_nie, inhalt):
    g = lage("B", "A")
    vorher = g.autoboot.read_bytes()
    (g.dt / "partition").write_bytes(inhalt)
    assert g.rauf("set-state", "B", "good") == 1
    assert g.rauf("get-current") == 1
    assert g.autoboot.read_bytes() == vorher


def test_fehlende_dt_datei_gibt_exit_1_ohne_schreiben(lage, schreibt_nie):
    g = lage("B", "A")
    (g.dt / "partition").unlink()
    assert g.rauf("set-state", "B", "good") == 1
    assert g.rauf("get-current") == 1


def test_fehlende_dt_datei_stoert_get_primary_und_nicht_gestartete_befehle_nicht(lage, capsys):
    """Gelesen wird nur, was der Befehl braucht: get-primary und get-state kommen ohne die DT-Datei aus."""
    g = lage("B", "A")
    (g.dt / "partition").unlink()
    assert g.rauf("get-primary") == 0
    assert g.rauf("get-state", "A") == 0
    assert capsys.readouterr().out == "A\ngood\n"


@pytest.mark.parametrize("argv", [
    [],
    ["unbekannt"],
    ["get-primary", "A"],
    ["get-current", "A"],
    ["get-state"],
    ["get-state", "C"],
    ["get-state", "a"],
    ["set-state", "A"],
    ["set-state", "A", "gut"],
    ["set-state", "C", "good"],
    ["set-state", "A", "good", "mehr"],
    ["set-primary"],
    ["set-primary", "C"],
])
def test_ungueltige_aufrufe_geben_exit_1_ohne_zu_schreiben(lage, schreibt_nie, argv):
    g = lage("B", "A")
    vorher = g.autoboot.read_bytes()
    assert g.rauf(*argv) == 1
    assert g.autoboot.read_bytes() == vorher


# --- Aufruf als Programm ---

def test_wrapper_ruft_das_modul_des_host_pakets():
    wrapper = Path(rauc_backend.__file__).resolve().parents[1] / "rauc-backend"
    assert wrapper.read_text().splitlines() == [
        "#!/bin/sh",
        'PYTHONPATH=/opt/smartheat/host exec python3 -m smartheat_host.rauc_backend "$@"',
    ]
    assert wrapper.stat().st_mode & 0o111 == 0o111
