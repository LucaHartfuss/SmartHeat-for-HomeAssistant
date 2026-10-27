"""probestart-frist: POSIX-sh ohne Python, 20 min nach dem Start vom Timer gerufen. Gestartet != primaer (Probestart ohne
Bestaetigung, auch wenn der Updater des neuen Systems nie entscheidet) -> Journalzeile und systemctl reboot (ohne
Argument, also zurueck auf [all]); sonst nichts. Ist autoboot.txt nicht lesbar, entscheidet der DT-Merker tryboot (R26).
`sofort` (Drop-ins von emergency.service und rescue.service) trifft dieselbe Entscheidung im Notfallmodus. systemctl ist
hier ein Fake im PATH."""
import os
import subprocess
from pathlib import Path

import pytest

HOST = Path(__file__).resolve().parents[1]
SKRIPT = HOST / "probestart-frist"
KANONISCH = "[all]\ntryboot_a_b=1\nboot_partition={p}\n[tryboot]\nboot_partition={t}\n"


class Geraet:
    def __init__(self, tmp_path: Path) -> None:
        self.autoboot = tmp_path / "autoboot.txt"
        self.dt = tmp_path / "dt"
        self.dt.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "systemctl.log"
        fake = self.bin / "systemctl"
        fake.write_text(f'#!/bin/sh\necho "$@" >> "{self.log}"\n')
        fake.chmod(0o755)

    def lage(self, gestartet: int, primaer: int, tryboot: int | None = 1) -> None:
        self.autoboot.write_text(KANONISCH.format(p=primaer, t=5 - primaer))
        (self.dt / "partition").write_bytes(gestartet.to_bytes(4, "big"))
        if tryboot is not None:  # mit lesbarer autoboot.txt zaehlt der Merker nicht
            (self.dt / "tryboot").write_bytes(tryboot.to_bytes(4, "big"))

    def lauf(self, *argumente: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "SHG_AUTOBOOT": str(self.autoboot),
               "SHG_DT_BOOTLOADER": str(self.dt)}
        return subprocess.run(["sh", str(SKRIPT), *argumente], env=env, capture_output=True, text=True, timeout=30, check=False)

    def neustarts(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


@pytest.fixture
def geraet(tmp_path):
    return Geraet(tmp_path)


@pytest.mark.parametrize(("gestartet", "primaer"), [(3, 2), (2, 3)])
def test_probestart_ohne_bestaetigung_startet_genau_einmal_ohne_argument_neu(geraet, gestartet, primaer):
    geraet.lage(gestartet, primaer)
    ergebnis = geraet.lauf()
    assert ergebnis.returncode == 0
    assert geraet.neustarts() == ["reboot"]
    assert "Probestart" in ergebnis.stderr and f"Partition {gestartet}" in ergebnis.stderr


@pytest.mark.parametrize("partition", [2, 3])
def test_bestaetigter_start_loest_keinen_neustart_aus(geraet, partition):
    geraet.lage(partition, partition)
    ergebnis = geraet.lauf()
    assert ergebnis.returncode == 0
    assert geraet.neustarts() == []


def test_tryboot_merker_allein_loest_nichts_aus(geraet):
    """Nach mark-good bleibt der DT-Merker tryboot bis zum naechsten Start 1; massgeblich ist gestartet != primaer."""
    geraet.lage(3, 3)
    assert (geraet.dt / "tryboot").read_bytes() == (1).to_bytes(4, "big")
    geraet.lauf()
    assert geraet.neustarts() == []


def _kein_autoboot(g: Geraet, tryboot: int | None = 0) -> None:
    g.lage(3, 2, tryboot)
    g.autoboot.unlink()


def _autoboot_ohne_all(g: Geraet, tryboot: int | None = 0) -> None:
    g.lage(3, 2, tryboot)
    g.autoboot.write_text("[tryboot]\nboot_partition=3\n")


def _autoboot_partition_4(g: Geraet, tryboot: int | None = 0) -> None:
    g.lage(3, 2, tryboot)
    g.autoboot.write_text(KANONISCH.format(p=4, t=3))


def _kein_dt(g: Geraet) -> None:
    g.lage(3, 2)
    (g.dt / "partition").unlink()


def _dt_partition_1(g: Geraet) -> None:
    g.lage(1, 2)


def _dt_zu_kurz(g: Geraet) -> None:
    g.lage(3, 2)
    (g.dt / "partition").write_bytes(b"\x00\x03")


@pytest.mark.parametrize("zerstoere", [_kein_dt, _dt_partition_1, _dt_zu_kurz])
def test_unlesbare_partition_loest_keinen_neustart_aus_und_steht_im_journal(geraet, zerstoere):
    zerstoere(geraet)
    ergebnis = geraet.lauf()
    assert geraet.neustarts() == []
    assert ergebnis.returncode == 1
    assert "nicht lesbar" in ergebnis.stderr


@pytest.mark.parametrize("zerstoere", [_kein_autoboot, _autoboot_ohne_all, _autoboot_partition_4])
@pytest.mark.parametrize("tryboot", [0, None, "garbage", "kurz"])
def test_unlesbare_startauswahl_ohne_probestart_merker_loest_keinen_neustart_aus(geraet, zerstoere, tryboot):
    """T1-03 (b): /bootfs fehlt, aber der Merker der Firmware sagt nicht `Probestart` (0, fehlt, unlesbar): kein Neustart auf
    Verdacht, Journalzeile, Exit 1."""
    zerstoere(geraet, tryboot=None)
    if tryboot == "garbage":
        (geraet.dt / "tryboot").write_bytes(b"\x00\x00\x00\x02")
    elif tryboot == "kurz":
        (geraet.dt / "tryboot").write_bytes(b"\x01")
    elif tryboot == 0:
        (geraet.dt / "tryboot").write_bytes((0).to_bytes(4, "big"))
    ergebnis = geraet.lauf()
    assert geraet.neustarts() == []
    assert ergebnis.returncode == 1
    assert "nicht lesbar" in ergebnis.stderr and "kein Neustart" in ergebnis.stderr


@pytest.mark.parametrize("zerstoere", [_kein_autoboot, _autoboot_ohne_all, _autoboot_partition_4])
@pytest.mark.parametrize("argumente", [(), ("sofort",)])
def test_unlesbare_startauswahl_mit_probestart_merker_startet_neu(geraet, zerstoere, argumente):
    """T1-03 (b): ohne lesbare autoboot.txt sagt der DT-Merker tryboot = 1 `dieser Start war ein Probestart`: Journalzeile und
    Neustart (immer sicher: vor mark-good nimmt die Firmware [all], der alte Slot). Auch ohne lesbare Partition."""
    zerstoere(geraet, tryboot=1)
    (geraet.dt / "partition").unlink()  # der Merker genuegt allein
    ergebnis = geraet.lauf(*argumente)
    assert ergebnis.returncode == 0
    assert geraet.neustarts() == ["reboot"]
    assert "tryboot sagt Probestart" in ergebnis.stderr


def test_sofort_im_notfallmodus_startet_nur_beim_unbestaetigten_probestart_neu(geraet):
    """T1-03 (a): die Drop-ins rufen `probestart-frist sofort`; die Meldung nennt den Notfallmodus, nicht die 20 min."""
    geraet.lage(3, 2)
    ergebnis = geraet.lauf("sofort")
    assert geraet.neustarts() == ["reboot"] and "im Notfallmodus" in ergebnis.stderr
    assert "nach 20 min" not in ergebnis.stderr


@pytest.mark.parametrize("partition", [2, 3])
def test_sofort_im_bestaetigten_system_startet_nicht_neu(geraet, partition):
    """Ein bestaetigtes System im Notfallmodus (kaputter Mount) wuerde sonst endlos neu starten."""
    geraet.lage(partition, partition)
    ergebnis = geraet.lauf("sofort")
    assert ergebnis.returncode == 0 and geraet.neustarts() == []


@pytest.mark.parametrize("unit", ["emergency", "rescue"])
def test_die_notfall_dropins_rufen_die_frist_vor_der_shell(unit):
    dropin = HOST / "systemd" / f"{unit}.service.d" / "smartheat-probestart.conf"
    zeilen = [z for z in dropin.read_text().splitlines() if z and not z.startswith("#")]
    assert zeilen == ["[Service]", "ExecStartPre=-/opt/smartheat/probestart-frist sofort"]  # `-`: ein Fehler haelt die Shell nicht auf


def test_autoboot_mit_windows_zeilenenden_wird_gelesen(geraet):
    geraet.lage(3, 2)
    geraet.autoboot.write_bytes(KANONISCH.format(p=2, t=3).replace("\n", "\r\n").encode())
    geraet.lauf()
    assert geraet.neustarts() == ["reboot"]


def test_timer_laeuft_einmal_je_start_nach_20_minuten_und_ruft_den_dienst():
    timer = (HOST / "systemd" / "smartheat-probestart-frist.timer").read_text()
    dienst = (HOST / "systemd" / "smartheat-probestart-frist.service").read_text()
    assert "OnBootSec=20min" in timer
    assert "OnUnitActiveSec" not in timer and "OnCalendar" not in timer
    assert "WantedBy=timers.target" in timer
    assert "Type=oneshot" in dienst
    assert "ExecStart=/opt/smartheat/probestart-frist" in dienst
    assert "[Install]" not in dienst  # nur der Timer startet ihn


def test_skript_ist_ausfuehrbar_und_posix_sh():
    assert SKRIPT.stat().st_mode & 0o111 == 0o111
    assert SKRIPT.read_text().splitlines()[0] == "#!/bin/sh"
    assert "python" not in SKRIPT.read_text()
