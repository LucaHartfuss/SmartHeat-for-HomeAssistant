"""Bausteine der Geraetenachrichten (Audit 5 A5-34; Spec Stufe 3 S8 Abschnitt 6; Plan S8a-1 P1): jede Nachricht des
Geraets entsteht hier und hat genau die Felder des Vertrags (wire.py). Eine Abweichung ist ein Programmierfehler:
Vertragsfehler (ein ValueError) mit Nachricht und Feldnamen, nie mit Werten (Regel 6); die Aufrufer im
Geraete-Thread loggen nur die Fehlerart. tools/contract_check.py ruft dieselben Bausteine fuer die Round-Trips mit
dem Server.

Ausser den Feldmengen setzen die Bausteine nur Vertragsregeln durch, die ohne Wert pruefbar sind: host aus HOSTS,
zustand aus ZUSTAENDE, die Felder von raum und faehigkeiten, die Groesse des CSR."""
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


def hello(*, device_state: str | None = None, **felder) -> dict:
    body = {"schema": wire.SCHEMA, "schemata": list(wire.SCHEMATA), **felder}
    if device_state is not None:
        body["device_state"] = device_state
    pruefen("hello", body, wire.HELLO_FIELDS, wire.HELLO_OPTIONAL)
    _host("hello", body)
    _faehigkeiten("hello.faehigkeiten", body["faehigkeiten"])
    return body


def status(*, zustand: str, runtime_status, raum, ts: str, ursprungswerte: dict | None = None) -> dict:
    body = {"schema": wire.SCHEMA, "zustand": zustand, "runtime_status": runtime_status, "raum": raum, "ts": ts}
    if ursprungswerte:
        body["ursprungswerte"] = ursprungswerte
    pruefen("status", body, wire.STATUS_FIELDS, wire.STATUS_OPTIONAL)
    if zustand not in wire.ZUSTAENDE:
        raise Vertragsfehler("status: zustand steht nicht in ZUSTAENDE")
    if raum is not None:
        if not isinstance(raum, dict):
            raise Vertragsfehler("status.raum: kein Objekt")
        pruefen("status.raum", raum, wire.RAUM_FIELDS)
    return body


def meldung(*, key: str, kategorie: str, kritisch: bool, text: str, offen: bool, ts: str) -> dict:
    return pruefen("meldung", {"key": key, "kategorie": kategorie, "kritisch": kritisch, "text": text, "offen": offen,
                               "ts": ts}, wire.NOTIFICATION_FIELDS)


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
