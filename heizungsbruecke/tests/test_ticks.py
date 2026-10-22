"""Tick-Zustellung (ticks.py) auf Einheitsebene; der volle Ablauf steht in test_runtime.py."""
from types import SimpleNamespace

import pytest
from bindung_beispiele import VAILLANT_MYPYLLANT
from fakes import FakeHa, FakeMqttChannel, FakeNotifySink

from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core.pipeline import DeviceWriteError, LeverPipeline
from smartheat_core.safety import LocalSafety
from smartheat_runtime import delivery, ticks
from smartheat_runtime.notifier import Notifier
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.texts import HA_TEXTS

SETTLE = VAILLANT_MYPYLLANT.settle_seconds


def _safety(heat_limit_max=20.0):
    return LocalSafety(
        ranges={"curve": (0.4, 1.5), "room_setpoint": (15.0, 25.0), "heat_limit": (5.0, heat_limit_max),
                "min_flow": (20.0, 30.0)},
        comfort_boost={"curve": 1.5, "room_setpoint": 25.0, "heat_limit": heat_limit_max},
        emergency_boost_levers=("curve", "room_setpoint", "heat_limit"),
        arrival_threshold_k=0.5,
    )


def _rt(ha, manifest, store, override=None, mqtt=None, clock=None, zone_prepared=True):
    """Laufzeit-Sicht von ticks._attempt mit echter Pipeline (Vaillant) und dem MQTT-Kanal als Port (A5-25).
    zone_prepared: die Zone wurde in diesem Prozess vorbereitet (Runtime.zone_prepared)."""
    if override is None:
        override = LeverPipeline(store, HaPlantBinding(ha, manifest, VAILLANT_MYPYLLANT), _safety(), clock=clock)
    return SimpleNamespace(manifest=manifest, signals=ha, mqtt_client=mqtt, store=store, override=override,
                           manual_override_seq=None, manual_override_sent=None, zone_prepared=zone_prepared)


@pytest.mark.parametrize("mqtt", [None, FakeMqttChannel(connected=False)], ids=["ohne-kanal", "getrennt"])
def test_attempt_without_mqtt_connection_reports_unsent_and_sends_nothing(make_store, clock, mqtt):
    manifest = ChannelManifest(refs={"room_target": "sensor.room_target", "shift_current": "number.shift_current"})
    rt = _rt(FakeHa(), manifest, make_store(), mqtt=mqtt, clock=clock)

    assert ticks._attempt(rt, "s1", "daily") == delivery.Published(seq="s1", unsent=True)
    assert mqtt is None or mqtt.snapshots == []


@pytest.mark.parametrize(("zone", "expected"), [(0.0, 21.0), (22.0, 22.0)], ids=["zone-ruht", "zone-aktiv"])
def test_snapshot_uses_restore_point_when_zone_is_off(make_store, clock, zone, expected):
    ha = FakeHa()
    ha.states["climate.zone::temperature"] = zone
    manifest = ChannelManifest(refs={"room_target": "sensor.room_target", "shift_current": "climate.zone::temperature"})
    store = make_store(backup={"restore_point": {"room_setpoint": 21.0}})
    mqtt = FakeMqttChannel()
    rt = _rt(ha, manifest, store, mqtt=mqtt, clock=clock)
    clock.advance(SETTLE + 1)

    assert ticks._attempt(rt, "s1", "daily") == delivery.Published(seq="s1")
    assert [s["levers"] for s in mqtt.snapshots] == [{"room_setpoint": expected}]


def test_ruhende_zone_beim_ersten_start_nimmt_das_raum_soll(make_store, clock):
    """S3-3: Zone vorbereitet, ruht (liest 0), und es gibt noch keinen Wiederherstellungspunkt (erster Start): der
    Snapshot meldet das stabile Raum-Soll statt eines Datenfehlers; der Wiederherstellungspunkt bleibt leer (ihn
    schreibt erst die Antwort des Servers)."""
    ha = FakeHa()
    ha.states["climate.zone::temperature"] = 0.0
    manifest = ChannelManifest(refs={"room_target": "sensor.room_target", "shift_current": "climate.zone::temperature"})
    store = make_store()
    store.update(stable_target=21.0)
    mqtt = FakeMqttChannel()
    rt = _rt(ha, manifest, store, mqtt=mqtt, clock=clock)
    clock.advance(SETTLE + 1)

    assert ticks._attempt(rt, "s1", "daily") == delivery.Published(seq="s1")
    assert [s["levers"] for s in mqtt.snapshots] == [{"room_setpoint": 21.0}]
    assert store.state.restore_point == {}


def test_ruhende_nie_vorbereitete_zone_ohne_wiederherstellungspunkt_bleibt_ein_datenfehler(make_store, clock):
    """S3-3, Gegenprobe (Schlussreview I-1): ist die Zone in diesem Prozess nie vorbereitet worden, gilt der
    Rueckfall auf das Raum-Soll nicht; der Snapshot bleibt zurueckgehalten. Sonst stellte die Antwort des Servers
    die Zone ausserhalb des Kontingents `zone_prepare` um."""
    ha = FakeHa()
    ha.states["climate.zone::temperature"] = 0.0
    manifest = ChannelManifest(refs={"room_target": "sensor.room_target", "shift_current": "climate.zone::temperature"})
    store = make_store()
    store.update(stable_target=21.0)
    mqtt = FakeMqttChannel()
    rt = _rt(ha, manifest, store, mqtt=mqtt, clock=clock, zone_prepared=False)
    clock.advance(SETTLE + 1)

    assert ticks._attempt(rt, "s1", "daily") == delivery.ReadInvalid(seq="s1", roles=("room_setpoint",))
    assert mqtt.snapshots == []


def test_ruhende_zone_ohne_wiederherstellungspunkt_und_ohne_raum_soll_bleibt_ein_datenfehler(make_store, clock):
    """S3-3, Gegenprobe: ohne stabiles Raum-Soll gibt es nichts zu melden, der Snapshot wird zurueckgehalten."""
    ha = FakeHa()
    ha.states["climate.zone::temperature"] = 0.0
    manifest = ChannelManifest(refs={"room_target": "sensor.room_target", "shift_current": "climate.zone::temperature"})
    mqtt = FakeMqttChannel()
    rt = _rt(ha, manifest, make_store(), mqtt=mqtt, clock=clock)
    clock.advance(SETTLE + 1)

    assert ticks._attempt(rt, "s1", "daily") == delivery.ReadInvalid(seq="s1", roles=("room_setpoint",))
    assert mqtt.snapshots == []


def test_ruhende_zone_bevorzugt_den_wiederherstellungspunkt_vor_dem_raum_soll(make_store, clock):
    """S3-3, Gegenprobe: der Rueckfall auf das Raum-Soll gilt nur ohne Wiederherstellungspunkt."""
    ha = FakeHa()
    ha.states["climate.zone::temperature"] = 0.0
    manifest = ChannelManifest(refs={"room_target": "sensor.room_target", "shift_current": "climate.zone::temperature"})
    store = make_store(backup={"restore_point": {"room_setpoint": 19.0}})
    store.update(stable_target=21.0)
    mqtt = FakeMqttChannel()
    rt = _rt(ha, manifest, store, mqtt=mqtt, clock=clock)
    clock.advance(SETTLE + 1)

    assert ticks._attempt(rt, "s1", "daily") == delivery.Published(seq="s1")
    assert [s["levers"] for s in mqtt.snapshots] == [{"room_setpoint": 19.0}]


class _StaleZoneHa:
    """mypyllant nach dem Umstellen auf Manuell: HA zeigt bis zum naechsten Poll weiter den Sollwert
    des Zeitprogramms, eigene Schreibvorgaenge erscheinen noch nicht."""

    def __init__(self):
        self.states = {"climate.zone": "auto", "climate.zone::temperature": 18.0, "number.curve": 1.0,
                       "sensor.room_target": 20.0}
        self.writes = []

    def get_state(self, entity_id):
        return self.states[entity_id]

    def get_raw_state(self, entity_id):
        return self.states[entity_id]

    def set_hvac_mode(self, entity_id, mode):
        self.writes.append(("hvac", entity_id, mode))

    def set_climate_temperature(self, entity_id, value):
        self.writes.append(("temperature", entity_id, value))


def test_first_start_snapshot_reports_the_written_start_shift_not_the_stale_zone(make_store, clock):
    # Re-Review: _prime schreibt die Startverschiebung (21.0), der erzwungene Tick geht Sekunden spaeter
    # raus. Der Live-Read zeigt noch 18.0 aus dem Zeitprogramm; der Server-Erstkontakt wuerde 18
    # uebernehmen und mit seiner Antwort die 21 ueberschreiben.
    manifest = ChannelManifest(refs={
        "room_target": "sensor.room_target", "curve_current": "number.curve",
        "shift_current": "climate.zone::temperature", "min_flow": "number.min_flow",
    })
    ha, store, mqtt = _StaleZoneHa(), make_store(), FakeMqttChannel()
    override = LeverPipeline(store, HaPlantBinding(ha, manifest, VAILLANT_MYPYLLANT), _safety(), clock=clock)
    override.prepare_start(21.0)
    assert ("temperature", "climate.zone", 21.0) in ha.writes
    rt = _rt(ha, manifest, store, override=override, mqtt=mqtt)

    ticks._attempt(rt, "s1", "first_start")
    assert mqtt.snapshots[-1]["levers"] == {"curve": 1.0, "room_setpoint": 21.0}

    # Nach der Beruhigungszeit gilt wieder der Live-Wert (z. B. ein echter Eingriff in der App).
    clock.advance(SETTLE + 1)
    ticks._attempt(rt, "s2", "daily")
    assert mqtt.snapshots[-1]["levers"] == {"curve": 1.0, "room_setpoint": 18.0}


def test_fallback_follow_up_keeps_the_retry_chain_alive():
    assert ticks._fallback_follow_up(delivery.Attempt(seq="s1", trigger="daily")) == delivery.Published(seq="s1")
    assert ticks._fallback_follow_up(delivery.EndEmergencyBoost()) is None


class _LaggingPlantHa:
    """mypyllant nach einem Cloud-Schreibvorgang: HA zeigt bis zum naechsten Poll die alten Werte."""

    def __init__(self):
        self.states = {"number.curve": 1.0, "number.shift": 17.5, "number.heat_limit": 16.0, "sensor.room_target": 20.0}
        self.writes = []

    def get_state(self, entity_id):
        return self.states[entity_id]

    def get_raw_state(self, entity_id):
        return str(self.states[entity_id])

    def set_number_value(self, entity_id, value):
        self.writes.append((entity_id, value))


def test_snapshot_reports_all_own_writes_until_settled_then_the_live_values(make_store, clock):
    # Audit 3, A3-02: der Tagestick vergleicht curve/heat_limit mit den zuletzt gesendeten Werten
    # ("Anlage folgt nicht"). Kurz nach einem eigenen Schreiben zeigt HA noch die alten Werte; ohne diese Regel
    # zaehlte eine Soll-Absenkung kurz vor dem Tagestick als Abweichung. Einschwingzeit Vaillant: 2100 s.
    manifest = ChannelManifest(refs={
        "room_target": "sensor.room_target", "curve_current": "number.curve", "shift_current": "number.shift",
        "heat_limit": "number.heat_limit",
    })
    ha, store, mqtt = _LaggingPlantHa(), make_store(), FakeMqttChannel()
    override = LeverPipeline(store, HaPlantBinding(ha, manifest, VAILLANT_MYPYLLANT), _safety(heat_limit_max=23.0),
                             clock=clock)
    override.apply_server_values({"curve": 1.2, "room_setpoint": 18.5, "heat_limit": 17.0})
    assert sorted(ha.writes) == [("number.curve", 1.2), ("number.heat_limit", 17.0), ("number.shift", 18.5)]
    rt = _rt(ha, manifest, store, override=override, mqtt=mqtt)

    ticks._attempt(rt, "s1", "daily")
    clock.advance(2100)  # genau die Einschwingzeit: noch die eigenen Schreibwerte
    ticks._attempt(rt, "s2", "daily")
    clock.advance(1)  # eingeschwungen: die Live-Werte aus HA
    ticks._attempt(rt, "s3", "daily")

    assert [(s["seq"], s["levers"]) for s in mqtt.snapshots] == [
        ("s1", {"curve": 1.2, "room_setpoint": 18.5, "heat_limit": 17.0}),
        ("s2", {"curve": 1.2, "room_setpoint": 18.5, "heat_limit": 17.0}),
        ("s3", {"curve": 1.0, "room_setpoint": 17.5, "heat_limit": 16.0}),
    ]


def test_write_fault_detail_names_the_lever():
    # Ersetzt test_write_fault_detail_keeps_the_role_names_of_the_customer_text (Controller-Ruling Task 10, Spec
    # P2-3): Datenfehler nennen Hebel; der Detailtext ist der von DeviceWriteError ("curve (number.x): Ursache").
    error = DeviceWriteError("curve", "number.curve", RuntimeError("myVAILLANT-Cloud nicht erreichbar"))
    assert str(error) == "curve (number.curve): myVAILLANT-Cloud nicht erreichbar"
    error = DeviceWriteError("room_setpoint", "climate.zone::temperature", RuntimeError("x" * 300))
    assert str(error) == "room_setpoint (climate.zone::temperature): " + "x" * 200
    error = DeviceWriteError("heat_limit", "number.hl", RuntimeError("weg"))
    assert str(error) == "heat_limit (number.hl): weg"


# --- Fremdtext aus der Serverantwort (A6-37) ---

class _FakeWorker:
    def schedule(self, delay_s, event):
        return 0


def _rt_mit_offenem_tick(make_store, clock):
    """Laufzeit-Sicht von handle_setpoints: echte Pipeline, echter Notifier mit FakeNotifySink, offener Tick s1."""
    manifest = ChannelManifest(refs={"room_target": "sensor.room_target", "shift_current": "number.shift_current"})
    store = make_store(failsafe={"failsafe_active": False, "datenfehler": None,
                                 "pending": {"seq": "s1", "trigger": "daily"}})
    sink = FakeNotifySink()
    rt = _rt(FakeHa(), manifest, store, clock=clock)
    rt.notifier, rt.texts, rt.worker = Notifier(store, sink), HA_TEXTS, _FakeWorker()
    return rt, sink


def _server_grund(rt):
    return rt.store.state.delivery.datenfehler.detail[0]


def test_riesige_ungueltige_antwort_gibt_kurzen_grund(make_store, clock):
    """A6-37: der Server liefert ungueltige `levers` von 130 KB; der Grund (Datenfehler, Push, Speicher, spaeter der
    Stapel an den Server) bleibt bei 200 Zeichen, der Anfang mit dem Schluessel vor dem Doppelpunkt bleibt."""
    rt, sink = _rt_mit_offenem_tick(make_store, clock)

    ticks.handle_setpoints(rt, {"seq": "s1", "schema": 4, "status": "ok", "levers": {"curve": "x" * 130_000}})

    grund = _server_grund(rt)
    assert len(grund) <= 200
    assert grund.startswith("ungültige Serverantwort (status='ok', levers=")
    (_, meldung) = sink.pushes[-1]
    assert len(meldung) < 400
    assert len(rt.store.state.notify_messages["datenfehler"]) < 400


def test_riesiges_unbekanntes_schema_gibt_kurzen_grund(make_store, clock):
    rt, _ = _rt_mit_offenem_tick(make_store, clock)

    ticks.handle_setpoints(rt, {"seq": "s1", "schema": "x" * 130_000})

    assert len(_server_grund(rt)) <= 200


def test_langer_ablehnungsgrund_wird_gekuerzt(make_store, clock):
    rt, sink = _rt_mit_offenem_tick(make_store, clock)
    reason = "unplausibler Wert für dat: " + "9" * 130_000

    ticks.handle_setpoints(rt, {"seq": "s1", "schema": 4, "status": "rejected", "reason": reason})

    grund = _server_grund(rt)
    assert len(grund) <= 200
    assert grund.startswith("unplausibler Wert für dat: 99")
    assert len(sink.pushes[-1][1]) < 400


def test_kurzer_ablehnungsgrund_bleibt_unveraendert(make_store, clock):
    rt, _ = _rt_mit_offenem_tick(make_store, clock)
    reason = "x" * 200

    ticks.handle_setpoints(rt, {"seq": "s1", "schema": 4, "status": "rejected", "reason": reason})

    assert _server_grund(rt) == reason


def test_gekuerzter_ablehnungsgrund_hat_genau_die_grenze_und_endet_auf_auslassung(make_store, clock):
    """Mutationsprobe TP2: die Kuerzung nimmt 199 Zeichen vom Anfang und haengt "…" an, zusammen genau 200."""
    rt, _ = _rt_mit_offenem_tick(make_store, clock)
    reason = "".join(chr(ord("a") + i % 26) for i in range(1000))

    ticks.handle_setpoints(rt, {"seq": "s1", "schema": 4, "status": "rejected", "reason": reason})

    grund = _server_grund(rt)
    assert len(grund) == ticks.FREMDTEXT_MAX_CHARS == 200
    assert grund == reason[:199] + "…"


def test_ablehnungsgrund_mit_201_zeichen_wird_gekuerzt_einer_mit_200_nicht(make_store, clock):
    rt, _ = _rt_mit_offenem_tick(make_store, clock)

    ticks.handle_setpoints(rt, {"seq": "s1", "schema": 4, "status": "rejected", "reason": "y" * 201})

    assert _server_grund(rt) == "y" * 199 + "…"
