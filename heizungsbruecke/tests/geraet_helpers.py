"""Geraete-Pfad fuer die Add-on-Tests (Spec 5c): alte Test-Optionen als Konfigurationsdokument, Attrappe des
Geraete-Hosts und Start der Laufzeit ueber den Link (RuntimeChannel). So laufen die bestehenden Laufzeit-Tests mit
ihren Optionen weiter, nur der Weg der Einstellungen ist neu."""
import json

from heizungsbruecke.ha_sinks import HaNotifySink
from heizungsbruecke.host import HaHost
from smartheat_device.laufzeit import RuntimeChannel
from smartheat_runtime import app, entitlement
from smartheat_runtime.roles import LEVER_ROLES

_HEBEL = {f"entity_{role}": lever for lever, role in LEVER_ROLES.items()}


def konfiguration_aus_optionen(options: dict) -> dict:
    """Umkehrung von dokument.optionen fuer die alten Test-Optionen; entity_room_target wird rollen.room_target."""
    rollen = {"room_target": options["entity_room_target"]}
    for key, value in options.items():
        if key.startswith("entity_") and key != "entity_room_target" and value:
            rollen[_HEBEL.get(key, key.removeprefix("entity_"))] = value
    bindung = {"kreis": {"plattform": "mypyllant", "config_entry": "e1", "anlage": "S", "nummer": "0"},
               "rollen": rollen, "batterien": list(options.get("battery_entities", [])),
               "benachrichtigung": list(options.get("notify_services", [])),
               "hinweise_aus": list(options.get("notify_hints_off", []))}
    if options.get("poll_interval_seconds") is not None:
        bindung["abfrage_s"] = options["poll_interval_seconds"]
    konfiguration = {
        "anlage": options["tenant_id"], "profil": "vaillant_gastherme_heizkoerper",
        "hebelsatz": options.get("lever_set") or "vaillant_vrc720", "verteilsystem": options.get("verteilsystem"),
        "bindung": bindung, "raumfuehler": list(options.get("room_sensors", [])),
        "tagestick": options.get("daily_trigger_time"), "abo": "aktiv", "mindest_software": "1.0.0", "software": None,
    }
    intervalle = {k: options[o] for k, o in (("pruefung_s", "local_check_interval_seconds"),
                                              ("telemetrie_s", "telemetry_interval_seconds")) if o in options}
    if intervalle:
        konfiguration["intervalle"] = intervalle
    return konfiguration


class Sent:
    def __init__(self):
        self.messages, self.connected = [], True

    def publish(self, name, payload):
        self.messages.append((name, json.loads(json.dumps(payload))))
        return len(self.messages)

    def of(self, name):
        return [payload for topic, payload in self.messages if topic == name]


class _NullSink:
    def push(self, key, message):
        pass

    def show(self, key, message):
        pass

    def withdraw(self, key):
        pass


class FakeGeraet:
    """Attrappe des Geraete-Hosts (Task 6): haelt Konfiguration, Bedienung, Abo, gesammelte Wuensche und Status."""

    def __init__(self, konfiguration, *, bedienung=None, dirty=False, abo=entitlement.ACTIVE, ha=None):
        self._konfiguration, self._bedienung, self._dirty, self._abo = konfiguration, bedienung, dirty, abo
        self.wuensche, self.aus, self.status = [], [], []
        self.raum = None
        self.sent = Sent()
        self.channel = None
        self.status_sink = self
        self._ha = ha

    # Geraete-Host-Sicht fuer HaHost
    def konfiguration(self):
        return self._konfiguration

    def bedienung(self):
        return self._bedienung, self._dirty

    def abo_status(self):
        return self._abo

    def notify_sink_fuer(self, services):
        return HaNotifySink(self._ha, services) if self._ha is not None else _NullSink()

    def wunsch(self, wert, herkunft=None, durch=None):
        self.wuensche.append((wert, herkunft, durch))
        return min(max(float(wert), 15.0), 25.0)

    def soll_quelle_aus(self, aus, wirksam):
        self.aus.append((aus, wirksam))

    def raum_merken(self, raum):
        self.raum = raum

    def publish(self, event):
        self.status.append(event)

    def kanal(self, worker):
        self.channel = RuntimeChannel(self.sent.publish, lambda: self.sent.connected, lambda: 0)
        return self.channel.binden(worker)


def start_bridge(env, *, abo=entitlement.ACTIVE, abgemeldet=False, bedienung=None, **option_overrides):
    """Startet die Laufzeit auf dem Dokument aus den Test-Optionen (plus Overrides). Liefert (bridge, geraet); die
    gesendeten Nachrichten stehen in geraet.sent, der Kanal in geraet.channel."""
    from test_runtime import OPTIONS  # Kreisimport vermeiden: erst zur Laufzeit

    from heizungsbruecke import config
    options = {**OPTIONS, **option_overrides}
    konfiguration = konfiguration_aus_optionen(options)
    if abgemeldet:
        config.ABGEMELDET_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.ABGEMELDET_PATH.write_text(json.dumps({"anlage": konfiguration["anlage"]}))
    geraet = FakeGeraet(konfiguration, abo=abo, ha=env.ha,
                        bedienung=bedienung or {"raum_soll": env.ha.states.get(options["entity_room_target"], 21.0)})
    bridge = app.start(HaHost(geraet, env.ha, clock=env.clock), env.clock, mqtt_factory=geraet.kanal)
    return bridge, geraet
