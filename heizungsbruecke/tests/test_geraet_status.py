"""Spec 5c 1.1/1.3: Status-Ereignis Schema 3 (Laufzeit-Status Schema 2 plus Geraet, wirksamer Soll, Hinweise)."""
import logging
import threading
import time

from conftest import FakeClock
from fakes import FakeHa
from geraet_helpers import konfiguration_aus_optionen
from test_geraet_host import Device, _host, _register
from test_runtime import OPTIONS

from heizungsbruecke import geraet_status
from smartheat_device.commands import Done, Waiting
from smartheat_device.host import Anzeige
from smartheat_runtime import status
from smartheat_runtime.status import STATUS_ABGEMELDET

GERAET_ID = "shg-aaaaaaaaaaaaaaaa"
CODE = "ABCD-1234-SECRET"


def _anzeige(**changes):
    base = dict(zustand="regelt", server_ok=True, device_state="uebernommen", eingerichtet=True, hinweise=(),
                meldungen=(), konfiguration_abgelehnt=None)
    return Anzeige(**{**base, **changes})


def _status(ha, soll=21.5):
    seen = []
    return geraet_status.GeraetStatus(ha, GERAET_ID, lambda: soll, laufzeit_merken=seen.append), seen


def _runtime_event(**changes):
    return {key: None for key in status.EVENT_FIELDS} | {"schema": 2, "status": "regelt"} | changes


def test_event_carries_runtime_and_device_fields():
    ha = FakeHa()
    sink, seen = _status(ha)
    sink.publish(_runtime_event())
    sink.anzeige(_anzeige())
    event_type, data = ha.events[-1]
    assert event_type == "smartheat_status" and data["schema"] == 3 and set(data) == set(geraet_status.EVENT_FIELDS)
    assert (data["geraet_id"], data["geraet_zustand"], data["server_verbunden"], data["raum_soll"]) == (
        GERAET_ID, "regelt", True, 21.5)
    assert seen and seen[-1]["status"] == "regelt"


def test_event_before_any_input_is_a_valid_starting_snapshot():
    ha = FakeHa()
    sink, _ = _status(ha)
    sink.anzeige(_anzeige(zustand="startet", server_ok=False))
    data = ha.events[-1][1]
    assert set(data) == set(geraet_status.EVENT_FIELDS) and data["status"] == "startet" and data["schema"] == 3
    assert data["geraet_zustand"] == "startet" and data["server_verbunden"] is False


def test_unchanged_display_sends_nothing_new():
    ha = FakeHa()
    sink, _ = _status(ha)
    sink.anzeige(_anzeige())
    count = len(ha.events)
    sink.anzeige(_anzeige())
    assert len(ha.events) == count


def test_changed_display_or_effective_target_sends_again_and_every_runtime_event_sends():
    ha = FakeHa()
    soll = [21.5]
    sink = geraet_status.GeraetStatus(ha, GERAET_ID, lambda: soll[0], laufzeit_merken=lambda e: None)
    sink.anzeige(_anzeige())
    sink.anzeige(_anzeige(server_ok=False))
    soll[0] = 19.0
    sink.anzeige(_anzeige(server_ok=False))  # Soll geaendert: inhaltliche Aenderung des Ereignisses
    assert [d["server_verbunden"] for _, d in ha.events] == [True, False, False]
    assert ha.events[-1][1]["raum_soll"] == 19.0
    sink.publish(_runtime_event())
    sink.publish(_runtime_event())  # Lebenszeichen: auch ohne Aenderung
    assert len(ha.events) == 5


def test_hints_messages_rejection_and_update_become_device_hints():
    ha = FakeHa()
    sink, _ = _status(ha)
    sink.anzeige(_anzeige(zustand="update_noetig", hinweise=({"key": "geraet_doppelt", "stufe": "kritisch",
                                                               "text": "läuft doppelt"},),
                          meldungen=({"key": "wunsch_begrenzt", "text": "15 °C"},),
                          konfiguration_abgelehnt="Entity fehlt"))
    keys = {h["key"]: h for h in ha.events[-1][1]["geraet_hinweise"]}
    # update_noetig mit abgelehnter Konfiguration ergibt bewusst zwei Hinweise (Grund und Handlungsanweisung)
    assert set(keys) == {"geraet_doppelt", "wunsch_begrenzt", "konfiguration_abgelehnt", "update_noetig"}
    assert all(set(h) == set(geraet_status.HINWEIS_FIELDS) for h in keys.values())
    assert keys["konfiguration_abgelehnt"]["text"] == "Entity fehlt"
    assert keys["geraet_doppelt"]["stufe"] == "kritisch" and keys["wunsch_begrenzt"]["stufe"] == "warnung"


def test_locked_device_gets_a_critical_hint_and_malformed_entries_are_dropped():
    ha = FakeHa()
    sink, _ = _status(ha)
    sink.anzeige(_anzeige(zustand="gesperrt", hinweise=("kaputt", {"text": "ohne key"}), meldungen=({"text": "x"},)))
    hints = ha.events[-1][1]["geraet_hinweise"]
    assert [h["key"] for h in hints] == ["gesperrt"] and hints[0]["stufe"] == "kritisch"


def test_status_event_never_carries_the_claim_code(caplog):
    """Der Code steht in Quellen, die ein ganzes Objekt tragen koennten (Hinweis- und Meldungs-Dicts mit Zusatzfeld,
    Laufzeit-Ereignis mit Zusatzfeld): ins Ereignis kommen nur die benannten Felder, im Log steht er nie."""
    ha = FakeHa()
    sink, seen = _status(ha)
    with caplog.at_level(logging.DEBUG):
        sink.publish(_runtime_event(claim_code=CODE))
        sink.anzeige(_anzeige(
            device_state="nicht_uebernommen", eingerichtet=False, zustand="nicht_uebernommen",
            hinweise=({"key": "geraet_doppelt", "stufe": "kritisch", "text": "x", "claim_code": CODE},),
            meldungen=({"key": "wunsch_begrenzt", "text": "y", "claim_code": CODE},)))
    assert ha.events and all(CODE not in str(data) for _, data in ha.events)
    assert "claim" not in str(ha.events[-1][1]).lower() and CODE not in caplog.text
    assert seen[-1]["claim_code"] == CODE  # nur das Rohereignis fuer sign_off bleibt unveraendert (nie gesendet)


class _SlowHa(FakeHa):
    """fire_event dauert und merkt sich Ueberlappung: ohne Sperre sendeten zwei Threads gleichzeitig."""

    def __init__(self):
        super().__init__()
        self.in_flight = self.max_in_flight = 0
        self._guard = threading.Lock()

    def fire_event(self, event_type, data):
        with self._guard:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        time.sleep(0.002)
        super().fire_event(event_type, data)
        with self._guard:
            self.in_flight -= 1


def test_threads_do_not_interleave_events():
    """Geraete-Thread (anzeige) und Worker-Thread (publish) senden nie gleichzeitig; jedes Ereignis ist ein
    stimmiger Schnappschuss (zustand und Verbindung gehoeren zur selben Anzeige, status und Anlage zum selben
    Laufzeit-Ereignis) und jedes Laufzeit-Ereignis wird gesendet."""
    ha = _SlowHa()
    sink, _ = _status(ha)
    anzeigen = {"regelt": True, "stoerung": False, "keine_verbindung": False}
    laufzeit = {"regelt": "t1", "konfigurationsfehler": "t2"}
    runs = 25

    def anzeige_loop():
        for i in range(runs):
            zustand = list(anzeigen)[i % len(anzeigen)]
            sink.anzeige(_anzeige(zustand=zustand, server_ok=anzeigen[zustand]))

    def publish_loop():
        for i in range(runs):
            name = list(laufzeit)[i % len(laufzeit)]
            sink.publish(_runtime_event(status=name, tenant_id=laufzeit[name]))

    threads = [threading.Thread(target=anzeige_loop), threading.Thread(target=publish_loop)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert ha.max_in_flight == 1
    assert len(ha.events) >= runs  # mindestens jedes Laufzeit-Ereignis (die Anzeige sendet nur bei Aenderung)
    for _, data in ha.events:
        if data["geraet_zustand"] != "startet":
            assert anzeigen[data["geraet_zustand"]] == data["server_verbunden"]
        if data["status"] != "startet":
            assert laufzeit[data["status"]] == data["tenant_id"]
    # Reihenfolge: das letzte Ereignis zeigt den Endzustand beider Schleifen
    last = ha.events[-1][1]
    assert last["geraet_zustand"] == list(anzeigen)[(runs - 1) % len(anzeigen)]
    assert last["status"] == list(laufzeit)[(runs - 1) % len(laufzeit)]


def test_runtime_event_kept_for_sign_off_completes_the_command(tmp_path, monkeypatch):
    """Verdrahtung wie in Task 9: laufzeit_merken legt das Laufzeit-Ereignis im Host ab, sign_off wartet darauf."""
    konf = konfiguration_aus_optionen(OPTIONS)
    ha = FakeHa()
    host, _, _ = _host(tmp_path, monkeypatch, device=Device(konf), ha=ha)
    sink = geraet_status.GeraetStatus(ha, GERAET_ID, lambda: None,
                                      laufzeit_merken=lambda e: setattr(host, "runtime_status_event", e))
    host.status_sink = sink
    outcome = _register(host, tmp_path)._handlers["sign_off"]({})
    assert isinstance(outcome, Waiting)
    sink.publish(_runtime_event(status="regelt", tenant_id=konf["anlage"]))
    assert outcome.check() is None  # noch angemeldet
    sink.publish(_runtime_event(status=STATUS_ABGEMELDET, tenant_id="andere_anlage"))
    assert outcome.check() is None  # fremde Anlage
    sink.publish(_runtime_event(status=STATUS_ABGEMELDET, tenant_id=konf["anlage"], grund=None))
    assert host.runtime_status_event["status"] == STATUS_ABGEMELDET
    assert isinstance(outcome.check(), Done)


# --- HA nicht erreichbar: Wiederholung mit Pause, eine Log-Zeile je Ausfall (Final-Review I2) ---


class _DownHa(FakeHa):
    """fire_event scheitert, solange down; zaehlt die Versuche."""

    def __init__(self):
        super().__init__()
        self.down, self.attempts = True, 0

    def fire_event(self, event_type, data):
        self.attempts += 1
        if self.down:
            raise ConnectionError("HA Core startet")
        super().fire_event(event_type, data)


def _down_status(ha):
    clock = FakeClock()
    sink = geraet_status.GeraetStatus(ha, GERAET_ID, lambda: 21.5, laufzeit_merken=lambda e: None, clock=clock)
    return sink, clock


def test_while_ha_is_down_the_device_tick_retries_only_after_a_pause_and_logs_once(caplog):
    """startup: services startet das Add-on vor HA Core: 120 Takte (1 s) ergeben wenige Versuche und eine Warnung."""
    ha = _DownHa()
    sink, clock = _down_status(ha)
    with caplog.at_level(logging.INFO, logger="heizungsbruecke.geraet_status"):
        for _ in range(120):
            sink.anzeige(_anzeige())  # wirft nicht
            clock.advance(1)
    assert ha.attempts <= 3 and ha.events == []
    warnungen = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnungen) == 1 and "ConnectionError" in warnungen[0].getMessage()
    assert "HA Core startet" not in caplog.text  # nur die Fehlerart
    ha.down = False
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="heizungsbruecke.geraet_status"):
        for _ in range(geraet_status.WIEDERHOLEN_SECONDS + 1):
            sink.anzeige(_anzeige())
            clock.advance(1)
    assert len(ha.events) == 1  # nach der Pause gesendet, danach unveraendert: nichts mehr
    assert [r.levelno for r in caplog.records] == [logging.INFO]  # eine Zeile zur Erholung
    assert "wieder" in caplog.records[0].getMessage()


def test_a_forced_send_during_the_pause_is_tried_at_most_once_per_interval():
    ha = _DownHa()
    sink, clock = _down_status(ha)
    sink.anzeige(_anzeige())  # erster Fehler: Pause beginnt
    assert ha.attempts == 1
    clock.advance(5)
    sink.publish(_runtime_event())  # Laufzeit-Ereignis in der Pause: ein Versuch
    clock.advance(5)
    sink.publish(_runtime_event(status="notbetrieb"))  # zweites in derselben Pause: keiner
    sink.anzeige(_anzeige())
    assert ha.attempts == 2
    ha.down = False
    clock.advance(geraet_status.WIEDERHOLEN_SECONDS)
    sink.anzeige(_anzeige())  # nach der Pause: der aktuelle Stand (mit dem letzten Laufzeit-Ereignis)
    assert ha.attempts == 3 and ha.events[-1][1]["status"] == "notbetrieb"
    sink.publish(_runtime_event(status="regelt"))  # wieder erreichbar: jedes Laufzeit-Ereignis sofort
    sink.publish(_runtime_event(status="regelt"))
    assert len(ha.events) == 3


def test_the_ha_call_runs_outside_the_state_lock():
    """Waehrend fire_event laeuft, ist der Zustand frei: laufzeit_merken (sign_off) wartet nie auf HA."""
    seen = []

    class _Pruefend(FakeHa):
        def fire_event(self, event_type, data):
            seen.append(sink._lock.acquire(blocking=False))
            sink._lock.release()
            super().fire_event(event_type, data)

    ha = _Pruefend()
    sink = geraet_status.GeraetStatus(ha, GERAET_ID, lambda: 21.5, laufzeit_merken=lambda e: None)
    sink.publish(_runtime_event())
    sink.anzeige(_anzeige())
    assert seen == [True, True]
