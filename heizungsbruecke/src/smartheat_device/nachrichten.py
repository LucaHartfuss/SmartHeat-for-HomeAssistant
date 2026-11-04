"""Bausteine der Geraetenachrichten: jede Nachricht des
Geraets entsteht hier und hat genau die Felder des Vertrags (wire.py). Eine Abweichung ist ein Programmierfehler:
Vertragsfehler (ein ValueError) mit Nachricht und Feldnamen, nie mit Werten (Regel 6); die Aufrufer im
Geraete-Thread loggen nur die Fehlerart. tools/contract_check.py ruft dieselben Bausteine fuer die Round-Trips mit
dem Server.

Ausser den Feldmengen setzen die Bausteine nur Vertragsregeln durch, die ohne Wert pruefbar sind: host aus HOSTS,
zustand aus ZUSTAENDE, stufe aus STUFEN, die Felder von kreise, raum und faehigkeiten, die Groesse des CSR."""
from collections.abc import Iterable

from smartheat_device import wire


class Vertragsfehler(ValueError):
    """Eine Nachricht verletzt den Vertrag. Der Text nennt Nachricht und Feldnamen, nie Werte; ein Host-Rueckruf, der
    selbst ein ValueError wirft, ist damit nicht zu verwechseln."""


def pruefen(name: str, body: dict, pflicht: Iterable[str], optional: Iterable[str] = ()) -> dict:
    pflicht, erlaubt = set(pflicht), set(pflicht) | set(optional)
    fehlt, fremd = sorted(pflicht - set(body)), sorted(set(body) - erlaubt)
    if fehlt or fremd:
        raise Vertragsfehler(f"{name}: fehlt {fehlt}, fremd {fremd}")
    return body


def _host(name: str, body: dict, feld: str = "host") -> None:
    if body.get(feld) not in wire.HOSTS:
        raise Vertragsfehler(f"{name}: {feld} steht nicht in HOSTS")


def _faehigkeiten(name: str, wert) -> dict:
    """hello.faehigkeiten und register.capabilities: genau drivers und zigbee, dazu optional updater und programm."""
    if not isinstance(wert, dict):
        raise Vertragsfehler(f"{name}: kein Objekt")
    return pruefen(name, wert, wire.CAPABILITY_FIELDS, wire.CAPABILITY_OPTIONAL)


def _kreisnummern(name: str, eintraege, felder: tuple[str, ...]) -> list[dict]:
    """Eine Liste von Eintraegen mit genau `felder`, deren `kreis` ganze Zahlen von KREIS_MIN bis KREIS_MAX sind,
    aufsteigend und einmalig."""
    if not isinstance(eintraege, list):
        raise Vertragsfehler(f"{name}: keine Liste")
    letzte = wire.KREIS_MIN - 1
    for eintrag in eintraege:
        if not isinstance(eintrag, dict):
            raise Vertragsfehler(f"{name}: Eintrag ist kein Objekt")
        pruefen(name, eintrag, felder)
        nummer = eintrag["kreis"]
        if isinstance(nummer, bool) or not isinstance(nummer, int) or not letzte < nummer <= wire.KREIS_MAX:
            raise Vertragsfehler(f"{name}: kreis ist keine aufsteigende ganze Zahl von KREIS_MIN bis KREIS_MAX")
        letzte = nummer
    return eintraege


def hello(*, device_state: str | None = None, **felder) -> dict:
    body = {"schema": wire.SCHEMA, "schemata": list(wire.SCHEMATA), **felder}
    if device_state is not None:
        body["device_state"] = device_state
    pruefen("hello", body, wire.HELLO_FIELDS, wire.HELLO_OPTIONAL)
    _host("hello", body)
    _faehigkeiten("hello.faehigkeiten", body["faehigkeiten"])
    wege = body["wege"]
    if not isinstance(wege, list) or not all(isinstance(weg, str) for weg in wege) or len(set(wege)) != len(wege):
        raise Vertragsfehler("hello.wege: keine Liste verschiedener Texte")
    return body


def status(*, zustand: str, runtime_status, kreise: list[dict], ts: str, ursprungswerte: dict | None = None) -> dict:
    body = {"schema": wire.SCHEMA, "zustand": zustand, "runtime_status": runtime_status, "kreise": kreise, "ts": ts}
    if ursprungswerte:
        body["ursprungswerte"] = ursprungswerte
    pruefen("status", body, wire.STATUS_FIELDS, wire.STATUS_OPTIONAL)
    if zustand not in wire.ZUSTAENDE:
        raise Vertragsfehler("status: zustand steht nicht in ZUSTAENDE")
    for eintrag in _kreisnummern("status.kreise", kreise, wire.STATUS_KREIS_FIELDS):
        raum = eintrag["raum"]
        if raum is not None:
            if not isinstance(raum, dict):
                raise Vertragsfehler("status.kreise.raum: kein Objekt")
            pruefen("status.kreise.raum", raum, wire.RAUM_FIELDS)
    return body


def meldung(*, key: str, kategorie: str, stufe: str, text: str, offen: bool, ts: str) -> dict:
    body = pruefen("meldung", {"key": key, "kategorie": kategorie, "stufe": stufe, "text": text, "offen": offen,
                               "ts": ts}, wire.NOTIFICATION_FIELDS)
    if stufe not in wire.STUFEN:
        raise Vertragsfehler("meldung: stufe steht nicht in STUFEN")
    return body


def notifications(items: list[dict]) -> dict:
    return pruefen("notifications", {"schema": wire.SCHEMA, "items": items}, wire.NOTIFICATIONS_FIELDS)


# up/result: Pflicht je Ziel steht in wire.RESULT_PFLICHT_DOKUMENT (Dokument, Software) und RESULT_FIELDS (Befehl).
# Das Ziel ist command_id (Befehl) oder dokument und version (Dokument); beide Ziele stehen in RESULT_TARGETS.
ZIEL_BEFEHL, *ZIELE_DOKUMENT = wire.RESULT_TARGETS


def ergebnis_dokument(dokument: str, version, ok: bool, error: dict | None = None) -> dict:
    body = {"schema": wire.SCHEMA, "dokument": dokument, "version": version, "ok": ok}
    if error is not None:
        body["error"] = error
    return pruefen("ergebnis", body, (*wire.RESULT_PFLICHT_DOKUMENT, *ZIELE_DOKUMENT), wire.RESULT_OPTIONAL_DOKUMENT)


def _csr(name: str, csr) -> None:
    if not isinstance(csr, str) or len(csr.encode()) > wire.CSR_MAX_BYTES:
        raise Vertragsfehler(f"{name}: csr ist kein Text oder zu gross")


def register(*, werks_nachweis=None, **felder) -> dict:
    body = dict(felder)
    if werks_nachweis is not None:
        body["werks_nachweis"] = werks_nachweis
    pruefen("register", body, wire.REGISTER_FIELDS, wire.REGISTER_OPTIONAL)
    _host("register", body)
    _faehigkeiten("register.capabilities", body["capabilities"])
    _csr("register", body["csr"])
    return body


def zertifikat(csr: str) -> dict:
    body = pruefen("zertifikat", {"csr": csr}, wire.CERTIFICATE_FIELDS)
    _csr("zertifikat", csr)
    return body
