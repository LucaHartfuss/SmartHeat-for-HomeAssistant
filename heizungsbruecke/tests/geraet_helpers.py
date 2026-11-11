"""Geraete-Pfad fuer die Add-on-Tests: alte Test-Optionen als Konfigurationsdokument, Attrappe des
Geraete-Hosts und Start der Laufzeit ueber den Link (RuntimeChannel). So laufen die bestehenden Laufzeit-Tests mit
ihren Optionen weiter, nur der Weg der Einstellungen ist neu. Dazu (gemeinsam fuer
test_main_geraet.py, test_geraet_host.py und test_core_config_check*.py): die Attrappen des Geraete-Hosts im
Add-on-Prozess (FakeDevice, FakeSupervisor, Device, _host) und Beispieldokumente der Konfigurationspruefung
(gateway_konfiguration, ha_konfiguration)."""
import copy
import json
from types import SimpleNamespace

import bindung_beispiele
import konfig_form
from bindung_beispiele import parameter
from device_helpers import SENSOR
from fakes import KONFIG_EINTRAG, FakeClock, FakeHa

from heizungsbruecke import config, geraet_host
from heizungsbruecke.geraet_status import GeraetStatus, lokale_sicht
from heizungsbruecke.ha_sinks import HaNotifySink
from heizungsbruecke.host import HaHost
from smartheat_device.commands import CommandRegister
from smartheat_device.geraet_host_basis import GeraetHostBasis
from smartheat_device.laufzeit import RuntimeChannel
from smartheat_runtime import app, aufraeumen, dokument_config, entitlement
from smartheat_runtime.roles import LEVER_ROLES
from smartheat_runtime.status import Statussammler

_HEBEL = {f"entity_{role}": lever for lever, role in LEVER_ROLES.items()}
#: Plattform des Kreises je Hebelsatz in den Test-Dokumenten (als Literal, test_dokument haelt
#: dokument.SCHREIB_PLATTFORM fest); fakes.FakeHa traegt die Schreibziele mit ihr und fakes.KONFIG_EINTRAG ein.
KREIS_PLATTFORM = {"vaillant_vrc720": "mypyllant", "weishaupt_wwp": "weishaupt_modbus",
                   "weishaupt_wwp_basis": "weishaupt_modbus", "viessmann_vicare": "vicare"}
GERAET_ID = "shg-0000000000000001"


def bedienung_inhalt(raum_soll, *, kreis=1) -> dict:
    """Inhalt des Dokuments Bedienung mit einem Kreis (so uebergibt ihn das Geraet dem Host)."""
    return {"kreise": [{"kreis": kreis, "modus": "manuell", "raum_soll": raum_soll}]}


def konfiguration_aus_optionen(options: dict) -> dict:
    """Alte Test-Optionen als Konfigurationsdokument (Bindung wie dokument.bindung sie liest); entity_room_target wird
    rollen.room_target."""
    rollen = {"room_target": options["entity_room_target"]}
    for key, value in options.items():
        if key.startswith("entity_") and key != "entity_room_target" and value:
            rollen[_HEBEL.get(key, key.removeprefix("entity_"))] = value
    hebelsatz = options.get("lever_set") or "vaillant_vrc720"  # ein unbekannter bleibt im Dokument, Parameter: Vaillant
    plattform = KREIS_PLATTFORM.get(hebelsatz, "mypyllant")  # ein unbekannter Hebelsatz scheitert schon an bindung
    kreis = {"plattform": plattform, "config_entry": KONFIG_EINTRAG[plattform], "anlage": "S", "nummer": "0"}
    bindung = {"kreis": kreis,
               "rollen": rollen, "batterien": list(options.get("battery_entities", [])),
               "benachrichtigung": list(options.get("notify_services", [])),
               "hinweise_aus": list(options.get("notify_hints_off", []))}
    konfiguration = {
        "anlage": options["tenant_id"],
        "hebelsatz": hebelsatz, "verteilsystem": options.get("verteilsystem"),
        "bindung": bindung, "raumfuehler": list(options.get("room_sensors", [])),
        "tagestick": options.get("daily_trigger_time"), "abo": options.get("abo", "aktiv"), "mindest_software": "1.0.0", "software": None,
        "bindungsparameter": options["bindungsparameter"] if "bindungsparameter" in options
        else bindung_beispiele.parameter(hebelsatz if hebelsatz in bindung_beispiele.PARAMETER else "vaillant_vrc720"),
    }
    dokument = konfig_form.nach_form(konfiguration)
    if "zustand" in options:  # Zustand des (ersten) Kreises: in_betrieb, ausser_betrieb, testphase_ohne
        dokument["kreise"][0]["zustand"] = options["zustand"]
    return dokument


class Sent:
    """Was die Laufzeit ueber den Link des Geraets sendet. publish_error: der naechste Snapshot scheitert (wie frueher
    der paho-Client der Tests); connected: Verbindung des Links; failures: gescheiterte Verbindungsversuche seit der
    letzten Verbindung (MqttChannel.connect_failures)."""

    def __init__(self, log=None):
        self.messages, self.connected, self.failures = [], True, 0
        self.publish_error = None
        self._log = log

    def publish(self, name, payload):
        if self.publish_error is not None and name == "up/snapshot":
            raise self.publish_error
        payload = json.loads(json.dumps(payload, allow_nan=False))  # wie der echte Link
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
    """Attrappe des Geraete-Hosts: haelt Konfiguration, Bedienung, Abo, gesammelte Wuensche und Status.

    abo: Status oder eine Funktion, die ihn liefert (die Laufzeit liest ihn bei jeder Abo-Pruefung frisch; so kann ein
    Test den Status mitten im Lauf wechseln). Mit ha laufen die Status-Ereignisse wie produktiv ueber GeraetStatus
    (Schema 3, smartheat_status an ha.fire_event; ohne Anzeige des Geraete-Threads); das unveraenderte
    Laufzeit-Ereignis steht in status. Jeder Link der Laufzeit steht in channels, der letzte in channel; log zeichnet
    die Nachrichten des Links als ["link", <topic>, payload] auf (Golden Master). neustart ist der Rueckruf des Hosts
    fuer app.start und zaehlt in neustarts."""

    #: Sicht der Laufzeit auf den Start (laufzeit_konfiguration, ursprungswerte_server) wie im echten Geraete-Host
    laufzeit_konfiguration = GeraetHostBasis.laufzeit_konfiguration
    ursprungswerte_server = GeraetHostBasis.ursprungswerte_server

    def __init__(self, konfiguration, *, bedienung=None, dirty=False, abo=entitlement.ACTIVE, ha=None, log=None,
                 start_markierung=None):
        self._konfiguration, self._bedienung, self._dirty, self._abo = konfiguration, bedienung, dirty, abo
        self.start_markierung = start_markierung  # die beim Start gelesene Aufraeum-Markierung (aufraeumen.entscheiden)
        self.uhr = True  # was der Zeitdienst des Hosts meldet (uhr_synchron)
        self.wuensche, self.aus, self.status = [], [], []
        self.wunsch_kreise: list[int] = []  # der Kreis jedes Wunschs, parallel zu wuensche
        self.gemeldet: list[dict] = []  # Ursprungswerte, die die Laufzeit dem Host gab
        self.gemeldet_kreise: list[int] = []
        self.raum = None  # der Raum des Kreises 1
        self.raeume: dict[int, dict | None] = {}  # je Kreis der zuletzt gemeldete Raum
        self.sent = Sent(log)
        self.channel = None
        self.channels = []
        self.neustarts: list[int] = []
        self.host = None
        self._ha = ha
        self.status_sink = self if ha is None else GeraetStatus(
            ha, GERAET_ID, lokale_sicht(self.konfiguration, self._raum_soll, self._raum_ist, lambda: None),
            laufzeit_merken=self.status.append)

    def uhr_synchron(self):
        return self.uhr

    def neustart(self):
        self.neustarts.append(1)

    def _raum_soll(self, kreis):
        kreis_host = self.host.hosts.get(kreis) if hasattr(self.host, "hosts") else self.host if kreis == 1 else None
        store = kreis_host.store if kreis_host is not None else None
        return store.value if store is not None else None

    def _raum_ist(self, kreis):
        return (self.raeume.get(kreis) or {}).get("ist")

    # Geraete-Host-Sicht fuer HaHost
    def konfiguration(self):
        return self._konfiguration

    def bedienung(self):
        return self._bedienung, self._dirty

    def abo_status(self, kreis=None):
        return self._abo() if callable(self._abo) else self._abo

    def notify_sink_fuer(self, services):
        return HaNotifySink(self._ha, services) if self._ha is not None else _NullSink()

    def wunsch(self, wert, herkunft=None, durch=None, kreis=1):
        self.wuensche.append((wert, herkunft, durch))
        self.wunsch_kreise.append(kreis)
        return min(max(float(wert), 15.0), 25.0)

    def soll_quelle_aus(self, aus, wirksam):
        self.aus.append((aus, wirksam))

    def ursprungswerte_merken(self, werte, kreis=1):
        self.gemeldet.append(werte)
        self.gemeldet_kreise.append(kreis)

    def raum_merken(self, kreis, raum):
        self.raeume[kreis] = raum
        if kreis == 1:
            self.raum = raum

    def publish(self, event):
        self.status.append(event)

    def kanal(self, worker, kreis):
        self.channel = RuntimeChannel(self.sent.publish, lambda: self.sent.connected, lambda: self.sent.failures)
        self.channels.append(self.channel)
        return self.channel.binden(worker)


def start_bridge(env, *, options, abo=entitlement.ACTIVE, abgemeldet=False, bedienung=None, log=None,
                 verbunden=True, leer_mit_markierung=False, ursprungswerte=None, neustart=None, **option_overrides):
    """Startet die Laufzeit auf dem Dokument aus den Test-Optionen (options, z. B. runtime_helpers.OPTIONS; plus
    Overrides). Liefert (bridge, geraet); die gesendeten Nachrichten stehen in geraet.sent, der Kanal in geraet.channel.
    Ohne bedienung ist die Bedienung des Servers der Wert, den die Soll-Entity gerade zeigt (wie nach einem Wunsch,
    den der Server als Bedienung zurueckgibt). abgemeldet setzt die Abmelde-Markierung (Befehl sign_off).
    leer_mit_markierung: die gespeicherte Konfiguration ist leer, die Aufraeum-Markierung haelt die Konfiguration aus den
    Optionen (wie nach HaDeviceHost.konfiguration_uebernommen). Eine schon vorhandene
    Markierung bleibt unveraendert (Neustart mit demselben Stand).
    verbunden=False: der Link ist beim Start der Laufzeit noch nicht verbunden (der Test meldet die Verbindung spaeter,
    channel.verbunden).
    ursprungswerte: Feld `ursprungswerte` der Konfiguration.
    neustart: Rueckruf des Hosts fuer app.start; ohne ihn zaehlt geraet.neustarts die Neustarts."""
    options = {**options, **option_overrides}
    konfiguration = konfiguration_aus_optionen(options)
    if ursprungswerte is not None:
        konfiguration["kreise"][0]["ursprungswerte"] = ursprungswerte
    if abgemeldet:
        dokument_config.abmelden(config.ABGEMELDET_PATH, konfiguration["anlage"])
    start_markierung = None
    if leer_mit_markierung:
        if not config.AUFRAEUMEN_PATH.exists():
            aufraeumen.markieren(config.AUFRAEUMEN_PATH, konfiguration)
        konfiguration = {**konfiguration, "anlage": None}
        # wie prozess.aufraeumen_beim_start beim Start des Prozesses: die Markierung einmal lesen und weiterreichen
        start_markierung = aufraeumen.entscheiden(config.AUFRAEUMEN_PATH, konfiguration)[1]
    if bedienung is None:
        angezeigt = env.ha.states.get(options["entity_room_target"], 21.0)
        bedienung = {"raum_soll": angezeigt if isinstance(angezeigt, int | float) else 21.0}
    geraet = FakeGeraet(konfiguration, abo=abo, ha=env.ha, bedienung=bedienung, log=log,
                        start_markierung=start_markierung)
    geraet.sent.connected = verbunden
    geraet.host = HaHost(geraet, env.ha, clock=env.clock)
    # wie die Laufzeit der Kreise: ein Statussammler fasst das Ereignis des einen Kreises fuer die Statussenke zusammen
    sammler = Statussammler(geraet.host.status_sink)
    bridge = app.start(geraet.host, env.clock, mqtt_factory=geraet.kanal,
                       neustart=geraet.neustart if neustart is None else neustart, status_sink=sammler.senke(1))
    return bridge, geraet


DEVICE_ID = "shg-aaaaaaaaaaaaaaaa"
CLAIM_CODE = "ABCDEFGHIJKL"


class FakeThread:
    def __init__(self, order):
        self._order = order

    def join(self, timeout=None):
        self._order.append("join")

    def is_alive(self):
        return False


class FakeDevice:
    """Sicht des Geraetekerns, die HaDeviceHost, HaHost und main brauchen; protokolliert start/stop."""

    def __init__(self, konfiguration=None, bedienung=None, order=None):
        self.konf, self.bed = konfiguration, bedienung
        self.order = order if order is not None else []
        self.identity = SimpleNamespace(device_id=DEVICE_ID, claim_code=CLAIM_CODE)
        self.documents = SimpleNamespace(aktuell=lambda art: None)
        self.sent = []
        self.aufgaben = []

    def im_geraete_thread(self, aufgabe):
        self.aufgaben.append(aufgabe)

    def konfiguration(self):
        return self.konf

    def bedienung(self):
        return self.bed

    def abo_status(self, kreis=None):
        return dokument_config.abo_status(self.konf, kreis)

    def start(self):
        self.order.append("start")
        return FakeThread(self.order)

    def stop(self):
        self.order.append("stop")

    def laufzeit_kanal(self, worker, kreis):
        return RuntimeChannel(lambda name, payload: self.sent.append(name) or len(self.sent), lambda: True,
                              lambda: 0).binden(worker)


class FakeSupervisor:
    def __init__(self):
        self.gemeldet = []

    def host_info(self):
        return {"dt_synchronized": True}

    def discovery_anmelden(self, config_):
        self.gemeldet.append(dict(config_))
        return f"uuid-{len(self.gemeldet)}"

    def discovery_entfernen(self, uuid):
        pass


class Supervisor:
    """Grenze Supervisor-HTTP: host_info (Uhr) und die Discovery-Meldungen (gemeldet, in Reihenfolge)."""

    def __init__(self, info=None, error=None):
        self.info, self.error, self.calls = info or {"dt_synchronized": True}, error, 0
        self.gemeldet: list[dict] = []

    def host_info(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.info

    def discovery_anmelden(self, config_):
        self.gemeldet.append(dict(config_))
        return f"uuid-{len(self.gemeldet)}"

    def discovery_entfernen(self, uuid):
        pass


class Doc:
    def __init__(self, dirty):
        self.dirty = dirty


class Device:
    def __init__(self, konf=None, bed=None, dirty=False):
        self.wuensche, self.aus, self.konf, self.bed, self.dirty = [], [], konf, bed, dirty
        self.wunsch_kreise = []
        self.meldungen = []
        self.documents = self

    def wunsch(self, wert, *, herkunft, durch=None, kreis=1):
        self.wuensche.append((wert, herkunft, durch))
        self.wunsch_kreise.append(kreis)
        return min(max(float(wert), 15.0), 25.0)

    def soll_quelle_aus(self, aus, wirksam=None):
        self.aus.append((aus, wirksam))

    def konfiguration(self):
        return self.konf

    def bedienung(self):
        return self.bed

    def aktuell(self, art):
        return Doc(self.dirty) if art == "bedienung" and self.bed is not None else None

    def abo_status(self, kreis=None):
        return "aktiv"

    def meldung(self, key, text, *, offen, stufe):
        self.meldungen.append((key, text, offen, stufe))


def _paths(tmp_path, monkeypatch):
    for name in ("RUNTIME_DIR", "ABGEMELDET_PATH", "AUFRAEUMEN_PATH", "ALTER_BACKUP_PATH",
                 "RASTUNG_PATH"):
        monkeypatch.setattr(config, name, tmp_path / getattr(config, name).name)


def _host(tmp_path, monkeypatch, supervisor=None, device=None, ha=None):
    _paths(tmp_path, monkeypatch)
    clock = FakeClock()
    host = geraet_host.HaDeviceHost(ha or FakeHa(), supervisor or Supervisor(), clock=clock)
    device = device or Device()
    host.verbinden(device)
    return host, device, clock


def _register(host, tmp_path):
    register = CommandRegister(tmp_path, lambda body: True)
    host.befehle_eintragen(register)
    return register


THERMOSTAT = "0x00124b0000000002"


def gateway_konfiguration(**changes):
    value = {
        "anlage": "kunde3", "hebelsatz": "viessmann_vicare",
        "verteilsystem": "Heizkoerper",
        "bindung": {"treiber": {"id": "simulation", "parameter": {"heizkreis": 0}}, "thermostat": THERMOSTAT},
        "raumfuehler": [SENSOR], "tagestick": "12:12", "abo": "aktiv", "mindest_software": "0.6.0", "software": None,
        "bindungsparameter": parameter("viessmann_vicare"),
    }
    value.update(changes)
    return konfig_form.nach_form(value)


def ha_konfiguration(**changes) -> dict:
    base = {
        "anlage": "zuhause2", "hebelsatz": "vaillant_vrc720",
        "verteilsystem": "Heizkoerper", "tagestick": "12:07", "abo": "aktiv",
        "bindungsparameter": parameter("vaillant_vrc720"), "raumfuehler": ["sensor.wohnzimmer_thermostat_temperatur"],
        "bindung": {
            "kreis": {"plattform": "mypyllant", "config_entry": "e1", "anlage": "S", "nummer": "0"},
            "rollen": {"room_target": "climate.wohnzimmer_thermostat::temperature",
                       "curve": "number.zuhause_circuit_0_heating_curve",
                       "room_setpoint": "climate.zuhause_zone_1_circuit_0_climate"},
            "batterien": ["sensor.wohnzimmer_thermostat_batterie"], "benachrichtigung": ["notify.mobile_app_handy"],
            "hinweise_aus": ["batterie"], "abfrage_s": 1800,
        },
    }
    bindung = {**base["bindung"], **changes.pop("bindung", {})}
    return konfig_form.nach_form({**base, **changes, "bindung": bindung})


def ha_kreis(nummer: int, **bindung) -> dict:
    """Ein weiterer HA-Kreis mit eigenen Entities (Rollen und Fuehler mit der Kreisnummer als Suffix)."""
    basis = ha_konfiguration()["kreise"][0]
    neu = copy.deepcopy(basis)
    neu["kreis"] = nummer
    neu["bindung"]["kreis"]["nummer"] = str(nummer)
    neu["bindung"]["rollen"] = {rolle: f"{ref.split('.')[0]}.{ref.split('.')[1].split('::')[0]}_{nummer}"
                                + ("::" + ref.split("::")[1] if "::" in ref else "")
                                for rolle, ref in neu["bindung"]["rollen"].items()}
    neu["bindung"].update(bindung)
    neu["raeume"][0]["fuehler"] = [f"sensor.raum_{nummer}_temperatur"]
    return neu


def ha_zwei_kreise(**changes) -> dict:
    """Konfigurationsdokument der HA-Anlage mit zwei Kreisen: Kreis 1 aus ha_konfiguration, Kreis 2 aus ha_kreis."""
    dokument = ha_konfiguration(**changes)
    dokument["kreise"] = [dokument["kreise"][0], ha_kreis(2)]
    return dokument
