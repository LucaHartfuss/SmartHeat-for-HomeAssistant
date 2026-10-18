"""Spec 5c 1.3 / 5b 3.2-3.3: Soll-Eingang hostneutral (Gateway und HA-Add-on)."""
from pathlib import Path

from conftest import FakeClock

from smartheat_runtime.runtime import EV_LOCAL_CHECK
from smartheat_runtime.soll_eingang import EV_SOLL_MODUS, EV_TARGET_INPUT, SollEingang
from smartheat_runtime.soll_speicher import SOURCE_LOKAL, SOURCE_PORTAL, SOURCE_THERMOSTAT, TargetStore
from smartheat_runtime.worker import Event, RegulationWorker


class Quelle:
    def __init__(self, schreibbar=True, wert=21.0):
        self.schreibbar, self.wert, self.geschrieben = schreibbar, wert, []

    def schreiben(self, value):
        if not self.schreibbar:
            return False
        self.geschrieben.append(value)
        self.wert = value
        return True

    def angezeigt(self):
        return self.wert


class Aufbau:
    def __init__(self, tmp_path: Path, *, schreibbar=True, entprellen_s=0.0):
        self.clock = FakeClock()
        self.worker = RegulationWorker(self.clock)
        self.store = TargetStore(tmp_path / "soll.json", 21.0, self.clock, lambda: "2026-10-11T10:00:00+00:00")
        self.quelle = Quelle(schreibbar)
        self.wuensche, self.aus, self.checks = [], [], []
        self.worker.register(EV_LOCAL_CHECK, lambda event: self.checks.append(event.data))
        self.eingang = SollEingang(self.worker, self.store, self.quelle, wunsch=self._wunsch,
                                   soll_quelle_aus=lambda aus, wirksam: self.aus.append((aus, wirksam)),
                                   entprellen_s=entprellen_s)

    def _wunsch(self, value, herkunft, durch):
        self.wuensche.append((value, herkunft, durch))
        return min(max(value, 15.0), 25.0)

    def post(self, value, source, **extra):
        self.worker.post(Event(EV_TARGET_INPUT, {"value": value, "source": source, **extra}))
        self.worker.run_pending()


def test_ha_entity_wish_is_one_wish_with_origin(tmp_path):
    a = Aufbau(tmp_path)
    a.post(22.5, SOURCE_THERMOSTAT, herkunft="soll_entity", durch="nutzer")
    assert a.store.value == 22.5 and a.wuensche == [(22.5, "soll_entity", "nutzer")]
    assert a.checks and a.checks[-1]["room_target_fired"] is True


def test_limited_wish_is_written_back(tmp_path):
    a = Aufbau(tmp_path)
    a.post(14.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.aus == [] and a.store.value == 15.0 and a.quelle.geschrieben == [15.0]


def test_frost_position_is_not_a_wish(tmp_path):
    a = Aufbau(tmp_path)
    a.post(7.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.wuensche == [] and a.aus == [(True, 21.0)] and a.store.value == 21.0


def test_hvac_off_then_on_restores_the_display(tmp_path):
    a = Aufbau(tmp_path)
    a.worker.post(Event(EV_SOLL_MODUS, {"mode": "off"}))
    a.worker.run_pending()
    a.post(23.0, SOURCE_PORTAL)
    assert a.store.value == 23.0 and a.quelle.geschrieben == []  # aus: nicht schreiben
    a.quelle.wert = 21.0
    a.worker.post(Event(EV_SOLL_MODUS, {"mode": "heat"}))
    a.worker.run_pending()
    assert a.quelle.geschrieben == [23.0] and a.aus[-1] == (False, None)


def test_ha_source_echo_is_not_a_wish(tmp_path):
    a = Aufbau(tmp_path)
    a.post(22.0, SOURCE_PORTAL)
    assert a.quelle.geschrieben == [22.0]
    a.post(22.0, SOURCE_THERMOSTAT, herkunft="soll_entity")  # HA meldet den eigenen Schreibwert
    assert a.wuensche == []


def test_local_event_wish_aligns_the_entity(tmp_path):
    a = Aufbau(tmp_path)
    a.post(22.0, SOURCE_LOKAL, herkunft="smartheat_entity", durch="automation")
    assert a.wuensche == [(22.0, "smartheat_entity", "automation")]
    assert a.store.value == 22.0 and a.store.source == SOURCE_LOKAL and a.quelle.geschrieben == [22.0]


def test_not_writable_source_keeps_the_value_internally(tmp_path):
    a = Aufbau(tmp_path, schreibbar=False)
    a.post(23.0, SOURCE_PORTAL)
    assert a.store.value == 23.0 and a.quelle.geschrieben == []


def test_gateway_debounce_still_waits(tmp_path):
    a = Aufbau(tmp_path, entprellen_s=10.0)
    a.worker.post(Event(EV_TARGET_INPUT, {"value": 22.0, "source": SOURCE_THERMOSTAT}))
    a.worker.run_pending()
    assert a.wuensche == []
    a.clock.advance(10)
    a.worker.run_pending()
    assert a.wuensche == [(22.0, None, None)]


def test_clamped_local_wish_keeps_source_lokal_and_writes_back(tmp_path):
    a = Aufbau(tmp_path)
    a.post(30.0, SOURCE_LOKAL, herkunft="smartheat_entity")
    assert a.wuensche == [(30.0, "smartheat_entity", None)]
    assert a.store.value == 25.0 and a.store.source == SOURCE_LOKAL and a.quelle.geschrieben == [25.0]


def test_stale_source_report_after_portal_operation_is_no_wish(tmp_path):
    a = Aufbau(tmp_path)
    a.post(22.0, SOURCE_PORTAL)  # Store hielt 21, die Quelle zeigt noch 21
    assert a.quelle.geschrieben == [22.0]
    a.post(21.0, SOURCE_THERMOSTAT, herkunft="soll_entity")  # verspaetete Meldung des alten Werts (Echo-Fenster)
    assert a.wuensche == [] and a.store.value == 22.0
    a.clock.advance(120)  # Echo-Fenster vorbei, der Schreibbefehl ist noch offen
    a.post(21.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert a.wuensche == [] and a.store.value == 22.0


def test_stale_source_report_after_clamped_write_back_is_no_wish(tmp_path):
    a = Aufbau(tmp_path)
    a.post(14.0, SOURCE_THERMOSTAT, herkunft="soll_entity")  # begrenzt auf 15 und zurueckgeschrieben
    assert a.quelle.geschrieben == [15.0] and len(a.wuensche) == 1
    a.post(14.0, SOURCE_THERMOSTAT, herkunft="soll_entity")  # die Quelle meldet noch den eigenen Wert 14
    assert len(a.wuensche) == 1 and a.store.value == 15.0
    a.clock.advance(120)
    a.post(14.0, SOURCE_THERMOSTAT, herkunft="soll_entity")
    assert len(a.wuensche) == 1 and a.store.value == 15.0


def test_local_wish_while_source_is_off_is_discarded(tmp_path):
    """5c F/Q3 (2026-10-12): wie der Drehwert am ausgeschalteten Zigbee-Thermostat des Gateways: kein Wunsch, kein
    Schreiben, kein Drift zwischen Soll-Speicher und Thermostat; der Hinweis soll_quelle_aus bleibt."""
    a = Aufbau(tmp_path)
    a.worker.post(Event(EV_SOLL_MODUS, {"mode": "off"}))
    a.worker.run_pending()
    a.post(22.0, SOURCE_LOKAL, herkunft="smartheat_entity", durch="automation")
    assert a.wuensche == [] and a.store.value == 21.0 and a.quelle.geschrieben == []
    assert a.aus == [(True, 21.0)] and a.checks == []
    a.quelle.wert = 21.0
    a.worker.post(Event(EV_SOLL_MODUS, {"mode": "heat"}))
    a.worker.run_pending()
    assert a.wuensche == [] and a.store.value == 21.0  # nach dem Einschalten kommt nichts nach


def test_repeated_identical_local_wish_is_not_repeated(tmp_path):
    a = Aufbau(tmp_path)
    a.post(22.0, SOURCE_LOKAL, herkunft="smartheat_entity")
    assert len(a.wuensche) == 1 and len(a.checks) == 1
    a.post(22.0, SOURCE_LOKAL, herkunft="smartheat_entity")
    assert len(a.wuensche) == 1 and len(a.checks) == 1 and a.quelle.geschrieben == [22.0]


class AblehnenderAufbau(Aufbau):
    def _wunsch(self, value, herkunft, durch):
        """Wie Device.wunsch bei einer Ablehnung (Fehlerinjektion an der Grenze zum Geraetekern)."""
        self.wuensche.append((value, herkunft, durch))
        raise ValueError("herkunft unbekannt")


def test_a_wish_the_device_refuses_keeps_the_entered_value_without_write_back(tmp_path):
    """Device.wunsch lehnt mit ValueError ab (keine endliche Zahl, herkunft oder durch kein Vertragswert, 5b-D1-4):
    der Soll-Eingang faengt das, der eingegebene Wert gilt lokal, nichts wird an die Quelle zurueckgeschrieben, und
    die lokale Pruefung laeuft."""
    a = AblehnenderAufbau(tmp_path)
    a.post(22.5, SOURCE_THERMOSTAT, herkunft="app", durch="nutzer")
    assert a.wuensche == [(22.5, "app", "nutzer")]
    assert a.store.value == 22.5 and a.quelle.geschrieben == []
    assert a.checks and a.checks[-1]["room_target_fired"] is True
