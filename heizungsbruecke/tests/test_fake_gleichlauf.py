"""Gleichlauf der Port-Fakes an der V2-Grenze (A5-25, Controller-Ruling Task 9): die Fakes des Geraetekerns, des
Geraete-Hosts und des Links (RuntimeChannel) in den Host- und Laufzeit-Tests duerfen festhalten, was ihnen uebergeben
wird, muessen aber dieselbe Signatur haben wie die echte Klasse (Name, Art und Vorgabe jedes Parameters). Aendert sich
die echte Methode, wird dieser Test rot, statt dass die Fakes still eine alte Schnittstelle weiter bedienen. Dazu der
Fake-Trigger-Client als Fake des Grenz-Adapters zum HA-WebSocket (Controller-Ruling Task 10)."""
import inspect

import pytest
from fakes import FakeTriggerClient, trigger_klasse
from geraet_helpers import Device as HostTestDevice
from geraet_helpers import FakeDevice, FakeGeraet
from golden_scenario import RecordingTrigger
from host_konformitaet import KonformDevice
from runtime_helpers import loop_start_aufzeichnen, verbindung_aus

from heizungsbruecke.geraet_host import HaDeviceHost
from heizungsbruecke.ha_trigger_client import HaTriggerClient
from smartheat_device import prozess
from smartheat_device.device import Device
from smartheat_device.documents import DocumentStore
from smartheat_device.laufzeit import RuntimeChannel
from smartheat_runtime.ports import StatusSink


def _form(funktion) -> list[tuple[str, object, object]]:
    """(Name, Art, Vorgabe) je Parameter, ohne `self`; Typangaben zaehlen nicht (Fakes tragen keine)."""
    parameter = list(inspect.signature(funktion).parameters.values())
    if parameter and parameter[0].name == "self":
        parameter = parameter[1:]
    return [(p.name, p.kind, p.default) for p in parameter]


DEVICE_SICHT = ("wunsch", "soll_quelle_aus", "konfiguration", "bedienung", "abo_status", "meldung")
PAARE = [
    # Geraetekern, wie ihn HaDeviceHost und die Konformitaetssuite sehen
    *((f"geraet_helpers.Device.{name}", getattr(HostTestDevice, name), getattr(Device, name)) for name in DEVICE_SICHT),
    ("geraet_helpers.Device.aktuell", HostTestDevice.aktuell, DocumentStore.aktuell),  # documents = self
    *((f"KonformDevice.{name}", getattr(KonformDevice, name), getattr(Device, name)) for name in DEVICE_SICHT),
    # Geraetekern, wie ihn main (laufen, verdrahten) sieht
    *((f"FakeDevice.{name}", getattr(FakeDevice, name), getattr(Device, name))
      for name in ("konfiguration", "bedienung", "abo_status", "start", "stop", "laufzeit_kanal")),
    # Geraete-Host, wie ihn HaHost und app.start sehen
    *((f"FakeGeraet.{name}", getattr(FakeGeraet, name), getattr(HaDeviceHost, name))
      for name in ("konfiguration", "bedienung", "abo_status", "notify_sink_fuer", "wunsch", "soll_quelle_aus",
                   "ursprungswerte_merken", "raum_merken")),
    ("FakeGeraet.kanal", FakeGeraet.kanal, Device.laufzeit_kanal),  # mqtt_factory von app.start
    ("FakeGeraet.neustart", FakeGeraet.neustart, prozess.neustart_der_laufzeit),  # neustart von app.start
    ("FakeGeraet.publish", FakeGeraet.publish, StatusSink.publish),  # status_sink ohne HA
    # Link der Laufzeit (Spione in runtime_helpers.py)
    ("loop_start_aufzeichnen", loop_start_aufzeichnen([], lambda: None), RuntimeChannel.loop_start),
    ("verbindung_aus", verbindung_aus({"up": True}), RuntimeChannel.is_connected),
    # Grenz-Adapter zum HA-WebSocket (Controller-Ruling Task 10): Fake-Trigger-Client der Laufzeit-, Property- und
    # Szenario-Tests (geraet_trigger.HaTriggerClient per monkeypatch ersetzt)
    *((f"{fake.__name__}.{name}", getattr(fake, name), getattr(HaTriggerClient, name))
      for fake in (FakeTriggerClient, RecordingTrigger) for name in ("__init__", "start", "stop")),
]


@pytest.mark.parametrize(("fake", "echt"), [(fake, echt) for _, fake, echt in PAARE], ids=[name for name, _, _ in PAARE])
def test_port_fakes_haben_die_signatur_der_echten_klasse(fake, echt):
    assert _form(fake) == _form(echt)


def test_die_fake_trigger_clients_haben_connected_wie_der_echte():
    """`connected` ist am echten Client eine Eigenschaft ohne Parameter; die Fakes tragen sie als Attribut."""
    assert isinstance(HaTriggerClient.__dict__["connected"], property)
    assert trigger_klasse([])("ws://x", "t", [], print).connected is True


def test_eine_abweichende_signatur_faellt_auf():
    """Gegenprobe des Vergleichs: anderer Name, andere Art (keyword-only) und fehlende Vorgabe weichen je ab."""
    class Falsch:
        def wunsch(self, wert, herkunft, durch=None):  # herkunft nicht keyword-only
            pass

        def soll_quelle_aus(self, aus, wirksam):  # Vorgabe fehlt
            pass

        def laufzeit_kanal(self, w):  # anderer Name
            pass

    assert _form(Falsch.wunsch) != _form(Device.wunsch)
    assert _form(Falsch.soll_quelle_aus) != _form(Device.soll_quelle_aus)
    assert _form(Falsch.laufzeit_kanal) != _form(Device.laufzeit_kanal)
