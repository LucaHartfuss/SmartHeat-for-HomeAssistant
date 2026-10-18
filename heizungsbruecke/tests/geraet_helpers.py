"""Geraete-Pfad fuer die Add-on-Tests (Spec 5c): alte Test-Optionen als Konfigurationsdokument, Attrappe des
Geraete-Hosts und Start der Laufzeit ueber den Link (RuntimeChannel). So laufen die bestehenden Laufzeit-Tests mit
ihren Optionen weiter, nur der Weg der Einstellungen ist neu."""
import json

import bindung_beispiele

from heizungsbruecke.geraet_status import GeraetStatus
from heizungsbruecke.ha_sinks import HaNotifySink
from heizungsbruecke.host import HaHost
from smartheat_device.laufzeit import RuntimeChannel
from smartheat_runtime import app, aufraeumen, dokument_config, entitlement
from smartheat_runtime.roles import LEVER_ROLES

_HEBEL = {f"entity_{role}": lever for lever, role in LEVER_ROLES.items()}
GERAET_ID = "shg-0000000000000001"


def konfiguration_aus_optionen(options: dict) -> dict:
    """Alte Test-Optionen als Konfigurationsdokument (Bindung wie dokument.bindung sie liest); entity_room_target wird
    rollen.room_target."""
    rollen = {"room_target": options["entity_room_target"]}
    for key, value in options.items():
        if key.startswith("entity_") and key != "entity_room_target" and value:
            rollen[_HEBEL.get(key, key.removeprefix("entity_"))] = value
    bindung = {"kreis": {"plattform": "mypyllant", "config_entry": "e1", "anlage": "S", "nummer": "0"},
               "rollen": rollen, "batterien": list(options.get("battery_entities", [])),
               "benachrichtigung": list(options.get("notify_services", [])),
               "hinweise_aus": list(options.get("notify_hints_off", []))}
    hebelsatz = options.get("lever_set") or "vaillant_vrc720"  # ein unbekannter bleibt im Dokument, Parameter: Vaillant
    konfiguration = {
        "anlage": options["tenant_id"], "profil": "vaillant_gastherme_heizkoerper",
        "hebelsatz": hebelsatz, "verteilsystem": options.get("verteilsystem"),
        "bindung": bindung, "raumfuehler": list(options.get("room_sensors", [])),
        "tagestick": options.get("daily_trigger_time"), "abo": "aktiv", "mindest_software": "1.0.0", "software": None,
        "bindungsparameter": options["bindungsparameter"] if "bindungsparameter" in options
        else bindung_beispiele.parameter(hebelsatz if hebelsatz in bindung_beispiele.PARAMETER else "vaillant_vrc720"),
    }
    return konfiguration


class Sent:
    """Was die Laufzeit ueber den Link des Geraets sendet. publish_error: der naechste Snapshot scheitert (wie frueher
    der paho-Client der Tests); connected: Verbindung des Links."""

    def __init__(self, log=None):
        self.messages, self.connected = [], True
        self.publish_error = None
        self._log = log

    def publish(self, name, payload):
        if self.publish_error is not None and name == "up/snapshot":
            raise self.publish_error
        payload = json.loads(json.dumps(payload, allow_nan=False))  # wie der echte Link (Testoffensive T2, Nr. 17)
        self.messages.append((name, payload))
        if self._log is not None:
            self._log.append(["link", name, json.loads(json.dumps(payload))])
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
    """Attrappe des Geraete-Hosts (Task 6): haelt Konfiguration, Bedienung, Abo, gesammelte Wuensche und Status.

    abo: Status oder eine Funktion, die ihn liefert (die Laufzeit liest ihn bei jeder Abo-Pruefung frisch; so kann ein
    Test den Status mitten im Lauf wechseln). Mit ha laufen die Status-Ereignisse wie produktiv ueber GeraetStatus
    (Schema 3, smartheat_status an ha.fire_event; ohne Anzeige des Geraete-Threads); das unveraenderte
    Laufzeit-Ereignis steht in status. Jeder Link der Laufzeit steht in channels, der letzte in channel; log zeichnet
    die Nachrichten des Links als ["link", <topic>, payload] auf (Golden Master). neustart ist der Rueckruf des Hosts
    fuer app.start (Spec Stufe 3 S6 4.2) und zaehlt in neustarts."""

    def __init__(self, konfiguration, *, bedienung=None, dirty=False, abo=entitlement.ACTIVE, ha=None, log=None):
        self._konfiguration, self._bedienung, self._dirty, self._abo = konfiguration, bedienung, dirty, abo
        self.wuensche, self.aus, self.status = [], [], []
        self.gemeldet: list[dict] = []  # Ursprungswerte, die die Laufzeit dem Host gab (Spec Stufe 3 S3 5.1)
        self.raum = None
        self.sent = Sent(log)
        self.channel = None
        self.channels = []
        self.neustarts: list[int] = []
        self.host = None
        self._ha = ha
        self.status_sink = self if ha is None else GeraetStatus(ha, GERAET_ID, self._raum_soll,
                                                                laufzeit_merken=self.status.append)

    def neustart(self):
        self.neustarts.append(1)

    def _raum_soll(self):
        store = self.host.store if self.host is not None else None
        return store.value if store is not None else None

    # Geraete-Host-Sicht fuer HaHost
    def konfiguration(self):
        return self._konfiguration

    def bedienung(self):
        return self._bedienung, self._dirty

    def abo_status(self):
        return self._abo() if callable(self._abo) else self._abo

    def notify_sink_fuer(self, services):
        return HaNotifySink(self._ha, services) if self._ha is not None else _NullSink()

    def wunsch(self, wert, herkunft=None, durch=None):
        self.wuensche.append((wert, herkunft, durch))
        return min(max(float(wert), 15.0), 25.0)

    def soll_quelle_aus(self, aus, wirksam):
        self.aus.append((aus, wirksam))

    def ursprungswerte_merken(self, werte):
        self.gemeldet.append(werte)

    def raum_merken(self, raum):
        self.raum = raum

    def publish(self, event):
        self.status.append(event)

    def kanal(self, worker):
        self.channel = RuntimeChannel(self.sent.publish, lambda: self.sent.connected, lambda: 0)
        self.channels.append(self.channel)
        return self.channel.binden(worker)


def start_bridge(env, *, abo=entitlement.ACTIVE, abgemeldet=False, bedienung=None, options=None, log=None,
                 verbunden=True, leer_mit_markierung=False, ursprungswerte=None, neustart=None, **option_overrides):
    """Startet die Laufzeit auf dem Dokument aus den Test-Optionen (options, sonst test_runtime.OPTIONS; plus
    Overrides). Liefert (bridge, geraet); die gesendeten Nachrichten stehen in geraet.sent, der Kanal in geraet.channel.
    Ohne bedienung ist die Bedienung des Servers der Wert, den die Soll-Entity gerade zeigt (wie nach einem Wunsch,
    den der Server als Bedienung zurueckgibt). abgemeldet setzt die Abmelde-Markierung (Befehl sign_off).
    leer_mit_markierung: die gespeicherte Konfiguration ist leer, die Aufraeum-Markierung haelt die Konfiguration aus den
    Optionen (Spec Stufe 3 S3 Abschnitt 4; wie nach HaDeviceHost.konfiguration_uebernommen). Eine schon vorhandene
    Markierung bleibt unveraendert (Neustart mit demselben Stand).
    verbunden=False: der Link ist beim Start der Laufzeit noch nicht verbunden (der Test meldet die Verbindung spaeter,
    channel.verbunden).
    ursprungswerte: Feld `ursprungswerte` der Konfiguration (Spec Stufe 3 S3 5.2).
    neustart: Rueckruf des Hosts fuer app.start; ohne ihn zaehlt geraet.neustarts die Neustarts."""
    if options is None:
        from test_runtime import OPTIONS as options  # Kreisimport vermeiden: erst zur Laufzeit

    from heizungsbruecke import config
    options = {**options, **option_overrides}
    konfiguration = konfiguration_aus_optionen(options)
    if ursprungswerte is not None:
        konfiguration = {**konfiguration, "ursprungswerte": ursprungswerte}
    if abgemeldet:
        dokument_config.abmelden(config.ABGEMELDET_PATH, konfiguration["anlage"])
    if leer_mit_markierung:
        if not config.AUFRAEUMEN_PATH.exists():
            aufraeumen.markieren(config.AUFRAEUMEN_PATH, konfiguration)
        konfiguration = {**konfiguration, "anlage": None}
    if bedienung is None:
        angezeigt = env.ha.states.get(options["entity_room_target"], 21.0)
        bedienung = {"raum_soll": angezeigt if isinstance(angezeigt, int | float) else 21.0}
    geraet = FakeGeraet(konfiguration, abo=abo, ha=env.ha, bedienung=bedienung, log=log)
    geraet.sent.connected = verbunden
    geraet.host = HaHost(geraet, env.ha, clock=env.clock)
    bridge = app.start(geraet.host, env.clock, mqtt_factory=geraet.kanal,
                       neustart=geraet.neustart if neustart is None else neustart)
    return bridge, geraet


def ankunftsschwelle_der_laufzeit(verzeichnis) -> float:
    """Ankunftsschwelle des Comfort-Boosts (zugleich Austrittsschwelle des Notfall-Boosts, regulation.run_local_check)
    aus der echten RuntimeConfig (dokument_config.runtime_config) des Dokuments aus test_runtime.OPTIONS (Vaillant x
    Heizkoerper wie client1); verzeichnis ohne Rastung."""
    from test_runtime import OPTIONS  # Kreisimport vermeiden: erst zur Laufzeit

    konfiguration = konfiguration_aus_optionen(OPTIONS)
    runtime = dokument_config.runtime_config(konfiguration, rastung_pfad=verzeichnis / "rastung.json",
                                             entitlement_path=verzeichnis / "entitlement.json",
                                             abo_source=lambda: entitlement.ACTIVE)
    return runtime.local_safety.arrival_threshold_k
