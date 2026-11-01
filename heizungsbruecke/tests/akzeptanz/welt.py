"""Die schmale Welt der Akzeptanzsuite: das echte Add-on (Geraetekern, HA-Host, Laufzeit, Regel-Worker, verdrahtet wie
in __main__) auf Fake-HA, Fake-Link und gemeinsamer Fake-Uhr, ohne Netz und Threads: `Device.schritt()` und
`run_pending()` ruft die Welt selbst. Beobachtet wird nur die Aussenwirkung. Prozess 1 nimmt Dokumente an, 2 regelt."""
import datetime
import functools
import json

from anlagen import RAUMFUEHLER, konfiguration, schreibziele, zustand
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from uhr import Uhr
from welt_handgriffe import Handgriffe

import heizungsbruecke.__main__ as main_module
from heizungsbruecke import config
from heizungsbruecke.derived_sensors import DerivedSensors
from smartheat_device import bootstrap, device, identity

URL = "https://accounts.example.test"


class FakeHa:
    """Home Assistant: Zustaende, Registry, Schreibwege (Signaturen wie HomeAssistantApi, prueft conftest.py).
    `schreibvorgaenge`: (Entity, Wert, Zeit) je Schreiben (setzt den Zustand); `meldungen`: Ereignisse und Push."""

    token = "tok"

    def __init__(self, uhr, states: dict, registry: list[dict]) -> None:
        self.states, self.registry, self.uhr = dict(states), registry, uhr
        self.schreibvorgaenge: list[tuple[str, object, float]] = []
        self.meldungen: list[tuple] = []
        self.abgelehnt: list[tuple[str, object, float]] = []  # abgewiesene Schreibaufrufe; HA lehnt bis lehnt_ab_bis ab
        self.lehnt_ab_bis = 0.0

    def websocket_url(self):
        return "ws://ha.test/api/websocket"

    def entity_registry(self):
        return [dict(eintrag) for eintrag in self.registry]

    def device_registry(self):
        return []

    get_services = device_registry

    def get_config(self):
        return {"time_zone": "Europe/Berlin", "state": "RUNNING", "unit_system": {"temperature": "\u00b0C"}}

    def get_states(self):
        ids = dict.fromkeys(key.partition("::")[0] for key in self.states)
        return [{"entity_id": entity, "state": str(self.states.get(entity, "unknown")), "last_updated": "t",
                 "attributes": {k.partition("::")[2]: v for k, v in self.states.items() if k.startswith(f"{entity}::")}}
                for entity in ids]

    def get_state(self, entity_id):
        return float(self.states[entity_id])  # wie die echte API: unavailable und Text sind ungueltig (ValueError)

    def get_raw_state(self, entity_id):
        wert = self.states[entity_id]
        if wert in ("unavailable", "unknown", ""):
            raise ValueError(wert)
        return str(wert)

    def get_attribute(self, entity_id, attribute):
        return str(self.states[f"{entity_id}::{attribute}"])

    def _schreiben(self, schluessel, wert):
        if self.uhr() < self.lehnt_ab_bis:
            self.abgelehnt.append((schluessel, wert, self.uhr()))
            raise OSError("HA lehnt den Schreibaufruf ab")
        self.states[schluessel] = wert
        self.schreibvorgaenge.append((schluessel, wert, self.uhr()))

    def set_number_value(self, entity_id, value):
        self._schreiben(entity_id, value)

    def set_climate_temperature(self, entity_id, value):
        self._schreiben(f"{entity_id}::temperature", value)

    def set_hvac_mode(self, entity_id, mode):
        self._schreiben(entity_id, mode)

    def select_option(self, entity_id, option):
        self._schreiben(entity_id, option)

    def set_preset_mode(self, entity_id, preset):
        self._schreiben(f"{entity_id}::preset_mode", preset)

    def entity_exists(self, entity_id):
        return entity_id in self.states

    def is_reachable(self):
        return True

    def fire_event(self, event_type, data):
        self.meldungen.append(("ereignis", event_type, json.loads(json.dumps(data))))

    def send_notification(self, notify_service, message):
        self.meldungen.append(("push", notify_service, message))

    def create_persistent_notification(self, title, message, notification_id):
        self.meldungen.append(("dauerhaft", notification_id, message))

    def dismiss_persistent_notification(self, notification_id):
        self.meldungen.append(("zurueckgenommen", notification_id))


class FakeSupervisor:
    def host_info(self):
        return {"dt_synchronized": True}

    def discovery_anmelden(self, konfiguration):
        return "uuid-1"

    def discovery_entfernen(self, uuid):
        return None


class FakeTrigger:
    """Ersatz fuer den WebSocket-Client zu HA: verbindet nichts, haelt die Rueckrufe."""

    erzeugt: list = []

    def __init__(self, ws_url, token, triggers, on_trigger_event, on_connected=None):
        self.kwargs = {"ws_url": ws_url, "token": token, "triggers": triggers,
                       "on_trigger_event": on_trigger_event, "on_connected": on_connected}
        self.connected = True
        type(self).erzeugt.append(self)

    def start(self):
        pass

    stop = start


class FakeLink:
    """Link des Geraets zum Server: Nachrichten nach oben in `sent`, die des Servers per `empfangen` in die Inbox."""

    def __init__(self, device_id, zugang, key_pem, ereignis) -> None:
        self.ereignis, self.connected, self.sent, self.client_nr = ereignis, False, [], 1

    def start(self):
        pass

    def stop(self):
        self.connected = False

    port_wechseln_falls_noetig = start

    def publish(self, name, payload):
        if not self.connected:
            return None
        self.sent.append((name, json.loads(json.dumps(payload, allow_nan=False))))
        return len(self.sent)

    def verbinden(self):
        self.connected = True
        self.ereignis("verbunden")

    def empfangen(self, name, body):
        self.ereignis("nachricht", name, json.dumps(body).encode())

    def of(self, name):
        return [payload for topic, payload in self.sent if topic == name]


class GeraeteCa:
    """Selbstsignierte EC-CA, die das CSR des Geraets mit einem Client-Zertifikat beantwortet."""

    def __init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        self._name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "akzeptanz-ca")])
        self._cert = self._ausstellen(self._name, self._key.public_key(), ca=True)
        self.pem = self._cert.public_bytes(serialization.Encoding.PEM).decode()

    def _ausstellen(self, name, schluessel, *, ca):
        jetzt = datetime.datetime.now(datetime.UTC)
        bauer = (x509.CertificateBuilder().subject_name(name).issuer_name(self._name).public_key(schluessel)
                 .serial_number(x509.random_serial_number()).not_valid_before(jetzt - datetime.timedelta(minutes=1))
                 .not_valid_after(jetzt + datetime.timedelta(days=2))
                 .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
        if ca:
            bauer = bauer.add_extension(x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False),
                critical=True)
        else:
            bauer = bauer.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        return bauer.sign(self._key, hashes.SHA256())

    def sign_csr(self, csr_pem: str) -> str:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
        cert = self._ausstellen(csr.subject, csr.public_key(), ca=False)
        return cert.public_bytes(serialization.Encoding.PEM).decode()


def _ohne_bootstrap(ident):
    raise AssertionError("Bootstrap-Aufruf in der Akzeptanzwelt (der Zugang ist gespeichert)")


class Anlage(Handgriffe):
    """Eine Anlage (Hebelsatz x Verteilsystem) mit laufendem Add-on. `starten()` bringt sie in den Regelbetrieb,
    `server_antwortet(hebel)` ist die Antwort des Servers auf den letzten Snapshot, `runde(sekunden)` ein Durchlauf von
    Geraete-Thread und Regel-Worker mit anschliessend vorgestellter Uhr."""

    def __init__(self, tmp_path, mp, satz: str, verteilsystem: str) -> None:
        self.uhr, self.ca, self.links, self.supervisor = Uhr(), GeraeteCa(), [], FakeSupervisor()
        self.ha = FakeHa(self.uhr, zustand(satz, verteilsystem), schreibziele(satz))
        self.konfiguration = konfiguration(satz, verteilsystem)
        self.daten = tmp_path / "data"
        self.trigger: list[FakeTrigger] = []
        self._umgebung(tmp_path, mp)
        self.device = self.bridge = None

    def _umgebung(self, tmp_path, mp) -> None:
        """Dateien in tmp_path, Uhren der Welt, Ersatz fuer Netz (Link, WebSocket, Bootstrap) und Hilfssensoren."""
        pfade = {"DEVICE_DATA_DIR": self.daten, "RASTUNG_PATH": self.daten / "device" / "sicherheitssatz.json",
                 "OPTIONS_PATH": tmp_path / "options.json", "BACKUP_PATH": tmp_path / "backup.json",
                 "FAILSAFE_PATH": tmp_path / "failsafe_state.json",
                 "ENTITLEMENT_PATH": tmp_path / "entitlement_state.json",
                 "DERIVED_SENSORS_PATH": tmp_path / "derived_sensors.json",
                 "AUFRAEUMEN_PATH": tmp_path / "aufraeumen.json", "RUNTIME_DIR": tmp_path / "runtime",
                 "ROOM_TARGET_PATH": tmp_path / "runtime" / "room_target.json",
                 "ABGEMELDET_PATH": tmp_path / "runtime" / "abgemeldet.json"}
        for name, pfad in pfade.items():
            mp.setattr(config, name, pfad)
        mp.setattr(main_module.status_holder, "host", None)
        mp.setattr("smartheat_core.wallclock._now", self.uhr.wand)
        mp.setattr("heizungsbruecke.derived_sensors.ensure_all",
                   lambda **kwargs: DerivedSensors({"room_actual": RAUMFUEHLER}, (), "fp"))
        mp.setattr("heizungsbruecke.geraet_trigger.HaTriggerClient",
                   type("FakeTrigger", (FakeTrigger,), {"erzeugt": self.trigger}))

        def link(*args):
            self.links.append(FakeLink(*args))
            return self.links[-1]

        mp.setattr(main_module, "Device", functools.partial(
            device.Device, clock=self.uhr, wall=lambda: self.uhr.wand().timestamp(),
            bootstrap_client=_ohne_bootstrap, link_factory=link))
        for name in ("HaDeviceHost", "GeraetStatus", "Discovery"):
            mp.setattr(main_module, name, self._mit_uhr(getattr(main_module, name)))

    def _mit_uhr(self, klasse):
        """Die Klasse des Produkts, die ihre Uhr aus der Welt bekommt (Klassenattribute wie `host` bleiben)."""
        uhr = self.uhr
        return type("MitUhr", (klasse,), {"__init__": lambda selbst, *a, **k: klasse.__init__(selbst, *a, clock=uhr, **k)})

    def _prozess(self):
        """Ein Start wie `main` ihn vorbereitet: Geraetekern und Host auf denselben Dateien."""
        self._geraet, self.device, status = main_module._vorbereiten(self.ha, self.supervisor, URL)
        return self._geraet, status

    def _zugang(self) -> None:
        ident = identity.load_or_create(self.daten / device.DEVICE_DIR)
        zugang = bootstrap.Zugang(self.ca.sign_csr(identity.csr_pem(ident)),
                                  bootstrap.Endpoint("mqtt.e2e.test", 8883, self.ca.pem))
        bootstrap.speichern(self.daten / device.DEVICE_DIR, bootstrap.Antwort("uebernommen", zugang))

    def _verbinden(self) -> None:
        self.device.schritt()
        self.link.verbinden()
        self.device.schritt()

    def starten(self) -> None:
        """Einrichtung (Konfiguration und Bedienung vom Server), dann der Betrieb im neuen Prozess."""
        self._zugang()
        self._prozess()
        self._verbinden()
        self.link.empfangen("down/config", {"schema": 2, "version": 1, **self.konfiguration})
        self.link.empfangen("down/operation", {
            "schema": 2, "version": 1, "kreise": [{"kreis": 1, "modus": "manuell", "raum_soll": 21.0}],
            "quelle": "portal", "ts": "2027-01-15T07:00:00+00:00"})
        self.device.schritt()
        assert self.device.documents.eingerichtet()
        geraet, status = self._prozess()
        self.bridge, _ = main_module.start(geraet, self.device, self.ha, status, clock=self.uhr)
        self.bridge.worker.run_pending()
        self._verbinden()
        self.trigger[-1].kwargs["on_connected"]()
        self.runde()

    link = property(lambda self: self.links[-1])

    def server_antwortet(self, hebel: dict) -> None:
        """Antwort des Servers (setpoints) auf den letzten Snapshot; verarbeitet wird sie in der naechsten Runde."""
        self.link.empfangen("down/setpoints", {
            "schema": 4, "seq": self.link.of("up/snapshot")[-1]["seq"], "ts": "x", "status": "ok", "reason": None,
            "levers": hebel, "learned": None})

    def runde(self, sekunden: float = 0) -> None:
        self.device.schritt()
        self.bridge.worker.run_pending()
        self.uhr.jetzt += sekunden
