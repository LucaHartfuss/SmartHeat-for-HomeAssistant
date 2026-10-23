"""Bausteine der Geraetenachrichten: jede Nachricht hat genau die Felder des Vertrags."""
import pytest

from smartheat_device import commands, nachrichten, wire

FAEHIGKEITEN = {"drivers": [], "zigbee": False}
RAUM = {"ist": 20.5, "soll": 21.0, "soll_quelle": "portal", "ts": "t"}


def _hello_felder() -> dict:
    felder = {name: None for name in wire.HELLO_FIELDS if name not in ("schema", "schemata")}
    return felder | {"host": "gateway", "faehigkeiten": dict(FAEHIGKEITEN)}


def _register_felder() -> dict:
    return {name: "x" for name in wire.REGISTER_FIELDS} | {"host": "ha", "capabilities": dict(FAEHIGKEITEN)}


def test_pruefen_laesst_genau_den_vertrag_durch():
    assert nachrichten.pruefen("x", {"a": 1, "b": 2}, ("a",), ("b",)) == {"a": 1, "b": 2}
    with pytest.raises(nachrichten.Vertragsfehler, match="x.*fehlt.*a"):
        nachrichten.pruefen("x", {"b": 2}, ("a",), ("b",))
    with pytest.raises(nachrichten.Vertragsfehler, match="x.*fremd.*c"):
        nachrichten.pruefen("x", {"a": 1, "c": 3}, ("a",), ("b",))


def test_pruefen_nennt_nie_werte():
    with pytest.raises(nachrichten.Vertragsfehler) as fehler:
        nachrichten.pruefen("x", {"geheim": "WERT-123"}, ("a",))
    assert "WERT-123" not in str(fehler.value)


def test_hello_hat_genau_die_vertragsfelder():
    body = nachrichten.hello(**_hello_felder())
    assert set(body) == set(wire.HELLO_FIELDS)
    assert body["schema"] == wire.SCHEMA and body["schemata"] == list(wire.SCHEMATA)


def test_hello_mit_device_state():
    assert set(nachrichten.hello(**_hello_felder(), device_state="x")) == set(wire.HELLO_FIELDS) | {"device_state"}


def test_hello_prueft_host_und_faehigkeiten_ohne_werte():
    with pytest.raises(nachrichten.Vertragsfehler, match="hello.*host"):
        nachrichten.hello(**(_hello_felder() | {"host": "GEHEIM-HOST"}))
    for faehigkeiten in ({**FAEHIGKEITEN, "GEHEIM-FELD": 1}, {"drivers": []}, None):
        with pytest.raises(nachrichten.Vertragsfehler, match="faehigkeiten"):
            nachrichten.hello(**(_hello_felder() | {"faehigkeiten": faehigkeiten}))
    for optional in wire.CAPABILITY_OPTIONAL:
        nachrichten.hello(**(_hello_felder() | {"faehigkeiten": {**FAEHIGKEITEN, optional: True}}))


def test_status_meldung_notifications_ergebnis_register_zertifikat():
    status = nachrichten.status(zustand="regelt", runtime_status=None, raum=None, ts="t")
    assert set(status) == set(wire.STATUS_FIELDS)
    assert set(nachrichten.status(zustand="regelt", runtime_status=None, raum=None, ts="t", ursprungswerte={"a": 1})) \
        == set(wire.STATUS_FIELDS) | {"ursprungswerte"}
    item = nachrichten.meldung(key="k", kategorie="k", kritisch=False, text="t", offen=True, ts="t")
    assert set(item) == set(wire.NOTIFICATION_FIELDS)
    assert set(nachrichten.notifications([item])) == set(wire.NOTIFICATIONS_FIELDS)
    assert set(nachrichten.ergebnis_dokument("konfiguration", 1, True)) <= set(wire.RESULT_FIELDS) | set(
        wire.RESULT_TARGETS)
    assert set(nachrichten.zertifikat("csr")) == set(wire.CERTIFICATE_FIELDS)
    assert set(nachrichten.register(**_register_felder())) == set(wire.REGISTER_FIELDS)
    assert set(nachrichten.register(**_register_felder(), werks_nachweis="n")) \
        == set(wire.REGISTER_FIELDS) | set(wire.REGISTER_OPTIONAL)


def test_status_prueft_zustand_und_raum_ohne_werte():
    assert nachrichten.status(zustand="regelt", runtime_status=None, raum=RAUM, ts="t")["raum"] == RAUM
    for zustand in wire.ZUSTAENDE:
        nachrichten.status(zustand=zustand, runtime_status=None, raum=None, ts="t")
    with pytest.raises(nachrichten.Vertragsfehler, match="status.*zustand"):
        nachrichten.status(zustand="GEHEIM-ZUSTAND", runtime_status=None, raum=None, ts="t")
    for raum in ({**RAUM, "GEHEIM-FELD": 1}, {"ist": 20.0}, [1]):
        with pytest.raises(nachrichten.Vertragsfehler, match="raum"):
            nachrichten.status(zustand="regelt", runtime_status=None, raum=raum, ts="t")


def test_register_prueft_host_faehigkeiten_und_csr_groesse():
    with pytest.raises(nachrichten.Vertragsfehler, match="register.*host"):
        nachrichten.register(**(_register_felder() | {"host": "x"}))
    with pytest.raises(nachrichten.Vertragsfehler, match="capabilities"):
        nachrichten.register(**(_register_felder() | {"capabilities": {"zigbee": True}}))
    # CSR_MAX_BYTES = 8192 als Literal aus V1 (nicht ueber die Konstante): genau 8192 Bytes gehen, 8193 nicht
    with pytest.raises(nachrichten.Vertragsfehler, match="csr"):
        nachrichten.register(**(_register_felder() | {"csr": "x" * 8193}))
    with pytest.raises(nachrichten.Vertragsfehler, match="csr"):
        nachrichten.zertifikat("x" * 8193)
    assert nachrichten.zertifikat("x" * 8192) == {"csr": "x" * 8192}
    assert nachrichten.register(**(_register_felder() | {"csr": "x" * 8192}))["csr"] == "x" * 8192
    with pytest.raises(nachrichten.Vertragsfehler, match="csr"):
        nachrichten.zertifikat("ä" * 4097)  # 8194 Bytes in UTF-8 bei 4097 Zeichen: gezaehlt werden Bytes


def test_ergebnis_dokument_mit_fehler():
    body = nachrichten.ergebnis_dokument("konfiguration", 2, False, {"grund": "ungueltig", "details": None})
    assert body == {"schema": 1, "dokument": "konfiguration", "version": 2, "ok": False,
                    "error": {"grund": "ungueltig", "details": None}}


def test_befehls_ergebnis_hat_genau_die_pflichtfelder_des_befehls():
    # Bruchtest: Literal statt Konstante
    body = commands.result_payload("c" * 32, True, {"devices": []}, None)
    assert set(body) == {"schema", "ok", "result", "error", "command_id"}


def test_dokument_ergebnis_hat_nie_result_und_error_nur_bei_ablehnung():
    ok = nachrichten.ergebnis_dokument("bedienung", 2, True)
    abgelehnt = nachrichten.ergebnis_dokument("konfiguration", 3, False, {"grund": "ungueltig"})
    assert set(ok) == {"schema", "ok", "dokument", "version"}
    assert set(abgelehnt) == {"schema", "ok", "dokument", "version", "error"}
