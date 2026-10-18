"""Testoffensive T2 (Spec §4, §5, §12 Nr. 20): Property- und Angriffstests der Leser von Geraeteeingaben im Add-on
(V1 und V1b): Bootstrap-Antwort, Dokumente, Befehle und die Serverantwort `handle_setpoints`. Spec §5 "kompromittierter
Server": ein Server, der beliebige Bytes sendet, bringt weder den Geraete-Thread noch den Regel-Worker zum Stehen und
bringt keinen Wert ausserhalb der lokalen Grenzen (Regel 4) auf die Anlage. Eigene Kopie der JSON-Strategie (die Repos
teilen keine Testhilfen). `deadline=None`: ein Beispiel der Laufzeit-Tests startet eine ganze Laufzeit; die Zahl der
Beispiele ist begrenzt (50, fester Seed, keine .hypothesis-Datenbank im Checkout)."""
import json
import logging
import math
import os
import tempfile
from pathlib import Path

import pytest
from conftest import FakeClock
from device_helpers import GeraeteCa, zertifikat_fuer
from fakes import FakeHa
from hypothesis import given, settings
from hypothesis import strategies as st
from test_device_bootstrap import FakeResponse, FakeSession
from test_device_commands import Sent, _command
from test_device_documents import _bedienung, _konfiguration, _leer, _store
from test_runtime import OPTIONS, VIESSMANN_OPTIONEN, VIESSMANN_ZUSTAND, _ha_verbunden, _mqtt, _start
from test_runtime_plan3b import WEISHAUPT, WEISHAUPT_STATES

from heizungsbruecke.derived_sensors import DerivedSensors
from smartheat_core.safety import LOCAL_SAFETY
from smartheat_device import bootstrap, wire
from smartheat_device import commands as cmd
from smartheat_device import identity as identity_module
from smartheat_device.tls import ALPN_443, IOT_PORTS
from smartheat_runtime import entitlement

EINSTELLUNG = settings(max_examples=50, derandomize=True, database=None, deadline=None)
JSON_WERT = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True, allow_infinity=True)
    | st.text(max_size=300) | st.just(10**400),
    lambda kinder: st.lists(kinder, max_size=5) | st.dictionaries(st.text(max_size=20), kinder, max_size=5),
    max_leaves=30,
)
#: json.loads liefert aus "\ud800" ein einzelnes Surrogat; schreiben_json kodiert als UTF-8 (ensure_ascii=False)
SURROGATTEXT = st.text(alphabet=st.sampled_from(["a", "é", "\ud800", "\udfff"]), min_size=1, max_size=8)


@pytest.fixture(autouse=True)
def _ohne_fsync(monkeypatch):
    """Durchsatz: je Beispiel entstehen ein Speicher und etliche Dateien; die Absturzsicherheit des atomaren Schreibens
    (fsync) pruefen test_core_dateien.py und die Geraete-Tests, nicht diese Datei."""
    monkeypatch.setattr(os, "fsync", lambda fd: None)


def _pfade(wert, vorne=()):
    """Alle Pfade (Schluessel/Indizes) in einer Vorlage, auch die inneren Ebenen."""
    yield vorne
    if isinstance(wert, dict):
        for schluessel, kind in wert.items():
            yield from _pfade(kind, (*vorne, schluessel))
    elif isinstance(wert, list):
        for index, kind in enumerate(wert):
            yield from _pfade(kind, (*vorne, index))


_WEG = object()  # an der Stelle entfernen statt ersetzen


def _ersetze(wert, pfad, neu):
    if not pfad:
        return neu
    kopie = json.loads(json.dumps(wert))
    ziel = kopie
    for schluessel in pfad[:-1]:
        ziel = ziel[schluessel]
    if neu is _WEG:
        del ziel[pfad[-1]]
    else:
        ziel[pfad[-1]] = neu
    return kopie


def _nur_endliche_zahlen(wert) -> bool:
    if isinstance(wert, dict):
        return all(_nur_endliche_zahlen(kind) for kind in wert.values())
    if isinstance(wert, list | tuple):
        return all(_nur_endliche_zahlen(kind) for kind in wert)
    if isinstance(wert, float):
        return math.isfinite(wert)
    return not isinstance(wert, int) or isinstance(wert, bool) or abs(wert) < 10**300


# --- Bootstrap-Antwort (V1, bootstrap._antwort) ---

BASE = "https://accounts.example.test"
NOW = 1_800_000_000
ERLAUBTE_FEHLER = (bootstrap.Unavailable, bootstrap.Rejected, bootstrap.NotAuthenticated)
STATUS = [200, 201, 204, 301, 400, 401, 403, 404, 408, 429, 500, 503]


@pytest.fixture(scope="module")
def ident(tmp_path_factory):
    return identity_module.load_or_create(tmp_path_factory.mktemp("property") / "device")


@pytest.fixture(scope="module")
def gute_antwort(ident):
    ca = GeraeteCa()
    return {"device_state": "nicht_uebernommen", "poll_after": 60, "certificate": zertifikat_fuer(ident, ca),
            "mqtt_endpoint": "mqtt.e2e.test", "mqtt_port": 8883, "mqtt_ca": ca.pem, "mqtt_alpn": ALPN_443}


def _client(ident, *antworten):
    return bootstrap.BootstrapClient(BASE, ident, session=FakeSession(*antworten), now=lambda: NOW)


def _register_args():
    return {"version": "0.6.0", "host": "gateway", "capabilities": {"drivers": ["simulation"], "zigbee": True}}


def _ablauf(verzeichnis, ident, client):
    return bootstrap.Bootstrap(Path(verzeichnis), lambda: client, register_args=_register_args,
                               uhr_synchron=lambda: True, identity=lambda: ident, clock=lambda: 0.0)


@EINSTELLUNG
@given(JSON_WERT, st.sampled_from(STATUS))
def test_beliebige_bootstrap_antwort_ergibt_nur_die_fehlerarten_des_bootstraps(ident, body, status):
    """Beliebiges JSON (Objekt und Nicht-Objekt, jeder Status) an register: nur Unavailable/Rejected/NotAuthenticated
    (bei 200 immer Unavailable: kein zufaelliges Objekt hat alle Pflichtfelder), nie eine andere Ausnahme."""
    try:
        _client(ident, FakeResponse(status, body)).register(**_register_args())
    except ERLAUBTE_FEHLER as fehler:
        if status == 200:
            assert isinstance(fehler, bootstrap.Unavailable)
    else:
        pytest.fail("eine zufaellige Antwort darf keinen Zugang ergeben")


@EINSTELLUNG
@given(JSON_WERT, st.sampled_from(STATUS))
def test_eine_beliebige_bootstrap_antwort_wird_nie_gespeichert(ident, body, status):
    with tempfile.TemporaryDirectory() as verzeichnis:
        ablauf = _ablauf(verzeichnis, ident, _client(ident, FakeResponse(status, body)))
        assert ablauf.holen(rettung=False) is None
        assert list(Path(verzeichnis).iterdir()) == []


def _bootstrap_stellen(gut):
    return [pfad for pfad in _pfade(gut) if pfad]


@EINSTELLUNG
@given(st.integers(0, 6), JSON_WERT)
def test_beliebiger_wert_an_einer_stelle_der_bootstrap_antwort_wird_abgelehnt_oder_gelesen(ident, gute_antwort, nr, wert):
    stellen = _bootstrap_stellen(gute_antwort)
    pfad = stellen[nr % len(stellen)]
    try:
        antwort = _client(ident, FakeResponse(200, _ersetze(gute_antwort, pfad, wert))).register(**_register_args())
    except bootstrap.Unavailable:
        return
    _zugang_ist_vertragsgemaess(antwort)


@EINSTELLUNG
@given(st.integers(0, 6), JSON_WERT)
def test_bootstrap_speichert_genau_dann_wenn_es_einen_zugang_gibt(ident, gute_antwort, nr, wert):
    stellen = _bootstrap_stellen(gute_antwort)
    pfad = stellen[nr % len(stellen)]
    with tempfile.TemporaryDirectory() as verzeichnis:
        client = _client(ident, FakeResponse(200, _ersetze(gute_antwort, pfad, wert)))
        zugang = _ablauf(verzeichnis, ident, client).holen(rettung=False)
        gespeichert = sorted(datei.name for datei in Path(verzeichnis).iterdir())
        if zugang is None:
            assert gespeichert == []
        else:
            assert bootstrap.CERTIFICATE_FILE in gespeichert and bootstrap.ENDPOINT_FILE in gespeichert
            assert bootstrap.laden(Path(verzeichnis)) == zugang


SONDERWERTE = [None, True, False, 0, 1, -1, 443, 8883, 65536, 1.5, float("nan"), float("inf"), "", "x", "8883", " ",
               "-----BEGIN CERTIFICATE-----", ALPN_443, "alpn", [], {}, [8883], {"a": 1}, 10**400, -(10**400)]


def _zugang_ist_vertragsgemaess(antwort):
    endpunkt = antwort.zugang.endpoint
    assert isinstance(endpunkt.host, str) and endpunkt.host
    assert type(endpunkt.port) is int and endpunkt.port in IOT_PORTS
    assert bootstrap._PEM_CERT in endpunkt.ca_pem and bootstrap._PEM_CERT in antwort.zugang.certificate_pem
    assert endpunkt.alpn in (None, ALPN_443)
    assert antwort.device_state is None or antwort.device_state in wire.DEVICE_STATES


def test_jeder_sonderwert_an_jeder_stelle_der_bootstrap_antwort_wird_abgelehnt_oder_vertragsgemaess_gelesen(
        ident, gute_antwort):
    """Erschoepfend statt gezogen (die Zufallsziehung trifft einen Port oder ein Feld selten): jede Stelle der
    Antwort x jeder Sonderwert, fehlend eingeschlossen; Gegenprobe: mindestens ein Fall wird gelesen und einer abgelehnt."""
    gelesen = abgelehnt = 0
    for pfad in _bootstrap_stellen(gute_antwort):
        for wert in [*SONDERWERTE, _WEG]:
            try:
                antwort = _client(ident, FakeResponse(200, _ersetze(gute_antwort, pfad, wert))).register(
                    **_register_args())
            except bootstrap.Unavailable:
                abgelehnt += 1
                continue
            _zugang_ist_vertragsgemaess(antwort)
            gelesen += 1
    assert gelesen > 0 and abgelehnt > 0


def test_die_gute_bootstrap_antwort_wird_gelesen_und_gespeichert(ident, gute_antwort):
    """Gegenprobe zu den Property-Tests: die unveraenderte Vorlage ergibt einen Zugang (JSON-Rundweg inklusive)."""
    antwort = json.loads(json.dumps(gute_antwort))
    with tempfile.TemporaryDirectory() as verzeichnis:
        zugang = _ablauf(verzeichnis, ident, _client(ident, FakeResponse(200, antwort))).holen(rettung=False)
        assert zugang is not None and zugang.endpoint.port == 8883 and zugang.endpoint.alpn == ALPN_443
        assert bootstrap.laden(Path(verzeichnis)) == zugang


# --- Dokumente (V1, DocumentStore.empfangen) ---

KONFIG_OPTIONAL = ("ursprungswerte",)  # Namen der Spec, nicht wire.KONFIGURATION_OPTIONAL
DOKUMENTE = [
    ("konfiguration", "konfiguration", _konfiguration(), KONFIG_OPTIONAL),
    ("konfiguration_mit_ursprung", "konfiguration", _konfiguration(ursprungswerte={"heat_limit": 18.0}), KONFIG_OPTIONAL),
    ("konfiguration_leer", "konfiguration", _leer(), KONFIG_OPTIONAL),
    ("bedienung", "bedienung", _bedienung(), ()),
]
STELLEN = [(dokument, pfad) for dokument in DOKUMENTE for pfad in _pfade(dokument[2]) if pfad]


def _ergebnis_ist_wohlgeformt(ergebnis):
    """Antwort an den Server: Grund aus dem Vertrag, Nutzlast ohne NaN/Inf als JSON baubar."""
    if ergebnis.ok:
        assert ergebnis.grund is None
    else:
        assert ergebnis.grund in wire.DOKUMENT_ERRORS
    json.loads(json.dumps(ergebnis.payload(), allow_nan=False))


@EINSTELLUNG
@given(st.sampled_from(wire.DOKUMENTE), st.dictionaries(st.text(max_size=30), JSON_WERT, max_size=12),
       st.just(1) | JSON_WERT, st.just(1) | JSON_WERT)
def test_beliebiges_dokument_wird_abgelehnt_oder_ignoriert_nie_eine_andere_ausnahme(art, rest, schema, version):
    with tempfile.TemporaryDirectory() as verzeichnis:
        store = _store(Path(verzeichnis))
        ergebnis = store.empfangen(art, {**rest, "schema": schema, "version": version})
        if ergebnis is None:
            assert store.version(art) == 0
            return
        assert not ergebnis.ok  # ein zufaelliges Objekt ist nie ein vollstaendiges Dokument
        _ergebnis_ist_wohlgeformt(ergebnis)
        assert store.inhalt(art) is None and not (Path(verzeichnis) / "sicherheitssatz.json").exists()


@EINSTELLUNG
@given(st.sampled_from(DOKUMENTE), st.text(max_size=20), JSON_WERT)
def test_fremdes_feld_in_einem_gueltigen_dokument_ist_ungueltig(dokument, feld, wert):
    _name, art, gut, optional = dokument
    if feld in gut or feld in optional:
        return
    with tempfile.TemporaryDirectory() as verzeichnis:
        store = _store(Path(verzeichnis))
        ergebnis = store.empfangen(art, {**gut, feld: wert})
        assert (ergebnis.ok, ergebnis.grund) == (False, "ungueltig")
        assert store.inhalt(art) is None and store.version(art) == 0


@pytest.mark.parametrize("dokument", DOKUMENTE, ids=[dokument[0] for dokument in DOKUMENTE])
def test_gueltiges_dokument_ueberlebt_den_json_rundweg(tmp_path, dokument):
    _name, art, gut, _optional = dokument
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    direkt = _store(tmp_path / "a").empfangen(art, gut)
    ueber_json = _store(tmp_path / "b").empfangen(art, json.loads(json.dumps(gut)))
    assert direkt == ueber_json and direkt.ok and direkt.uebernommen  # Gegenprobe: die Vorlage selbst ist gueltig


@EINSTELLUNG
@given(st.sampled_from(STELLEN), JSON_WERT)
def test_beliebiger_wert_an_einer_stelle_lehnt_ab_und_behaelt_das_vorige_dokument(stelle, wert):
    """Wert an jeder Stelle (auch innere Ebenen): nie eine andere Ausnahme; abgelehnt bleibt das vorige Dokument,
    angenommen ist das Dokument nach einem Neustart dasselbe."""
    (_name, art, gut, _optional), pfad = stelle
    with tempfile.TemporaryDirectory() as verzeichnis:
        store = _store(Path(verzeichnis))
        if art == "bedienung":
            assert store.empfangen(art, gut).uebernommen
        else:
            assert store.empfangen(art, {**gut, "version": 1}).uebernommen
        vorher = (store.version(art), store.inhalt(art))
        neu = _ersetze({**gut, "version": 2}, pfad, wert)
        ergebnis = store.empfangen(art, neu)
        if ergebnis is None:
            assert (store.version(art), store.inhalt(art)) == vorher
            return
        _ergebnis_ist_wohlgeformt(ergebnis)
        if not ergebnis.ok:
            assert (store.version(art), store.inhalt(art)) == vorher
        elif ergebnis.uebernommen:
            assert _store(Path(verzeichnis)).inhalt(art) == store.inhalt(art)


@EINSTELLUNG
@given(st.sampled_from(STELLEN))
def test_fehlendes_feld_an_einer_stelle_lehnt_ab_oder_liest(stelle):
    (_name, art, gut, _optional), pfad = stelle
    with tempfile.TemporaryDirectory() as verzeichnis:
        store = _store(Path(verzeichnis))
        ergebnis = store.empfangen(art, _ersetze(gut, pfad, _WEG))
        if ergebnis is not None:
            _ergebnis_ist_wohlgeformt(ergebnis)
        if len(pfad) == 1 and pfad[0] not in KONFIG_OPTIONAL and pfad[0] != "version":
            assert ergebnis is None or not ergebnis.ok  # ein fehlendes Pflichtfeld der obersten Ebene wird nie angenommen


_NICHT_ENDLICH = [float("nan"), float("inf"), float("-inf"), 10**400, -(10**400)]
_FORMEN = [lambda zahl: zahl, lambda zahl: [zahl], lambda zahl: {"x": zahl}, lambda zahl: {"x": [zahl]}]


#: Felder, die der Leser nicht auf Typ und Zahl prueft (Befund Task 13, niedrig, Restpunkt T2-Feldtypen): `mindest_software`
#: und `ts` liest im Geraet keine Stelle als Zahl; `ursprungswerte` liest nur die Pipeline und filtert dort jede nicht
#: endliche Zahl (`_is_finite_number`, Rueckweg auf den lokalen Wert) und klemmt auf die lokalen Grenzen. Eine leere Konfiguration ist "immer gueltig" (nur `anlage` None zaehlt):
#: sie fehlt in dieser Aufzaehlung ganz.
UNGEPRUEFT = {"konfiguration": ("mindest_software", "ursprungswerte"),
              "konfiguration_mit_ursprung": ("mindest_software", "ursprungswerte"), "bedienung": ("ts",)}


@pytest.mark.parametrize("dokument", [d for d in DOKUMENTE if d[0] in UNGEPRUEFT], ids=list(UNGEPRUEFT))
def test_nicht_endliche_zahl_an_jeder_stelle_wird_abgelehnt_oder_nie_gespeichert(tmp_path, dokument):
    """Erschoepfend: jede Stelle x jede nicht endliche Zahl x jede Form. Angenommen nur, wenn die Zahl nicht im
    gespeicherten Dokument landet (Feld ohne Wirkung). Ausgenommen: die Versionen (Zaehler: eine riesige Ganzzahl ist dort
    eine gueltige Ganzzahl, _is_version liest sie nur als Zaehler), die Treiberparameter (`bindung.treiber.parameter`: sie
    prueft der Treiber ueber den Rueckruf des Hosts, den dieser Store hier nicht hat; Gateway-Host-Tests) und UNGEPRUEFT."""
    name, art, gut, _optional = dokument
    zaehler = 0
    for pfad in _pfade(gut):
        if not pfad or pfad[0] in ("version", "schema", *UNGEPRUEFT[name]) or pfad[:3] == ("bindung", "treiber", "parameter"):
            continue
        for zahl in _NICHT_ENDLICH:
            if isinstance(zahl, int) and pfad[:2] == ("bindungsparameter", "schreibschutz"):
                continue  # Tageszaehler: eine riesige Ganzzahl ist dort ein gueltiges Budget (lokal ohne Grenze = ohne Budget)
            for form in _FORMEN:
                zaehler += 1
                verzeichnis = tmp_path / f"d{zaehler}"
                verzeichnis.mkdir()
                store = _store(verzeichnis)
                ergebnis = store.empfangen(art, _ersetze(gut, pfad, form(zahl)))
                if ergebnis is not None and ergebnis.ok:
                    assert _nur_endliche_zahlen(store.inhalt(art)), (pfad, zahl)
    assert zaehler > 0


@EINSTELLUNG
@given(st.sampled_from(STELLEN), SURROGATTEXT)
def test_text_mit_einzelnem_surrogat_wird_abgelehnt_oder_ist_speicherbar(stelle, text):
    """json.loads liefert aus "\\ud800" ein einzelnes Surrogat, das sich nicht als UTF-8 speichern laesst: ein Dokument
    mit einem solchen Text wird abgelehnt oder gespeichert und wieder gelesen; die Annahme wirft nicht."""
    (_name, art, gut, _optional), pfad = stelle
    with tempfile.TemporaryDirectory() as verzeichnis:
        store = _store(Path(verzeichnis))
        ergebnis = store.empfangen(art, _ersetze(gut, pfad, text))
        if ergebnis is not None and ergebnis.uebernommen:
            assert _store(Path(verzeichnis)).inhalt(art) == store.inhalt(art)


# --- Befehle (V1, CommandRegister.empfangen) ---

COMMAND_FELDER = ("schema", "command_id", "kind", "payload", "expires_at")  # Namen der Spec, nicht der Konstante
BEFEHLE = [
    ("diagnostics", {}), ("hinweis", {"key": "k", "stufe": "info", "text": "t"}), ("sign_off", {}),
    ("zigbee_permit_join", {"seconds": 30}), ("claim_state", {"device_state": "uebernommen"}),
    ("inventory", {"stunden": 1}), ("inventory", {"stunden": 1, "filter": {}}),
]


def _register(verzeichnis, sent, aufrufe):
    register = cmd.CommandRegister(Path(verzeichnis), sent)
    for kind in wire.COMMANDS:
        register.register(kind, lambda payload, kind=kind: aufrufe.append((kind, payload)) or cmd.Done({}))
    return register


def _soll_laufen(nachricht) -> bool:
    """Unabhaengige Aussage der Annahmeregeln (Befehle: genau die Felder des Vertrags, die Nutzlast ein Objekt mit allen
    Pflichtfeldern der Art und sonst nur deren optionalen Feldern); der Wert von `schema` prueft die Annahme nicht."""
    command_id, kind, payload = nachricht.get("command_id"), nachricht.get("kind"), nachricht.get("payload")
    if not (isinstance(command_id, str) and 0 < len(command_id) <= 64) or set(nachricht) != set(COMMAND_FELDER):
        return False
    if not isinstance(kind, str) or kind not in wire.COMMANDS or not isinstance(payload, dict):
        return False
    art = wire.COMMANDS[kind]
    erlaubt = {*art.payload, *art.payload_optional}
    if not set(payload) <= erlaubt:
        return False
    return kind == "driver_login" or all(feld in payload or feld in art.payload_optional for feld in art.payload)


def _ergebnisse_sind_wohlgeformt(sent):
    for ergebnis in sent.results:
        assert set(ergebnis) == {"schema", "command_id", "ok", "result", "error"}
        json.dumps(ergebnis, allow_nan=False)
        if not ergebnis["ok"]:
            assert ergebnis["error"]["grund"] in {g for kind in wire.COMMANDS for g in wire.command_errors(kind)}


@EINSTELLUNG
@given(st.dictionaries(st.sampled_from([*COMMAND_FELDER, "fremd"]), JSON_WERT, max_size=6))
def test_beliebiger_befehl_fuehrt_nur_bei_gueltiger_form_einen_handler_aus(nachricht):
    with tempfile.TemporaryDirectory() as verzeichnis:
        sent, aufrufe = Sent(), []
        _register(verzeichnis, sent, aufrufe).empfangen(nachricht)
        assert len(aufrufe) == int(_soll_laufen(nachricht))
        assert len(sent.results) == int(isinstance(nachricht.get("command_id"), str)
                                        and 0 < len(nachricht["command_id"]) <= 64)
        _ergebnisse_sind_wohlgeformt(sent)


@EINSTELLUNG
@given(st.sampled_from(BEFEHLE), st.dictionaries(st.text(max_size=12), JSON_WERT, max_size=4), st.booleans())
def test_befehl_mit_fremdem_feld_fuehrt_keinen_handler_aus(befehl, fremd, im_payload):
    kind, payload = befehl
    nachricht = _command(kind, payload=payload)
    feld = next((name for name in fremd if name not in (*COMMAND_FELDER, *payload, "filter", "seconds")), None)
    if feld is None:
        return
    if im_payload:
        nachricht["payload"] = {**payload, feld: fremd[feld]}
    else:
        nachricht[feld] = fremd[feld]
    with tempfile.TemporaryDirectory() as verzeichnis:
        sent, aufrufe = Sent(), []
        _register(verzeichnis, sent, aufrufe).empfangen(nachricht)
        assert aufrufe == []
        assert [(r["ok"], r["error"]["grund"]) for r in sent.results] == [(False, "ungueltige_nutzlast")]
        _ergebnisse_sind_wohlgeformt(sent)


@pytest.mark.parametrize(("kind", "payload"), BEFEHLE, ids=[f"{kind}-{i}" for i, (kind, _) in enumerate(BEFEHLE)])
def test_gueltiger_befehl_ueberlebt_den_json_rundweg(tmp_path, kind, payload):
    ergebnisse = []
    for nr, nachricht in enumerate((_command(kind, payload=payload), json.loads(json.dumps(_command(kind, payload=payload))))):
        (tmp_path / str(nr)).mkdir()
        sent, aufrufe = Sent(), []
        _register(tmp_path / str(nr), sent, aufrufe).empfangen(nachricht)
        assert aufrufe == [(kind, payload)]  # Gegenprobe: der Befehl ist gueltig und laeuft
        ergebnisse.append(sent.results)
    assert ergebnisse[0] == ergebnisse[1] and ergebnisse[0][0]["ok"] is True


@EINSTELLUNG
@given(st.sampled_from(BEFEHLE), SURROGATTEXT | st.text(max_size=100) | st.just("x" * 65))
def test_command_id_beliebigen_textes_wird_abgelehnt_oder_ausgefuehrt_ohne_ausnahme(befehl, command_id):
    kind, payload = befehl
    with tempfile.TemporaryDirectory() as verzeichnis:
        sent, aufrufe = Sent(), []
        _register(verzeichnis, sent, aufrufe).empfangen(_command(kind, command_id=command_id, payload=payload))
        gueltig = 0 < len(command_id) <= 64 and not any("\ud800" <= zeichen <= "\udfff" for zeichen in command_id)
        assert len(aufrufe) == int(gueltig) and len(sent.results) == int(gueltig)
        _ergebnisse_sind_wohlgeformt(sent)


@EINSTELLUNG
@given(JSON_WERT, JSON_WERT, JSON_WERT | st.just(_WEG), st.sampled_from(["key", "stufe", "text"]))
def test_hinweis_mit_beliebigen_werten_speichert_nur_gueltige_hinweise(schluessel, stufe, text, feld):
    """Der eingebaute Befehl hinweis (jedes Geraet): was ankommt, ist ein Hinweis aus dem Vertrag oder abgelehnt."""
    with tempfile.TemporaryDirectory() as verzeichnis:
        sent = Sent()
        register = cmd.CommandRegister(Path(verzeichnis), sent)
        hinweise = cmd.Hinweise(Path(verzeichnis))
        register.register("hinweis", hinweise.handler)
        payload = {"key": schluessel, "stufe": stufe, **({} if text is _WEG else {"text": text})}
        payload[feld] = {"key": schluessel, "stufe": stufe, "text": None if text is _WEG else text}[feld]
        register.empfangen(_command("hinweis", payload=payload))
        _ergebnisse_sind_wohlgeformt(sent)
        for eintrag in hinweise.alle():
            assert isinstance(eintrag["key"], str) and 0 < len(eintrag["key"]) <= 64
            assert eintrag["stufe"] in wire.HINWEIS_STUFEN
            assert isinstance(eintrag["text"], str) and 0 < len(eintrag["text"]) <= 1000
        assert cmd.Hinweise(Path(verzeichnis)).alle() == hinweise.alle()  # was angenommen ist, ist auch gespeichert


# --- Serverantwort down/setpoints (V1b, ticks.handle_setpoints) ---

#: Entity -> Hebel (Bereich in LocalSafety.ranges) je Hebelsatz der Laufzeit-Tests; Werte der Auswahl- und Textentities
#: sind keine Zahlen und stehen nicht in der Tabelle
SAETZE = {
    "vaillant_vrc720": (
        {}, {}, {"number.curve_current": "curve", "number.shift_current": "room_setpoint",
                 "number.heat_limit": "heat_limit", "number.min_flow": "min_flow"}),
    "viessmann_vicare": (
        VIESSMANN_OPTIONEN, VIESSMANN_ZUSTAND, {"number.slope": "curve", "number.shift": "level",
                                                "number.normal_temperature": "room_setpoint"}),
    "weishaupt_wwp": (
        WEISHAUPT, WEISHAUPT_STATES, {"number.hk": "curve", "number.normal": "room_setpoint",
                                      "number.komfort": "room_setpoint", "number.swu": "heat_limit"}),
}
ALLE_HEBEL = ["curve", "room_setpoint", "level", "heat_limit", "min_flow"]


def _frische_umgebung(verzeichnis: Path, mp):
    """Wie die Fixture `env` in test_runtime.py, aber ohne Fixture (hypothesis: ein Beispiel = eine Umgebung)."""
    paths = {}
    for name in ("BACKUP_PATH", "FAILSAFE_PATH", "ENTITLEMENT_PATH", "DERIVED_SENSORS_PATH", "AUFRAEUMEN_PATH",
                 "RASTUNG_PATH"):
        paths[name] = verzeichnis / f"{name.lower()}.json"
        mp.setattr(f"heizungsbruecke.config.{name}", paths[name])
    laufzeit = verzeichnis / "runtime"
    for name, wert in (("RUNTIME_DIR", laufzeit), ("ROOM_TARGET_PATH", laufzeit / "room_target.json"),
                       ("ABGEMELDET_PATH", laufzeit / "abgemeldet.json")):
        paths[name] = wert
        mp.setattr(f"heizungsbruecke.config.{name}", wert)
    mp.setattr("heizungsbruecke.derived_sensors.ensure_all",
               lambda **kwargs: DerivedSensors({"room_actual": "sensor.room_actual"}, (), "fp1"))
    trigger = []

    class _Trigger:
        def __init__(self, **kwargs):
            self.kwargs, self.connected = kwargs, True
            trigger.append(self)

        def start(self):
            pass

        def stop(self):
            pass

    mp.setattr("heizungsbruecke.geraet_trigger.HaTriggerClient", _Trigger)
    from types import SimpleNamespace
    return SimpleNamespace(clock=FakeClock(), paths=paths, abo={"status": entitlement.ACTIVE, "queries": 0},
                           ha=FakeHa(), trigger_clients=trigger, derived={"replaced": ()}, geraet=None)


_ZAHL = st.floats(allow_nan=False, allow_infinity=False) | st.integers(-10**30, 10**30)
_HEBELWERT = st.one_of(
    _ZAHL, st.floats(-60, 60), st.floats(allow_nan=True, allow_infinity=True), st.integers(-5, 30),
    st.just(10**400), st.just(-(10**400)), st.booleans(), st.none(), st.text(max_size=5),
    st.lists(st.floats(), max_size=2))


def _hebel(erwartet):
    return st.one_of(
        st.fixed_dictionaries({name: _ZAHL for name in erwartet}),  # richtiger Aufbau, wilde endliche Zahlen
        st.fixed_dictionaries({name: _HEBELWERT for name in erwartet}),  # richtiger Aufbau, beliebiger Wert
        st.dictionaries(st.sampled_from(ALLE_HEBEL), _HEBELWERT, max_size=5),  # fehlende und fremde Hebel
        JSON_WERT)


def _antwort(erwartet):
    return st.builds(
        lambda extra, schema, status, reason, levers, learned: {
            **extra, "schema": schema, "ts": "x", "status": status, "reason": reason, "levers": levers,
            "learned": learned},
        st.dictionaries(st.text(max_size=8), JSON_WERT, max_size=3),
        st.sampled_from([4, 4, 4, 4]) | JSON_WERT,
        st.sampled_from(["ok", "ok", "ok", "skipped", "rejected", "maybe"]) | JSON_WERT,
        st.none() | st.text(max_size=20) | SURROGATTEXT | JSON_WERT,
        _hebel(erwartet),
        st.none() | st.fixed_dictionaries({"curve": _HEBELWERT, "heat_limit": _HEBELWERT}) | JSON_WERT,
    )


def _endlich(wert) -> bool:
    if isinstance(wert, bool) or not isinstance(wert, int | float):
        return False
    try:
        return math.isfinite(float(wert))
    except OverflowError:
        return False  # riesige Ganzzahl


class _Fehlerlog(logging.Handler):
    """Fehler des Regel-Workers (der Worker faengt Ausnahmen ab und loggt sie; ein Fehler dort ist ein Fund)."""

    def __init__(self):
        super().__init__(logging.ERROR)
        self.eintraege = []

    def emit(self, record):
        self.eintraege.append(record.getMessage())


@pytest.mark.parametrize("hebelsatz", sorted(SAETZE))
def test_beliebige_serverantwort_schreibt_nichts_ausserhalb_der_lokalen_grenzen_und_laesst_den_worker_laufen(hebelsatz):
    optionen, zustand, entities = SAETZE[hebelsatz]
    erwartet = tuple(sorted(set(entities.values()) - {"min_flow"}))
    bereiche = LOCAL_SAFETY[(hebelsatz, OPTIONS["verteilsystem"])].ranges
    zaehler = {"geschrieben": 0, "angenommen": 0}

    @EINSTELLUNG
    @given(_antwort(erwartet))
    def pruefen(antwort):
        with tempfile.TemporaryDirectory() as verzeichnis, pytest.MonkeyPatch.context() as mp:
            env = _frische_umgebung(Path(verzeichnis), mp)
            env.ha.states.update(zustand)
            bridge = _start(env, **optionen)
            _ha_verbunden(env, bridge)
            snapshot = _mqtt(env).snapshots[-1]
            erwartete_hebel = tuple(sorted(snapshot["levers"]))
            vorher = list(env.ha.writes)
            log = _Fehlerlog()
            worker_log = logging.getLogger("smartheat_runtime.worker")
            worker_log.addHandler(log)
            try:
                env.geraet.channel.setpoints(json.loads(json.dumps({"seq": snapshot["seq"], **antwort})))
                bridge.worker.run_pending()
            finally:
                worker_log.removeHandler(log)
            assert log.eintraege == []  # kein Fehler im Worker
            neu = env.ha.writes[len(vorher):]
            levers = antwort["levers"]
            gueltig = (antwort["schema"] == 4 and type(antwort["schema"]) is int and antwort["status"] in ("ok", "skipped")
                       and isinstance(levers, dict) and sorted(levers) == list(erwartete_hebel)
                       and all(_endlich(wert) for wert in levers.values()))
            if not gueltig:
                assert neu == []  # ungueltige Antwort: nichts geschrieben
            zaehler["angenommen"] += int(gueltig)
            for entity, wert in neu:
                hebel = entities.get(entity)
                if hebel is None:
                    continue  # Betriebsart und Hilfswerte (Text), keine Hebel des Servers
                zaehler["geschrieben"] += 1
                assert bereiche[hebel][0] <= wert <= bereiche[hebel][1], (entity, wert)
            if bridge.store.state.learned:
                assert set(bridge.store.state.learned) <= {"curve", "heat_limit"}
                assert all(_endlich(wert) for wert in bridge.store.state.learned.values())

    pruefen()
    assert zaehler["angenommen"] > 0 and zaehler["geschrieben"] > 0  # Gegenprobe: gueltige Antworten wurden geschrieben
