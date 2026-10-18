"""Spec 5b 5.1: das Geraet - Bootstrap, hello, Dokumente, Befehle, Bedienwunsch, Status, Meldungen, Rettungsweg,
Laufzeit-Kanal. Link und Bootstrap-Server sind Fakes; die Wire-Formate sind die des Vertrags."""
import json
import math
import time

import pytest
from bindung_beispiele import parameter
from device_helpers import GeraeteCa, zertifikat_fuer

from smartheat_core import config_check
from smartheat_device import bootstrap, commands, device, identity, wire
from smartheat_runtime.runtime import EV_MQTT_CONNECTED, EV_SETPOINTS
from smartheat_runtime.worker import RegulationWorker

WALL = 1_800_000_000.0
SENSOR = "zigbee:0x00124b0000000001:temperature"
SOFTWARE = {"version": "0.6.1", "manifest_url": "https://example.test/m.json", "manifest_sha256": "a" * 64,
            "signature": "sig"}


def _konfiguration(version=1, **changes):
    inhalt = {
        "anlage": "kunde3", "profil": "viessmann_gastherme_heizkoerper", "hebelsatz": "viessmann_vicare",
        "verteilsystem": "Heizkoerper",
        "bindung": {"treiber": {"id": "simulation", "parameter": {}}, "thermostat": None},
        "raumfuehler": [SENSOR], "tagestick": "12:12", "abo": "aktiv", "mindest_software": "0.6.0", "software": None,
        "bindungsparameter": parameter("viessmann_vicare"),
    }
    inhalt.update(changes)
    return {"schema": 1, "version": version, **inhalt}


def _bedienung(version=3, raum_soll=21.0):
    return {"schema": 1, "version": version, "modus": "manuell", "raum_soll": raum_soll, "quelle": "portal",
            "ts": "2027-01-15T07:00:00+00:00"}


def _command(kind="diagnostics", command_id="c1", payload=None):
    return {"schema": 1, "command_id": command_id, "kind": kind, "payload": payload or {},
            "expires_at": "2027-01-15T09:00:00+00:00"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeHost:
    host = "gateway"

    def __init__(self, version="0.6.0") -> None:
        self.version = version
        self.synchron: bool | None = True
        self.konfigurationen, self.bedienungen, self.software, self.anzeigen_liste = [], [], [], []
        self.status: dict | None = {"status": "regelt"}
        self.raum_wert: dict | None = {"ist": 20.0, "soll": 21.0, "soll_quelle": "portal", "ts": "t0"}
        self.treiber_details: dict | None = None
        self.handlers: dict = {}
        self.ursprung: dict = {}

    def ursprungswerte(self):
        return dict(self.ursprung)

    def faehigkeiten(self):
        return {"drivers": ["simulation"], "zigbee": True, "updater": True}

    def uhr_synchron(self):
        return self.synchron

    def treiber_pruefen(self, bindung):
        return self.treiber_details

    def befehle_eintragen(self, register):
        for kind, handler in self.handlers.items():
            register.register(kind, handler)

    def konfiguration_uebernommen(self, alt, neu):
        self.konfigurationen.append((alt, neu))

    def bedienung_uebernommen(self, inhalt):
        self.bedienungen.append(inhalt)

    def software_soll(self, software):
        self.software.append(software)

    def runtime_status(self):
        return self.status

    def raum(self):
        return self.raum_wert

    def anzeigen(self, anzeige):
        self.anzeigen_liste.append(anzeige)


class FakeLink:
    def __init__(self, device_id, zugang, key_pem, ereignis) -> None:
        self.device_id, self.zugang, self.key_pem, self.ereignis = device_id, zugang, key_pem, ereignis
        self.connected = self.started = self.stopped = False
        self.sent: list[tuple[str, dict]] = []
        self.connect_failures = 0

    def start(self):
        self.started = True

    def stop(self):
        self.stopped, self.connected = True, False

    def publish(self, name, payload):
        if not self.connected:
            return None
        # wie der echte Link: nie NaN/Inf hinaus (Testoffensive T2, Nr. 17), json wirft dann ValueError
        self.sent.append((name, json.loads(json.dumps(payload, allow_nan=False))))
        return len(self.sent)

    def port_wechseln_falls_noetig(self):
        pass

    # --- Teststeuerung ---

    def verbinden(self):
        self.connected = True
        self.ereignis("verbunden")

    def trennen(self):
        self.connected = False
        self.ereignis("getrennt")

    def empfangen(self, name, body):
        self.ereignis("nachricht", name, json.dumps(body).encode())

    def topics(self):
        return [name for name, _ in self.sent]

    def of(self, name):
        return [payload for topic, payload in self.sent if topic == name]


class ScriptedBootstrap:
    def __init__(self) -> None:
        self.script: dict[str, list] = {"register": [], "certificate": []}
        self.calls, self.idents = [], []

    def register(self, **kwargs):
        self.calls.append(("register", kwargs))
        return self._next("register")

    def certificate(self):
        self.calls.append(("certificate", None))
        return self._next("certificate")

    def _next(self, route):
        answer = self.script[route].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class World:
    def __init__(self, tmp_path, host=None, *, access=True) -> None:
        self.dir = tmp_path / device.DEVICE_DIR
        self.host = host or FakeHost()
        self.ca = GeraeteCa()
        self.clock = Clock()
        self.links: list[FakeLink] = []
        self.client = ScriptedBootstrap()
        ident = identity.load_or_create(self.dir)
        self.zugang = self.neuer_zugang(ident)
        if access:
            bootstrap.speichern(self.dir, bootstrap.Antwort("uebernommen", self.zugang))

        def client_for(ident):
            self.client.idents.append(ident)
            return self.client

        self.device = device.Device(self.host, tmp_path, "https://accounts.example.test", clock=self.clock,
                                    wall=lambda: WALL, bootstrap_client=client_for, link_factory=self._link)

    def neuer_zugang(self, ident=None):
        ident = ident or self.device.identity
        return bootstrap.Zugang(zertifikat_fuer(ident, self.ca), bootstrap.Endpoint("mqtt.e2e.test", 8883, self.ca.pem))

    def _link(self, *args):
        self.links.append(FakeLink(*args))
        return self.links[-1]

    @property
    def link(self) -> FakeLink:
        return self.links[-1]

    def verbinden(self):
        self.device.schritt()
        self.link.verbinden()
        self.device.schritt()

    def empfangen(self, name, body):
        self.link.empfangen(name, body)
        self.device.schritt()


# --- Ursprungswerte im Status (Spec Stufe 3 S3 5.1, G-4) ---

def test_status_carries_only_originals_the_server_does_not_know(tmp_path):
    world = World(tmp_path)
    world.host.ursprung = {"heat_limit": 16.0, "mode_select": "hz_operationmode_automatic"}
    world.verbinden()
    assert world.link.of("up/status")[-1]["ursprungswerte"] == world.host.ursprung  # Server kennt noch keinen
    world.empfangen("down/config", _konfiguration(ursprungswerte={"heat_limit": 16.0}))
    world.clock.now += device.STATUS_MIN_SECONDS
    world.device.schritt()
    assert world.link.of("up/status")[-1]["ursprungswerte"] == {"mode_select": "hz_operationmode_automatic"}


def test_status_without_new_originals_has_no_field(tmp_path):
    world = World(tmp_path)
    world.host.ursprung = {"heat_limit": 16.0}
    world.verbinden()
    world.empfangen("down/config", _konfiguration(ursprungswerte={"heat_limit": 16.0}))
    world.clock.now += device.STATUS_MIN_SECONDS
    world.device.schritt()
    statuses = world.link.of("up/status")
    assert len(statuses) == 2 and "ursprungswerte" not in statuses[-1]
    assert set(statuses[-1]) == set(wire.STATUS_FIELDS)
    world.clock.now += device.STATUS_MIN_SECONDS
    world.device.schritt()
    assert len(world.link.of("up/status")) == 2  # kein Sendeschleife, solange nichts neu ist


def test_status_respects_v1_limits(tmp_path):
    world = World(tmp_path)
    world.host.ursprung = {**{f"h{i:02d}": float(i) for i in range(20)}, "x" * 65: 1.0, "nan": math.nan,
                           "gross": 1e13, "wahr": True}
    world.verbinden()
    werte = world.link.of("up/status")[-1]["ursprungswerte"]
    assert len(werte) == wire.URSPRUNG_MAX_HEBEL and all(len(k) <= wire.URSPRUNG_HEBEL_MAX_CHARS for k in werte)
    assert all(isinstance(v, float) and math.isfinite(v) and abs(v) <= 1e12 for v in werte.values())


def test_ursprungswert_genau_auf_der_grenze_von_1e12(tmp_path):
    """URSPRUNG_WERT_MAX = 1e12 als Literal aus der Spec (nicht ueber die Konstante): beide Vorzeichen gehen hinaus,
    ein Hauch darueber nicht."""
    assert wire.URSPRUNG_WERT_MAX == 1e12
    world = World(tmp_path)
    world.host.ursprung = {"plus": 1e12, "minus": -1e12, "plus_drueber": 1.0000001e12, "minus_drueber": -1.0000001e12}
    world.verbinden()
    assert world.link.of("up/status")[-1]["ursprungswerte"] == {"plus": 1e12, "minus": -1e12}


def test_unreportable_originals_are_logged_once_per_name_without_values(tmp_path, caplog):
    world = World(tmp_path)
    world.host.ursprung = {**{f"h{i:02d}": float(i) for i in range(17)}, "leer": "", "gross": 1e13,
                           "riesig": 10 ** 400, "x" * 65: "geheimerwert"}
    with caplog.at_level("WARNING", logger=device.logger.name):
        world.verbinden()
        for _ in range(5):  # Takt: _status_senden laeuft jede Sekunde
            world.clock.now += 1
            world.device.schritt()
    text = caplog.text
    for name in ("leer", "gross", "riesig", "h16"):  # h16 ist der 17. gueltige Name: ueber dem Limit
        assert text.count(f"Ursprungswert {name} ") == 1, name
    assert "Ursprungswert ? " in text and "geheimerwert" not in text and "1e+13" not in text
    assert len(caplog.records) == 5  # leer, gross, riesig, ?, h16 - nicht je Takt
    assert len(world.link.of("up/status")[-1]["ursprungswerte"]) == wire.URSPRUNG_MAX_HEBEL


# --- Start, hello ---

def test_first_start_registers_then_connects_and_says_hello(tmp_path):
    world = World(tmp_path, access=False)
    world.client.script["register"] = [bootstrap.Antwort("nicht_uebernommen", world.zugang)]
    world.device.schritt()
    assert [call[0] for call in world.client.calls] == ["register"] and world.link.started
    assert world.client.calls[0][1] == {"version": "0.6.0", "host": "gateway",
                                        "capabilities": world.host.faehigkeiten()}
    world.link.verbinden()
    world.device.schritt()
    (hello,) = world.link.of("up/hello")
    assert set(hello) == set(wire.HELLO_FIELDS) | {"device_state"}
    assert hello == {"schema": 1, "schemata": [1], "konfiguration_version": 0, "bedienung_version": 0,
                     "software_version": "0.6.0", "host": "gateway", "faehigkeiten": world.host.faehigkeiten(),
                     "boot_id": world.device.boot_id, "uhr_synchron": True, "device_state": "nicht_uebernommen"}
    assert world.device.zustand() == "nicht_uebernommen"


def test_stored_access_skips_the_registration(tmp_path):
    world = World(tmp_path)
    world.device.schritt()
    assert world.client.calls == [] and world.link.zugang == world.zugang


def test_hello_comes_first_and_an_unknown_clock_counts_as_unsynchronised(tmp_path):
    world = World(tmp_path)
    world.host.synchron = None
    world.verbinden()
    assert world.link.topics()[:2] == ["up/hello", "up/status"]
    assert world.link.of("up/hello")[0]["uhr_synchron"] is False


def test_hello_after_a_reconnect_reports_the_taken_versions(tmp_path):
    world = World(tmp_path)
    world.verbinden()
    world.empfangen("down/config", _konfiguration(version=4))
    world.empfangen("down/operation", _bedienung(version=2))
    world.link.trennen()
    world.link.verbinden()
    world.device.schritt()
    hello = world.link.of("up/hello")[-1]
    assert (hello["konfiguration_version"], hello["bedienung_version"]) == (4, 2)


# --- Dokumente ---

def test_a_configuration_is_taken_reported_and_handed_to_the_host(tmp_path):
    world = World(tmp_path)
    world.verbinden()
    world.empfangen("down/config", _konfiguration())
    assert world.link.of("up/result") == [{"schema": 1, "dokument": "konfiguration", "version": 1, "ok": True}]
    ((alt, neu),) = world.host.konfigurationen
    assert alt is None and neu["anlage"] == "kunde3" and world.host.software == [None]
    assert world.device.konfiguration() == neu and world.device.abo_status() == "active"
    assert bootstrap.device_state(world.dir) == "uebernommen"


def test_a_rejected_configuration_keeps_the_old_one_and_tells_the_display(tmp_path):
    world = World(tmp_path)
    world.verbinden()
    world.host.treiber_details = {"feld": "parameter", "regel": "ungueltig"}
    world.empfangen("down/config", _konfiguration())
    (result,) = world.link.of("up/result")
    assert result["ok"] is False and result["error"] == {
        "grund": "treiber_parameter", "details": {"feld": "parameter", "regel": "ungueltig"}}
    assert world.host.konfigurationen == []
    assert world.host.anzeigen_liste[-1].konfiguration_abgelehnt == device.ABLEHNUNG_ANZEIGE.format(
        grund="treiber_parameter")


def test_a_rejection_from_before_5c_shows_the_local_hint_with_the_code_and_reports_no_text(tmp_path):
    """dokumente.json von vor 5c traegt einen Text an Stelle 2: der lokale Hinweis nennt nur den Code (Spec 6.3, E2 A)."""
    (tmp_path / device.DEVICE_DIR).mkdir(parents=True)
    (tmp_path / device.DEVICE_DIR / "dokumente.json").write_text(json.dumps(
        {"update_anlass": None, "abgelehnt": {"konfiguration": [3, "ungueltig", "Alter Gerätetext"]}}))
    world = World(tmp_path)
    hinweis = world.device.anzeige().konfiguration_abgelehnt
    assert hinweis == device.ABLEHNUNG_ANZEIGE.format(grund="ungueltig") and "Alter" not in hinweis
    assert world.device.documents.ablehnung("konfiguration") == (3, "ungueltig", None)


class UnpruefbarHost(FakeHost):
    """Der Treiber-Rueckruf kann nicht entscheiden (HA aus), solange `unpruefbar` gesetzt ist."""
    unpruefbar = True
    aufrufe = 0

    def treiber_pruefen(self, bindung):
        self.aufrufe += 1
        if self.unpruefbar:
            raise config_check.NichtPruefbar
        return self.treiber_details


def test_an_unverifiable_configuration_is_neither_taken_nor_refused_and_is_checked_again_later(tmp_path):
    """Spec 5c 1.3: HA nicht erreichbar -> keine Ablehnung (der Server stellt eine abgelehnte Version nicht erneut zu),
    das Geraet haelt das Dokument zurueck und prueft nach dem Wiederholabstand erneut, ohne neue Zustellung."""
    world = World(tmp_path, UnpruefbarHost())
    world.verbinden()
    world.empfangen("down/config", _konfiguration())
    assert world.link.of("up/result") == [] and world.host.konfigurationen == []
    assert world.device.documents.version("konfiguration") == 0 and world.device.documents.ablehnung("konfiguration") is None
    world.clock.now += device.PRUEFUNG_WIEDERHOLEN_SECONDS - 1
    world.device.schritt()
    assert world.host.aufrufe == 1  # noch zu frueh
    world.clock.now += 1
    world.device.schritt()
    assert world.host.aufrufe == 2 and world.link.of("up/result") == []  # HA noch weg: weiter zurueckgehalten
    world.host.unpruefbar = False
    world.clock.now += device.PRUEFUNG_WIEDERHOLEN_SECONDS
    world.device.schritt()
    assert world.link.of("up/result") == [{"schema": 1, "dokument": "konfiguration", "version": 1, "ok": True}]
    assert len(world.host.konfigurationen) == 1 and world.host.software == [None]
    calls = world.host.aufrufe
    world.clock.now += 10 * device.PRUEFUNG_WIEDERHOLEN_SECONDS
    world.device.schritt()
    assert world.host.aufrufe == calls and len(world.link.of("up/result")) == 1  # erledigt, kein weiterer Versuch


def test_an_unverifiable_configuration_that_gets_refused_later_says_so_once(tmp_path):
    world = World(tmp_path, UnpruefbarHost())
    world.verbinden()
    world.empfangen("down/config", _konfiguration())
    world.host.unpruefbar, world.host.treiber_details = False, {"feld": "parameter", "regel": "ungueltig"}
    world.clock.now += device.PRUEFUNG_WIEDERHOLEN_SECONDS
    world.device.schritt()
    (result,) = world.link.of("up/result")
    assert result["ok"] is False and result["error"]["grund"] == "treiber_parameter"


def test_a_redelivery_or_a_newer_version_replaces_the_held_configuration(tmp_path):
    world = World(tmp_path, UnpruefbarHost())
    world.verbinden()
    world.empfangen("down/config", _konfiguration(version=1))
    world.link.trennen()
    world.verbinden()  # nach dem Neuverbinden stellt der Server dieselbe Version erneut zu
    world.host.unpruefbar = False
    world.empfangen("down/config", _konfiguration(version=2, tagestick="13:00"))
    assert [r["version"] for r in world.link.of("up/result")] == [2]
    world.clock.now += 10 * device.PRUEFUNG_WIEDERHOLEN_SECONDS
    world.device.schritt()
    assert [r["version"] for r in world.link.of("up/result")] == [2]  # Version 1 wurde ersetzt, nicht nachgeprueft
    assert world.device.documents.version("konfiguration") == 2


def test_a_held_configuration_is_only_rechecked_while_connected(tmp_path):
    world = World(tmp_path, UnpruefbarHost())
    world.verbinden()
    world.empfangen("down/config", _konfiguration())
    world.host.unpruefbar = False
    world.link.trennen()
    world.clock.now += 10 * device.PRUEFUNG_WIEDERHOLEN_SECONDS
    world.device.schritt()
    assert world.host.konfigurationen == []
    world.link.verbinden()
    world.device.schritt()
    assert world.link.of("up/result") == [{"schema": 1, "dokument": "konfiguration", "version": 1, "ok": True}]


def test_software_reaches_the_host_even_when_the_device_is_too_old(tmp_path):
    world = World(tmp_path, FakeHost(version="0.5.9"))
    world.verbinden()
    world.empfangen("down/config", _konfiguration(software=SOFTWARE))
    assert world.link.of("up/result")[0]["error"]["grund"] == "update_noetig"
    assert world.host.software == [SOFTWARE] and world.host.konfigurationen == []
    assert world.device.zustand() == "update_noetig"


def test_a_valid_software_offer_reaches_the_host_from_a_rejected_configuration(tmp_path):
    """R15: ein Update kann der Ausweg aus einer Ablehnung sein."""
    world = World(tmp_path)
    world.verbinden()
    world.empfangen("down/config", _konfiguration(hebelsatz="gibt_es_nicht", software=SOFTWARE))
    assert world.link.of("up/result")[0]["ok"] is False
    assert world.host.software == [SOFTWARE] and world.host.konfigurationen == []


@pytest.mark.parametrize("software", [{**SOFTWARE, "extra": 1}, {"version": "0.6.1"}, "0.6.1", ["x"]])
def test_an_invalid_software_offer_does_not_reach_the_host_and_keeps_the_previous_one(tmp_path, software):
    world = World(tmp_path)
    world.verbinden()
    world.empfangen("down/config", _konfiguration(software=SOFTWARE))
    world.empfangen("down/config", _konfiguration(version=2, software=software))
    assert [r["error"]["grund"] for r in world.link.of("up/result") if not r["ok"]] == ["ungueltig"]
    assert world.host.software == [SOFTWARE]


def test_a_configuration_without_the_software_field_leaves_the_offer_alone(tmp_path):
    world = World(tmp_path)
    world.verbinden()
    world.empfangen("down/config", _konfiguration(software=SOFTWARE))
    konfiguration = _konfiguration(version=2)
    del konfiguration["software"]
    world.empfangen("down/config", konfiguration)
    assert world.host.software == [SOFTWARE]


def test_software_null_withdraws_a_previously_delivered_offer(tmp_path):
    world = World(tmp_path)
    world.verbinden()
    world.empfangen("down/config", _konfiguration(software=SOFTWARE))
    world.empfangen("down/config", _konfiguration(version=2, software=None))
    assert world.host.software == [SOFTWARE, None]
    assert [r["ok"] for r in world.link.of("up/result")] == [True, True]


def test_an_empty_configuration_is_handed_over(tmp_path):
    world = World(tmp_path)
    world.verbinden()
    world.empfangen("down/config", _konfiguration())
    leer = {**_konfiguration(version=2), "anlage": None, "profil": None, "hebelsatz": None, "verteilsystem": None,
            "bindung": None, "raumfuehler": [], "tagestick": None, "abo": None}
    world.empfangen("down/config", leer)
    assert world.host.konfigurationen[-1][1]["anlage"] is None and world.device.abo_status() == "unknown"


def test_an_operation_goes_to_the_host(tmp_path):
    world = World(tmp_path)
    world.verbinden()
    world.empfangen("down/operation", _bedienung(raum_soll=21.5))
    assert [b["raum_soll"] for b in world.host.bedienungen] == [21.5] and world.device.bedienung()["raum_soll"] == 21.5


def test_unknown_topics_and_broken_messages_are_ignored(tmp_path):
    world = World(tmp_path)
    world.verbinden()
    world.empfangen("down/zukunft", {"x": 1})
    for raw in (b"{kaputt", b"[1, 2]", b"x" * (wire.MAX_MESSAGE_BYTES + 1)):
        world.link.ereignis("nachricht", "down/config", raw)
    world.device.schritt()
    assert world.link.of("up/result") == [] and world.host.konfigurationen == []


@pytest.mark.parametrize("name", wire.DOWN_TOPICS)
def test_every_contract_down_topic_is_assigned(tmp_path, name):
    world = World(tmp_path)
    world.verbinden()
    seen = []
    world.device._dokument = lambda art, body: seen.append(("dokument", art))
    world.device.befehle.empfangen = lambda body: seen.append(("befehl", None))
    world.device._channel = type("Kanal", (), {"setpoints": lambda self, body: seen.append(("sollwerte", None))})()
    world.empfangen(name, {"x": 1})
    erwartet = {topic: ("dokument", art) for art, topic in wire.DOKUMENT_TOPICS.items()}
    erwartet.update({"down/command": ("befehl", None), "down/setpoints": ("sollwerte", None)})
    assert seen == [erwartet[name]]


def test_a_foreign_down_topic_reaches_nobody(tmp_path, caplog):
    world = World(tmp_path)
    world.verbinden()
    seen = []
    world.device._dokument = lambda art, body: seen.append(art)
    world.device.befehle.empfangen = seen.append
    with caplog.at_level("WARNING", logger=device.logger.name):
        world.empfangen("down/zukunft", _command())
        world.link.ereignis("nachricht", "down/zukunft", b"{kaputt")
        world.device.schritt()
    assert seen == [] and caplog.text == ""  # unbekannt: weder gelesen noch beanstandet


# --- Befehle ---

def test_commands_run_once_across_reconnects(tmp_path):
    host, calls = FakeHost(), []
    host.handlers = {"diagnostics": lambda payload: calls.append(1) or commands.Done({"a": 1})}
    world = World(tmp_path, host)
    world.verbinden()
    world.empfangen("down/command", _command())
    world.link.trennen()
    world.link.verbinden()
    world.empfangen("down/command", _command())
    assert calls == [1]
    assert world.link.of("up/result") == [commands.result_payload("c1", True, {"a": 1}, None)] * 2


def test_a_hint_command_reaches_the_display(tmp_path):
    world = World(tmp_path)
    world.verbinden()
    world.empfangen("down/command", _command("hinweis", payload={"key": "geraet_doppelt", "stufe": "kritisch",
                                                                 "text": "Kopie stoppen"}))
    assert world.host.anzeigen_liste[-1].hinweise == (
        {"key": "geraet_doppelt", "stufe": "kritisch", "text": "Kopie stoppen"},)


def test_inventory_parts_go_up_before_the_result(tmp_path):
    host = FakeHost()
    host.handlers = {"inventory": lambda payload: commands.Done({}, inventur={"kandidaten": [1, 2]})}
    world = World(tmp_path, host)
    world.verbinden()
    world.empfangen("down/command", _command("inventory", payload={"stunden": 1}))
    names = world.link.topics()
    assert names.index("up/inventory") < names.index("up/result")
    assert world.link.of("up/inventory") == [{"schema": 1, "command_id": "c1", "teil": 1, "teile": 1,
                                              "daten": {"kandidaten": [1, 2]}}]


def test_an_inventory_outside_the_contract_fails_intern_without_a_value(tmp_path, caplog, monkeypatch):
    host = FakeHost()
    host.handlers = {"inventory": lambda payload: commands.Done({}, inventur={"kandidaten": ["WERT-123"]})}
    world = World(tmp_path, host)
    world.verbinden()
    monkeypatch.setattr(wire, "INVENTORY_FIELDS", (*wire.INVENTORY_FIELDS, "neu"))
    world.empfangen("down/command", _command("inventory", payload={"stunden": 1}))
    assert world.link.of("up/inventory") == []
    assert world.link.of("up/result")[0]["error"] == {"grund": "intern"}
    assert "Vertragsfehler" in caplog.text and "WERT-123" not in caplog.text


def test_host_exceptions_become_contract_reasons(tmp_path):
    class DriverDown(Exception):
        pass

    def erkennung(payload):
        raise DriverDown()

    host = FakeHost()
    host.handlers = {"inventory": erkennung}
    world = World(tmp_path, host)
    world.device = device.Device(host, tmp_path, "https://accounts.example.test", clock=world.clock, wall=lambda: WALL,
                                 bootstrap_client=lambda ident: world.client, link_factory=world._link,
                                 befehl_fehler=lambda error: commands.Failed("anlage_nicht_erreichbar")
                                 if isinstance(error, DriverDown) else None)
    world.verbinden()
    world.empfangen("down/command", _command("inventory", payload={"stunden": 0, "filter": {"treiber": "simulation"}}))
    assert world.link.of("up/result")[0]["error"] == {"grund": "anlage_nicht_erreichbar"}


def test_software_results_go_up_only_after_hello(tmp_path):
    world = World(tmp_path)
    world.device.schritt()
    assert world.device.ergebnis_software("0.6.1", True) is False
    world.verbinden()
    assert world.device.ergebnis_software("0.6.1", False, "ungesund") is True
    assert world.link.of("up/result") == [{"schema": 1, "dokument": "software", "version": "0.6.1", "ok": False,
                                           "error": {"grund": "ungesund"}}]
    with pytest.raises(ValueError):
        world.device.ergebnis_software("0.6.1", False, "erfunden")


def test_new_claim_code_only_while_not_claimed(tmp_path):
    world = World(tmp_path)
    world.client.script["register"] = [bootstrap.Antwort("nicht_uebernommen", world.zugang),
                                       bootstrap.Antwort("uebernommen", world.zugang)]
    old = world.device.identity.claim_code
    world.verbinden()
    world.empfangen("down/command", _command("new_claim_code", "c1"))
    new = world.device.identity.claim_code
    assert new != old and world.client.idents[-1].claim_code == new
    assert identity.load_or_create(world.dir).claim_code == new
    world.empfangen("down/command", _command("new_claim_code", "c2"))
    results = world.link.of("up/result")
    assert results[0]["ok"] is True and results[1]["error"]["grund"] == "bereits_uebernommen"
    assert world.device.identity.claim_code == new


def test_hello_reports_the_claim_state_the_device_knows(tmp_path):
    world = World(tmp_path, access=False)
    world.client.script["register"] = [bootstrap.Antwort("nicht_uebernommen", world.zugang)]
    world.verbinden()
    (hello,) = world.link.of("up/hello")
    assert hello["device_state"] == bootstrap.device_state(world.dir) == "nicht_uebernommen"
    assert set(hello) == set(wire.HELLO_FIELDS) | set(wire.HELLO_OPTIONAL)


def test_claim_state_hides_the_code_and_waits_for_setup(tmp_path):
    """Spec 5c 1.1/F1: nach der Uebernahme ohne Code melden; der Zustand wird wartet_auf_einrichtung."""
    world = World(tmp_path, access=False)
    world.client.script["register"] = [bootstrap.Antwort("nicht_uebernommen", world.zugang)]
    world.verbinden()
    assert world.device.anzeige().code_zeigen is True
    world.empfangen("down/command", _command("claim_state", "c1", {"device_state": "uebernommen"}))
    anzeige = world.device.anzeige()
    assert (anzeige.device_state, anzeige.code_zeigen, anzeige.zustand) == (
        "uebernommen", False, "wartet_auf_einrichtung")
    assert world.link.of("up/result")[-1]["ok"] is True


@pytest.mark.parametrize("value", ["gesperrt", "nicht_uebernommen"])
def test_claim_state_accepts_only_uebernommen(tmp_path, value):
    """Final-Review F-3: frei wird ein Geraet nur ueber new_claim_code (neuer Code); claim_state nicht_uebernommen
    zeigte sonst nach einer Uebernahme den alten Code wieder an."""
    world = World(tmp_path, access=False)
    world.client.script["register"] = [bootstrap.Antwort("nicht_uebernommen", world.zugang)]
    world.verbinden()
    world.empfangen("down/command", _command("claim_state", "c1", {"device_state": "uebernommen"}))
    world.empfangen("down/command", _command("claim_state", "c2", {"device_state": value}))
    assert world.link.of("up/result")[-1]["error"] == {
        "grund": "ungueltige_nutzlast", "details": {"feld": "device_state", "regel": "unbekannt"}}
    assert bootstrap.device_state(world.dir) == "uebernommen"
    assert world.device.anzeige().code_zeigen is False


def test_after_release_the_new_code_is_shown_again(tmp_path):
    """Freigabe: der Server schickt new_claim_code; register antwortet nicht_uebernommen -> neuer Code sichtbar."""
    world = World(tmp_path, access=False)
    world.client.script["register"] = [bootstrap.Antwort("nicht_uebernommen", world.zugang),
                                       bootstrap.Antwort("nicht_uebernommen", world.zugang)]
    world.verbinden()
    world.empfangen("down/command", _command("claim_state", "c1", {"device_state": "uebernommen"}))
    old = world.device.identity.claim_code
    world.empfangen("down/command", _command("new_claim_code", "c2"))
    anzeige = world.device.anzeige()
    assert anzeige.code_zeigen is True and world.device.identity.claim_code != old


# --- Bedienwunsch ---

def test_a_wish_is_limited_applied_buffered_and_sent_after_hello(tmp_path):
    world = World(tmp_path)
    world.verbinden()
    world.empfangen("down/operation", _bedienung(version=3))
    world.link.trennen()
    assert world.device.wunsch(14.0, herkunft="thermostat", durch="nutzer") == 15.0
    world.device.schritt()
    assert world.link.of("up/wish") == []
    assert world.host.anzeigen_liste[-1].meldungen == (
        {"key": "wunsch_begrenzt", "text": "Thermostat auf 14 °C gestellt, SmartHeat regelt auf 15 °C"},)
    world.link.sent.clear()
    world.link.verbinden()
    world.device.schritt()
    names = world.link.topics()
    assert names[0] == "up/hello" and "up/wish" in names
    (sent,) = world.link.of("up/wish")
    assert (sent["basis_version"], sent["werte"], sent["roh"], sent["herkunft"], sent["durch"]) == (
        3, {"raum_soll": 15.0}, 14.0, "thermostat", "nutzer")
    world.link.ereignis("gesendet", names.index("up/wish") + 1)
    world.device.schritt()
    assert world.device.wunsch_puffer.wunsch is None


def test_losing_wish_takes_the_resent_operation(tmp_path):
    world = World(tmp_path)
    world.verbinden()
    world.empfangen("down/operation", _bedienung(version=3, raum_soll=21.0))
    world.device.wunsch(22.0, herkunft="thermostat")
    world.device.schritt()
    assert world.device.documents.aktuell("bedienung").dirty
    world.empfangen("down/operation", _bedienung(version=3, raum_soll=21.0))  # Server: Wunsch verliert
    assert [b["raum_soll"] for b in world.host.bedienungen] == [21.0, 21.0]
    assert not world.device.documents.aktuell("bedienung").dirty


def test_a_wish_within_the_range_clears_the_limit_message(tmp_path):
    world = World(tmp_path)
    world.device.wunsch(30.0, herkunft="thermostat")
    world.device.wunsch(21.0, herkunft="thermostat")
    world.device.schritt()
    assert world.host.anzeigen_liste[-1].meldungen == ()


def test_a_wish_outside_the_contract_is_logged_once_and_the_tick_goes_on(tmp_path, caplog, monkeypatch):
    world = World(tmp_path)
    world.verbinden()
    monkeypatch.setattr(wire, "WISH_FIELDS", (*wire.WISH_FIELDS, "neu"))
    world.device.wunsch(22.5, herkunft="thermostat")
    anzahl = len(world.link.of("up/status"))
    world.clock.now += device.STATUS_MIN_SECONDS
    world.host.raum_wert = {"ist": 20.0, "soll": 22.5, "soll_quelle": "geraet", "ts": "t1"}
    world.device.schritt()
    world.device.schritt()
    assert world.link.of("up/wish") == [] and caplog.text.count("up/wish nicht gebaut") == 1
    assert "22.5" not in caplog.text
    assert len(world.link.of("up/status")) == anzahl + 1  # der Rest des Takts laeuft weiter
    monkeypatch.undo()
    world.device.schritt()
    assert len(world.link.of("up/wish")) == 1  # der gepufferte Wunsch geht beim naechsten gueltigen Takt hoch


def test_wishes_that_are_no_number_are_refused(tmp_path):
    with pytest.raises(ValueError):
        World(tmp_path).device.wunsch(math.nan, herkunft="thermostat")


def test_setpoint_source_off_is_shown_and_cleared(tmp_path):
    world = World(tmp_path)
    world.device.soll_quelle_aus(True, 21.0)
    world.device.schritt()
    assert world.host.anzeigen_liste[-1].meldungen == ({"key": "soll_quelle_aus", "text": (
        "Thermostat ist aus, SmartHeat regelt weiter auf 21 °C; Heizung aus über die Sommersperre bzw. im Portal")},)
    world.device.soll_quelle_aus(False)
    world.device.schritt()
    assert world.host.anzeigen_liste[-1].meldungen == ()


# --- Status und Meldungen ---

def test_status_only_on_change_and_at_most_every_10_seconds(tmp_path):
    world = World(tmp_path)
    world.verbinden()
    assert len(world.link.of("up/status")) == 1  # nach hello
    world.device.schritt()
    assert len(world.link.of("up/status")) == 1
    world.host.status = {"status": "notbetrieb"}
    world.device.schritt()
    assert len(world.link.of("up/status")) == 1  # binnen 10 s
    world.clock.now += device.STATUS_MIN_SECONDS
    world.device.schritt()
    statuses = world.link.of("up/status")
    assert len(statuses) == 2 and statuses[-1]["runtime_status"] == {"status": "notbetrieb"}
    assert set(statuses[-1]) == set(wire.STATUS_FIELDS) and statuses[-1]["zustand"] in wire.ZUSTAENDE


def test_status_ignores_the_timestamp_of_the_room(tmp_path):
    world = World(tmp_path)
    world.verbinden()
    world.host.raum_wert = {**world.host.raum_wert, "ts": "t1"}
    world.clock.now += 60
    world.device.schritt()
    assert len(world.link.of("up/status")) == 1


def test_notifications_are_batched_and_sent_again_after_a_reconnect(tmp_path):
    world = World(tmp_path)
    world.verbinden()
    world.device.meldung("notbetrieb", "Notbetrieb aktiv", offen=True)
    world.device.meldung("raumfuehler:0x1", "Fühler weg", offen=True, kritisch=False)
    world.device.schritt()
    (batch,) = world.link.of("up/notifications")
    assert [item["key"] for item in batch["items"]] == ["notbetrieb", "raumfuehler:0x1"]
    assert batch["items"][1]["kategorie"] == "raumfuehler" and set(batch["items"][0]) == set(wire.NOTIFICATION_FIELDS)
    world.device.meldung("notbetrieb", "", offen=False)
    world.device.schritt()
    assert len(world.link.of("up/notifications")) == 1  # binnen 10 s
    world.clock.now += device.MELDUNGEN_MIN_SECONDS
    world.device.schritt()
    assert [(i["key"], i["offen"]) for i in world.link.of("up/notifications")[-1]["items"]] == [("notbetrieb", False)]
    world.link.trennen()
    world.link.verbinden()
    world.device.schritt()
    assert [i["key"] for i in world.link.of("up/notifications")[-1]["items"]] == ["raumfuehler:0x1"]


# --- Rettungsweg ---

def test_three_auth_failures_take_the_rescue_path_and_reconnect_with_a_new_certificate(tmp_path):
    world = World(tmp_path)
    world.device.schritt()
    first = world.link
    neu = world.neuer_zugang()
    world.client.script["certificate"] = [bootstrap.Antwort("uebernommen", neu)]
    first.ereignis("anmeldung_scheitert")
    world.device.schritt()
    assert [call[0] for call in world.client.calls] == ["certificate"]
    assert first.stopped and world.link is not first and world.link.zugang == neu and world.link.started


def test_a_blocked_device_never_shows_the_claim_code(tmp_path):
    """Die Sperre steht nur im Speicher; device_state bleibt auf der Platte nicht_uebernommen (Spec 5c F1)."""
    world = World(tmp_path, access=False)
    world.client.script["register"] = [bootstrap.Antwort("nicht_uebernommen", world.zugang),
                                       bootstrap.NotAuthenticated("r")]
    world.client.script["certificate"] = [bootstrap.NotAuthenticated("c")]
    world.verbinden()
    assert world.device.anzeige().code_zeigen is True
    world.link.ereignis("anmeldung_scheitert")
    world.device.schritt()
    anzeige = world.device.anzeige()
    assert bootstrap.device_state(world.dir) == "nicht_uebernommen"
    assert (anzeige.zustand, anzeige.code_zeigen) == ("gesperrt", False)


def test_a_blocked_device_stops_its_link_shows_blocked_and_retries_after_6_hours(tmp_path):
    world = World(tmp_path)
    world.device.schritt()
    first = world.link
    world.client.script["certificate"] = [bootstrap.NotAuthenticated("c"), bootstrap.Antwort("uebernommen",
                                                                                              world.zugang)]
    world.client.script["register"] = [bootstrap.NotAuthenticated("r")]
    first.ereignis("anmeldung_scheitert")
    world.device.schritt()
    assert first.stopped and world.device.zustand() == "gesperrt"
    assert world.host.anzeigen_liste[-1].zustand == "gesperrt" and len(world.links) == 1
    world.clock.now += bootstrap.GESPERRT_SECONDS
    world.device.schritt()
    assert len(world.links) == 2 and world.device.zustand() != "gesperrt"


class BrokenLink(FakeLink):
    def start(self):
        raise ValueError("Zugang unbrauchbar")  # wie TransportConfigError


def test_an_unusable_access_backs_off_instead_of_fetching_a_certificate_every_tick(tmp_path):
    world = World(tmp_path)
    world.client.script["certificate"] = [bootstrap.Antwort("uebernommen", world.zugang),
                                          bootstrap.Antwort("uebernommen", world.zugang)]

    def broken(*args):
        world.links.append(BrokenLink(*args))
        return world.links[-1]

    world.device = device.Device(world.host, tmp_path, "https://accounts.example.test", clock=world.clock,
                                 wall=lambda: WALL, bootstrap_client=lambda ident: world.client, link_factory=broken)

    def certificates():
        return sum(1 for call in world.client.calls if call[0] == "certificate")

    for _ in range(10):
        world.device.schritt()
    assert certificates() <= 1
    world.clock.now += bootstrap.RETRY_MIN_SECONDS
    world.device.schritt()
    assert certificates() == 1
    for _ in range(10):
        world.device.schritt()
    assert certificates() == 1  # neuer Zugang ebenfalls unbrauchbar: wieder Backoff, nicht jeden Takt
    world.clock.now += bootstrap.RETRY_MIN_SECONDS
    world.device.schritt()
    assert certificates() == 2 and world.device.link is None


def test_rescue_with_an_unsynchronised_clock_keeps_the_link(tmp_path):
    world = World(tmp_path)
    world.host.synchron = False
    world.device.schritt()
    world.client.script["certificate"] = [bootstrap.NotAuthenticated("c")]
    world.client.script["register"] = [bootstrap.NotAuthenticated("r")]
    world.link.ereignis("anmeldung_scheitert")
    world.device.schritt()
    assert not world.link.stopped and world.device.zustand() != "gesperrt"


def test_a_failed_rescue_ends_when_the_link_connects_normally(tmp_path):
    world = World(tmp_path)
    world.host.synchron = False
    world.device.schritt()
    world.client.script["certificate"] = [bootstrap.NotAuthenticated("c")]
    world.client.script["register"] = [bootstrap.NotAuthenticated("r")]
    world.link.ereignis("anmeldung_scheitert")
    world.device.schritt()
    world.link.verbinden()
    world.device.schritt()
    world.clock.now += bootstrap.RETRY_MAX_SECONDS
    world.device.schritt()
    assert [call[0] for call in world.client.calls] == ["certificate", "register"]
    assert len(world.links) == 1 and world.link.connected and not world.link.stopped


# --- Host-Rueckrufe ---

class SoftwareFehlerHost(FakeHost):
    def software_soll(self, software):
        raise RuntimeError("geheimer Parameter")


def test_a_failing_software_callback_still_answers_and_hands_over(tmp_path, caplog):
    world = World(tmp_path, SoftwareFehlerHost())
    world.verbinden()
    world.empfangen("down/config", _konfiguration())
    assert world.link.of("up/result") == [{"schema": 1, "dokument": "konfiguration", "version": 1, "ok": True}]
    assert len(world.host.konfigurationen) == 1
    assert "RuntimeError" in caplog.text and "geheimer Parameter" not in caplog.text


class FaehigkeitenFehlerHost(FakeHost):
    def __init__(self) -> None:
        super().__init__()
        self.fehler = 2

    def faehigkeiten(self):
        if self.fehler:
            self.fehler -= 1
            raise RuntimeError("geheimer Parameter")
        return super().faehigkeiten()


def test_a_failing_capabilities_callback_sends_hello_on_a_later_tick(tmp_path, caplog):
    world = World(tmp_path, FaehigkeitenFehlerHost())
    world.verbinden()
    assert world.link.of("up/hello") == [] and world.host.fehler == 0
    world.device.schritt()
    assert world.link.topics()[0] == "up/hello" and len(world.link.of("up/hello")) == 1
    assert "geheimer Parameter" not in caplog.text


class FaehigkeitenAusserhalbHost(FakeHost):
    """Faehigkeiten mit einem Feld ausserhalb des Vertrags (nur, solange `kaputt` gesetzt ist)."""

    def __init__(self) -> None:
        super().__init__()
        self.kaputt = True

    def faehigkeiten(self):
        return {**super().faehigkeiten(), "GEHEIMES-FELD": "WERT-123"} if self.kaputt else super().faehigkeiten()


def test_capabilities_outside_the_contract_block_hello_until_they_are_valid(tmp_path, caplog):
    world = World(tmp_path, FaehigkeitenAusserhalbHost())
    world.verbinden()
    world.device.schritt()
    world.device.schritt()
    assert world.link.of("up/hello") == [] and world.link.topics() == []
    assert "hello nicht gebaut (Vertragsfehler" in caplog.text and "WERT-123" not in caplog.text
    assert caplog.text.count("hello nicht gebaut") == 1  # je Fehlerart und Ort einmal, nicht jeden Takt
    world.host.kaputt = False
    world.device.schritt()  # der Geraete-Thread lebt: der naechste Takt sendet
    assert world.link.topics()[0] == "up/hello" and len(world.link.of("up/hello")) == 1
    assert set(world.link.of("up/hello")[0]) >= set(wire.HELLO_FIELDS)


def test_a_status_outside_the_contract_is_not_sent_and_the_next_valid_one_is(tmp_path, caplog):
    world = World(tmp_path)
    world.verbinden()
    anzahl = len(world.link.of("up/status"))
    world.clock.now += 60
    world.host.raum_wert = {"ist": 20.0, "GEHEIM": "WERT-123"}
    world.device.schritt()
    world.device.schritt()
    assert len(world.link.of("up/status")) == anzahl and world.link.topics()[0] == "up/hello"
    assert caplog.text.count("up/status nicht gebaut") == 1 and "WERT-123" not in caplog.text  # je Text einmal
    world.host.raum_wert = {"ist": 20.0, "soll": 22.0, "soll_quelle": "portal", "ts": "t1"}
    world.device.schritt()
    assert len(world.link.of("up/status")) == anzahl + 1


def test_ein_nicht_endlicher_wert_im_status_wird_nicht_gesendet_der_takt_lebt_weiter(tmp_path, caplog):
    """Testoffensive T2, Nr. 17: allow_nan=False des Links ist der letzte Riegel; Device._publish faengt den ValueError
    (Text ohne Werte), sendet nichts und der naechste gueltige Status geht hinaus."""
    world = World(tmp_path)
    world.verbinden()
    anzahl = len(world.link.of("up/status"))
    world.clock.now += 60
    world.host.raum_wert = {"ist": float("nan"), "soll": 22.0, "soll_quelle": "portal", "ts": "t1"}
    world.device.schritt()
    assert len(world.link.of("up/status")) == anzahl and "up/status nicht gesendet" in caplog.text
    for _ in range(5):    # der Takt versucht es jede Sekunde: genau eine Fehlerzeile, keine Flut
        world.clock.now += 1
        world.device.schritt()
    assert caplog.text.count("up/status nicht gesendet") == 1 and len(world.link.of("up/status")) == anzahl
    world.clock.now += 60
    world.host.raum_wert = {"ist": None, "soll": 22.0, "soll_quelle": "portal", "ts": "t2"}
    world.device.schritt()
    assert world.link.of("up/status")[-1]["raum"]["ist"] is None and len(world.link.of("up/status")) == anzahl + 1


def test_an_own_message_outside_the_contract_keys_is_logged_not_raised(tmp_path, caplog):
    world = World(tmp_path)
    world.verbinden()
    world.device._inbox.put(("eigene", "frei_erfunden", "Text"))
    world.device.schritt()
    assert "Vertragsfehler" in caplog.text and world.link.of("up/notifications") == []


# --- Laufzeit-Kanal ---

def test_setpoints_reach_the_runtime_and_snapshots_the_link(tmp_path):
    world = World(tmp_path)
    worker, seen = RegulationWorker(clock=world.clock), []
    worker.register(EV_SETPOINTS, seen.append)
    worker.register(EV_MQTT_CONNECTED, lambda event: None)
    channel = world.device.laufzeit_kanal(worker)
    world.verbinden()
    world.empfangen("down/setpoints", {"seq": "s1"})
    worker.run_pending()
    assert [event.data["payload"] for event in seen] == [{"seq": "s1"}]
    channel.publish_snapshot({"room_target": 21.0})
    assert world.link.of("up/snapshot") == [{"room_target": 21.0}]


def test_runtime_messages_wait_for_hello(tmp_path):
    world = World(tmp_path)
    channel = world.device.laufzeit_kanal(RegulationWorker(clock=world.clock))
    world.device.schritt()
    world.link.connected = True  # verbunden, aber das Ereignis (und damit hello) steht noch aus
    channel.publish_snapshot({"room_target": 21.0})
    channel.publish_telemetry({"a": 1})
    assert world.link.sent == []


# --- Zustand und Thread ---

@pytest.mark.parametrize("kwargs, zustand", [
    ({"gesperrt": True, "update_noetig": True}, "gesperrt"),
    ({"update_noetig": True}, "update_noetig"),
    ({"server_seen": False}, "startet"),
    ({"server_seen": False, "server_down_seconds": 301}, "keine_verbindung"),
    ({"server_down_seconds": 301}, "keine_verbindung"),
    ({"eingerichtet": False, "device_state": "nicht_uebernommen"}, "nicht_uebernommen"),
    ({"eingerichtet": False, "device_state": "uebernommen"}, "wartet_auf_einrichtung"),
    ({"runtime_status": None}, "wartet_auf_einrichtung"),
    ({"runtime_status": {"status": "abgemeldet"}}, "wartet_auf_einrichtung"),
    ({"runtime_status": {"status": "abo_inaktiv"}}, "regelt"),
    ({"runtime_status": {"status": "notbetrieb"}}, "stoerung"),
    ({"runtime_status": {"status": "startet"}}, "startet"),
    ({}, "regelt"),
])
def test_derive_zustand(kwargs, zustand):
    values = {"gesperrt": False, "update_noetig": False, "server_seen": True, "server_down_seconds": None,
              "eingerichtet": True, "device_state": "uebernommen", "runtime_status": {"status": "regelt"}, **kwargs}
    assert device.derive_zustand(**values) == zustand
    assert zustand in wire.ZUSTAENDE


def test_no_connection_for_five_minutes_shows_no_connection(tmp_path):
    world = World(tmp_path)
    world.device.schritt()
    assert world.device.zustand() == "startet"
    world.clock.now += device.SERVER_DOWN_SECONDS + 1
    assert world.device.zustand() == "keine_verbindung"


def test_the_thread_runs_and_stops(tmp_path):
    world = World(tmp_path)
    thread = world.device.start()
    deadline = time.monotonic() + 5
    while not world.links and time.monotonic() < deadline:
        time.sleep(0.01)
    world.device.stop()
    thread.join(timeout=5)
    assert world.links and world.links[0].started and not thread.is_alive()


def test_host_errors_are_logged_without_their_message(tmp_path, caplog):
    world = World(tmp_path)

    def kaputt(alt, neu):
        raise RuntimeError("geheim")

    world.host.konfiguration_uebernommen = kaputt
    world.verbinden()
    with caplog.at_level("DEBUG", logger="smartheat_device"):
        world.empfangen("down/config", _konfiguration())
    assert "RuntimeError" in caplog.text and "test_device_device.py" in caplog.text
    assert "geheim" not in caplog.text and "Traceback" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_nach_dem_wegfall_der_ursache_wird_ein_erneuter_fehler_wieder_geloggt(tmp_path, caplog):
    """Die Drosselung des Logs gilt je Ursache: nach einem erfolgreichen Senden meldet ein neuer Fehler sich wieder."""
    world = World(tmp_path)
    world.verbinden()
    for runde in range(2):
        world.clock.now += 60
        world.host.raum_wert = {"ist": float("nan"), "soll": 22.0, "soll_quelle": "portal", "ts": f"n{runde}"}
        world.device.schritt()
        world.device.schritt()
        world.clock.now += 60
        world.host.raum_wert = {"ist": 20.0, "soll": 22.0, "soll_quelle": "portal", "ts": f"g{runde}"}
        world.device.schritt()
    assert caplog.text.count("up/status nicht gesendet") == 2
