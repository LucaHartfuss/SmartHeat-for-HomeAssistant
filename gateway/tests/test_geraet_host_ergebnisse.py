"""Software-Ergebnisse und LED-Datei des GatewayDeviceHost: was die Software-Bruecke
aus software_meldungen.json macht (Grund, uebergangene Eintraege) und wann die LED-Datei neu geschrieben wird. Eigene
Datei, weil test_geraet_host.py mit dem Umbau von T4' wechselt. Die Ergebnisse gehen ueber das echte Geraet (Device,
eigener Client-Kern, keine Grenze) auf einen Fake-Link und werden als up/result gelesen: das Geraet lehnt einen Grund
ausserhalb von SOFTWARE_ERRORS selbst ab. Die LED-Tests brauchen kein Geraet und bauen den Host allein."""
import json
from datetime import UTC, datetime

import pytest
from fakes import FakeBus
from world import GatewayWorld

from smartheat_core import dateien
from smartheat_device.host import Anzeige
from smartheat_gateway import geraet_host as gh
from smartheat_gateway.paths import Paths
from smartheat_gateway.zigbee import ZigbeeMirror

WALL = 1_800_000_000.0


@pytest.fixture
def world(data_dir, monkeypatch, clock):
    welt = GatewayWorld(data_dir, monkeypatch, clock)
    welt.start_runtime()
    welt.connect()  # hello gesendet: das Geraet nimmt Ergebnisse an
    yield welt
    welt.close()


def _meldungen(world, eintraege: list) -> None:
    dateien.schreiben_json(world.paths.software_meldungen, {"meldungen": eintraege}, modus=dateien.LESBAR)


def _abwarten(world) -> list[tuple]:
    """Die Bruecke prueft hoechstens alle 10 s: nach 9 s ist noch nichts gesendet, nach 10 s sind es die offenen
    Ergebnisse. Liefert alles, was bisher als up/result mit dokument software hinausging, als (version, ok, grund)."""
    vorher = _gesendet(world)
    world.advance(9)
    assert _gesendet(world) == vorher
    world.advance(1)
    return _gesendet(world)


def _gesendet(world) -> list[tuple]:
    return [(b["version"], b["ok"], b.get("error", {}).get("grund")) for b in world.link.of("up/result")
            if b["dokument"] == "software"]


def _quittung(world):
    return json.loads(world.paths.software_quittung.read_text())


# --- F-gateway-host-30: Grund eines Rueckwegs ---

BEKANNTE_GRUENDE = ("manifest_ungueltig", "signatur_ungueltig", "updater_zu_alt", "version_zu_alt",
                    "start_fehlgeschlagen", "ungesund", "server_unerreichbar")


@pytest.mark.parametrize("grund", BEKANNTE_GRUENDE)
def test_a_known_rollback_reason_goes_up_unchanged(world, grund):
    _meldungen(world, [{"seq": 1, "version": "0.6.2", "result": "rollback", "reason": grund}])
    assert _abwarten(world) == [("0.6.2", False, grund)]


@pytest.mark.parametrize("reason", ["boese", "UNGESUND", "ungesund ", "", "plattenfehler", 5, None, ["ungesund"]])
def test_a_rollback_with_an_unknown_reason_goes_up_as_ungesund(world, reason):
    _meldungen(world, [{"seq": 1, "version": "0.6.2", "result": "rollback", "reason": reason}])
    assert _abwarten(world) == [("0.6.2", False, "ungesund")]
    assert _quittung(world) == {"seq": 1}  # nicht haengen geblieben


def test_a_rollback_without_a_reason_goes_up_as_ungesund(world):
    _meldungen(world, [{"seq": 1, "version": "0.6.2", "result": "rollback"}])
    assert _abwarten(world) == [("0.6.2", False, "ungesund")]


def test_a_successful_result_carries_no_reason_even_if_one_is_present(world):
    _meldungen(world, [{"seq": 1, "version": "0.6.2", "result": "ok", "reason": "boese"}])
    assert _abwarten(world) == [("0.6.2", True, None)]
    assert "error" not in world.link.of("up/result")[-1]


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
def test_an_entry_with_an_unknown_result_or_without_a_version_is_skipped(world, eintrag):
    """Ein Eintrag mit seq 0 liegt nicht ueber der Quittung (0) und geht nicht hinaus; der uebersprungene Eintrag in
    der Mitte wird nicht gesendet, der spaetere schon, und die Quittung steht auf dessen seq."""
    _meldungen(world, [{"seq": 0, "version": "0.6.0", "result": "ok"}, eintrag,
                       {"seq": 2, "version": "0.6.2", "result": "ok"}])
    assert _abwarten(world) == [("0.6.2", True, None)]
    assert _quittung(world) == {"seq": 2}


def test_a_skipped_entry_does_not_hold_back_later_ones_and_is_not_acknowledged_when_alone(world):
    _meldungen(world, [{"seq": 1, "version": "0.6.1", "result": "fehler"}])
    assert _abwarten(world) == []
    assert not world.paths.software_quittung.exists()
    _meldungen(world, [{"seq": 1, "version": "0.6.1", "result": "fehler"},
                       {"seq": 2, "version": "0.6.2", "result": "rollback", "reason": "start_fehlgeschlagen"}])
    assert _abwarten(world) == [("0.6.2", False, "start_fehlgeschlagen")]
    assert _quittung(world) == {"seq": 2}


# --- Hilfen fuer die LED-Tests unten: Host ohne Geraet, Anzeige, Zeitsprung ---


class LedWelt:
    """Host ohne Geraet: die Anzeige schreibt nur die LED-Datei."""

    def __init__(self, tmp_path, clock) -> None:
        self.paths = Paths(tmp_path / "data")
        self.clock = clock
        self.wall = {"t": WALL}
        self.host = gh.GatewayDeviceHost(self.paths, ZigbeeMirror(FakeBus(), clock), host_dir=tmp_path / "host",
                                         clock=clock, wall=lambda: self.wall["t"])

    def led(self) -> dict:
        return json.loads(self.paths.agent_state.read_text())


@pytest.fixture
def led(tmp_path, clock):
    return LedWelt(tmp_path, clock)


def _anzeige(zustand="regelt"):
    return Anzeige(zustand=zustand, server_ok=True, device_state="uebernommen", eingerichtet=True, hinweise=(),
                   meldungen=(), konfiguration_abgelehnt=None)


def _weiter(led: LedWelt, sekunden: float) -> None:
    """Monotone Uhr und Wanduhr gemeinsam vorstellen (ts der LED-Datei kommt von der Wanduhr)."""
    led.clock.advance(sekunden)
    led.wall["t"] += sekunden


def _iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, UTC).isoformat()


# --- F-gateway-host-30: LED-Datei mindestens jede Minute ---

def test_an_unchanged_state_rewrites_the_led_file_after_sixty_seconds_not_before(led):
    led.host.anzeigen(_anzeige("regelt"))
    erste = led.led()
    assert erste == {"zustand": "regelt", "seit": _iso(WALL), "ts": _iso(WALL)}
    _weiter(led, 59)
    led.host.anzeigen(_anzeige("regelt"))
    assert led.led() == erste  # 59 s: unveraendert, auch ts
    _weiter(led, 1)
    led.host.anzeigen(_anzeige("regelt"))
    assert led.led() == {"zustand": "regelt", "seit": _iso(WALL), "ts": _iso(WALL + 60)}  # 60 s: neu, seit bleibt


def test_the_led_minute_counts_from_the_last_write_not_from_the_first(led):
    led.host.anzeigen(_anzeige("regelt"))
    _weiter(led, 60)
    led.host.anzeigen(_anzeige("regelt"))  # zweites Schreiben bei 60 s
    _weiter(led, 59)
    led.host.anzeigen(_anzeige("regelt"))
    assert led.led()["ts"] == _iso(WALL + 60)  # 59 s nach dem letzten Schreiben: noch nicht
    _weiter(led, 1)
    led.host.anzeigen(_anzeige("regelt"))
    assert led.led()["ts"] == _iso(WALL + 120)


def test_the_led_timestamp_never_reaches_a_minute_of_age_at_a_ten_second_tick(led):
    """Bei einer Anzeige alle 10 s ist ts nie 60 s alt oder aelter, solange der Zustand gleich bleibt (der LED-Dienst
    zeigt eine Datei aelter als 5 min als stoerung)."""
    led.host.anzeigen(_anzeige("regelt"))
    for _ in range(60):  # 10 Minuten
        _weiter(led, 10)
        led.host.anzeigen(_anzeige("regelt"))
        assert led.wall["t"] - datetime.fromisoformat(led.led()["ts"]).timestamp() < 60
