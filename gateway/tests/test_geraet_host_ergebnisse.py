"""Software-Ergebnisse und LED-Datei des GatewayDeviceHost (Testoffensive T5', Task 8, Teil A): was die Software-Bruecke
aus software_meldungen.json macht (Grund, uebergangene Eintraege) und wann die LED-Datei neu geschrieben wird. Eigene
Datei, weil test_geraet_host.py mit dem Umbau von T4' wechselt; die Hilfen sind hier klein nachgebaut."""
import json
from datetime import UTC, datetime

import pytest
from fakes import FakeBus

from smartheat_core import dateien
from smartheat_device.host import Anzeige
from smartheat_gateway import geraet_host as gh
from smartheat_gateway.paths import Paths
from smartheat_gateway.zigbee import ZigbeeMirror

WALL = 1_800_000_000.0


class GeraetAttrappe:
    """Nimmt nur ergebnis_software entgegen und haelt fest, was der Host dem Geraetekern uebergibt."""

    def __init__(self) -> None:
        self.ergebnisse: list[tuple] = []

    def ergebnis_software(self, version, ok, grund=None):
        self.ergebnisse.append((version, ok, grund))
        return True


class Welt:
    def __init__(self, tmp_path, clock) -> None:
        self.paths = Paths(tmp_path / "data")
        self.clock = clock
        self.wall = {"t": WALL}
        self.host = gh.GatewayDeviceHost(self.paths, ZigbeeMirror(FakeBus(), clock), host_dir=tmp_path / "host",
                                         clock=clock, wall=lambda: self.wall["t"])
        self.device = GeraetAttrappe()
        self.host.verbinden(self.device)

    def meldungen(self, eintraege: list) -> None:
        dateien.schreiben_json(self.paths.software_meldungen, {"meldungen": eintraege}, modus=dateien.LESBAR)

    def led(self) -> dict:
        return json.loads(self.paths.agent_state.read_text())


@pytest.fixture
def welt(tmp_path, clock):
    return Welt(tmp_path, clock)


def _anzeige(zustand="regelt"):
    return Anzeige(zustand=zustand, server_ok=True, device_state="uebernommen", eingerichtet=True, hinweise=(),
                   meldungen=(), konfiguration_abgelehnt=None)


def _weiter(welt: Welt, sekunden: float) -> None:
    """Monotone Uhr und Wanduhr gemeinsam vorstellen (ts der LED-Datei kommt von der Wanduhr)."""
    welt.clock.advance(sekunden)
    welt.wall["t"] += sekunden


def _iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, UTC).isoformat()


# --- F-gateway-host-30: Grund eines Rueckwegs ---

BEKANNTE_GRUENDE = ("manifest_ungueltig", "signatur_ungueltig", "updater_zu_alt", "version_zu_alt",
                    "start_fehlgeschlagen", "ungesund", "server_unerreichbar")


@pytest.mark.parametrize("grund", BEKANNTE_GRUENDE)
def test_a_known_rollback_reason_goes_up_unchanged(welt, grund):
    welt.meldungen([{"seq": 1, "version": "0.6.2", "result": "rollback", "reason": grund}])
    welt.host.anzeigen(_anzeige())
    assert welt.device.ergebnisse == [("0.6.2", False, grund)]


@pytest.mark.parametrize("reason", ["boese", "UNGESUND", "ungesund ", "", "plattenfehler", 5, None, ["ungesund"]])
def test_a_rollback_with_an_unknown_reason_goes_up_as_ungesund(welt, reason):
    welt.meldungen([{"seq": 1, "version": "0.6.2", "result": "rollback", "reason": reason}])
    welt.host.anzeigen(_anzeige())
    assert welt.device.ergebnisse == [("0.6.2", False, "ungesund")]
    assert json.loads(welt.paths.software_quittung.read_text()) == {"seq": 1}  # nicht haengen geblieben


def test_a_rollback_without_a_reason_goes_up_as_ungesund(welt):
    welt.meldungen([{"seq": 1, "version": "0.6.2", "result": "rollback"}])
    welt.host.anzeigen(_anzeige())
    assert welt.device.ergebnisse == [("0.6.2", False, "ungesund")]


def test_a_successful_result_carries_no_reason_even_if_one_is_present(welt):
    welt.meldungen([{"seq": 1, "version": "0.6.2", "result": "ok", "reason": "boese"}])
    welt.host.anzeigen(_anzeige())
    assert welt.device.ergebnisse == [("0.6.2", True, None)]


# --- F-gateway-host-30: uebergangene Eintraege ---

UEBERGANGEN = [
    pytest.param({"seq": 1, "version": "0.6.1", "result": "fehler"}, id="result-unbekannt"),
    pytest.param({"seq": 1, "version": "0.6.1", "result": "OK"}, id="result-gross"),
    pytest.param({"seq": 1, "version": "0.6.1", "result": ""}, id="result-leer"),
    pytest.param({"seq": 1, "version": "0.6.1", "result": None}, id="result-none"),
    pytest.param({"seq": 1, "version": "0.6.1"}, id="result-fehlt"),
    pytest.param({"seq": 1, "result": "ok"}, id="version-fehlt"),
    pytest.param({"seq": 1, "version": 61, "result": "ok"}, id="version-keine-zeichenkette"),
    pytest.param({"seq": 1, "version": None, "result": "rollback", "reason": "ungesund"}, id="version-none"),
]


@pytest.mark.parametrize("eintrag", UEBERGANGEN)
def test_an_entry_with_an_unknown_result_or_without_a_version_is_skipped(welt, eintrag):
    """Der Eintrag davor und der danach gehen hinaus; dazwischen wird nichts gesendet und nichts quittiert."""
    welt.meldungen([{"seq": 0, "version": "0.6.0", "result": "ok"}, eintrag,
                    {"seq": 2, "version": "0.6.2", "result": "ok"}])
    welt.host.anzeigen(_anzeige())
    assert welt.device.ergebnisse == [("0.6.2", True, None)]  # seq 0 liegt nicht ueber der Quittung 0
    assert json.loads(welt.paths.software_quittung.read_text()) == {"seq": 2}


def test_a_skipped_entry_does_not_hold_back_later_ones_and_is_never_acknowledged_itself(welt, clock):
    welt.meldungen([{"seq": 1, "version": "0.6.1", "result": "fehler"}])
    welt.host.anzeigen(_anzeige())
    assert welt.device.ergebnisse == []
    assert not welt.paths.software_quittung.exists()
    welt.meldungen([{"seq": 1, "version": "0.6.1", "result": "fehler"},
                    {"seq": 2, "version": "0.6.2", "result": "rollback", "reason": "start_fehlgeschlagen"}])
    clock.advance(gh.SOFTWARE_SECONDS)
    welt.host.anzeigen(_anzeige())
    assert welt.device.ergebnisse == [("0.6.2", False, "start_fehlgeschlagen")]
    assert json.loads(welt.paths.software_quittung.read_text()) == {"seq": 2}


# --- F-gateway-host-30: LED-Datei mindestens jede Minute ---

def test_an_unchanged_state_rewrites_the_led_file_after_sixty_seconds_not_before(welt):
    welt.host.anzeigen(_anzeige("regelt"))
    erste = welt.led()
    assert erste == {"zustand": "regelt", "seit": _iso(WALL), "ts": _iso(WALL)}
    _weiter(welt, 59)
    welt.host.anzeigen(_anzeige("regelt"))
    assert welt.led() == erste  # 59 s: unveraendert, auch ts
    _weiter(welt, 1)
    welt.host.anzeigen(_anzeige("regelt"))
    assert welt.led() == {"zustand": "regelt", "seit": _iso(WALL), "ts": _iso(WALL + 60)}  # 60 s: neu, seit bleibt


def test_the_led_minute_counts_from_the_last_write_not_from_the_first(welt):
    welt.host.anzeigen(_anzeige("regelt"))
    _weiter(welt, 60)
    welt.host.anzeigen(_anzeige("regelt"))  # zweites Schreiben bei 60 s
    _weiter(welt, 59)
    welt.host.anzeigen(_anzeige("regelt"))
    assert welt.led()["ts"] == _iso(WALL + 60)  # 59 s nach dem letzten Schreiben: noch nicht
    _weiter(welt, 1)
    welt.host.anzeigen(_anzeige("regelt"))
    assert welt.led()["ts"] == _iso(WALL + 120)


def test_the_led_file_stays_younger_than_a_minute_plus_one_tick_while_the_state_stays_the_same(welt):
    """Der LED-Dienst zeigt eine Datei aelter als 5 min als stoerung: im Takt von 10 s ist ts nie aelter als 60 s."""
    welt.host.anzeigen(_anzeige("regelt"))
    for _ in range(60):  # 10 Minuten
        _weiter(welt, 10)
        welt.host.anzeigen(_anzeige("regelt"))
        assert welt.wall["t"] - datetime.fromisoformat(welt.led()["ts"]).timestamp() <= 50
