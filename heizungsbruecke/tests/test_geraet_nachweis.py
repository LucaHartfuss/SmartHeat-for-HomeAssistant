"""Nachweis-Ereignis `smartheat_nachweis` des Add-ons: der Geraetecode-Ablauf der Integration holt sich hier die
Signatur des Geraeteschluessels. Schema, Felder, Zeitquelle, Fehlerweg ohne Geheimnis im Log."""
import base64
import logging
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fakes import FakeHa

import heizungsbruecke.__main__ as main_module
from heizungsbruecke import geraet_nachweis
from smartheat_device import identity, wire

LOGGER = geraet_nachweis.logger.name


@pytest.fixture
def ident(tmp_path):
    return identity.load_or_create(tmp_path)


def _gueltig(ident, ereignis: dict, *, methode="POST", pfad="/einrichtung/v1/geraetecode", koerper=b"{}") -> bool:
    der = base64.urlsafe_b64decode(ereignis["signatur"] + "=" * (-len(ereignis["signatur"]) % 4))
    try:
        ident.signing_key.public_key().verify(
            der, wire.canonical_string(methode, pfad, ereignis["zeitstempel"], koerper), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


def test_das_ereignis_hat_name_schema_und_genau_die_vier_felder(ident):
    ha = FakeHa()
    geraet_nachweis.senden(ha, ident, lambda: 1_700_000_000.9)
    assert geraet_nachweis.EVENT_TYPE == "smartheat_nachweis"
    assert geraet_nachweis.EVENT_FIELDS == ("schema", "geraet_id", "zeitstempel", "signatur")
    [(typ, ereignis)] = ha.events
    assert typ == "smartheat_nachweis"
    assert tuple(ereignis) == geraet_nachweis.EVENT_FIELDS
    assert ereignis["schema"] == geraet_nachweis.EVENT_SCHEMA == 1
    assert ereignis["geraet_id"] == ident.device_id
    assert isinstance(ereignis["signatur"], str) and ereignis["signatur"]


def test_der_zeitstempel_sind_ganze_unix_sekunden_aus_der_uhr_des_aufrufers(ident):
    ha = FakeHa()
    geraet_nachweis.senden(ha, ident, lambda: 1_700_000_000.9)
    assert ha.events[0][1]["zeitstempel"] == 1_700_000_000  # abgeschnitten, nicht gerundet
    assert type(ha.events[0][1]["zeitstempel"]) is int


def test_die_standarduhr_ist_die_wanduhr():
    import inspect
    import time
    assert inspect.signature(geraet_nachweis.senden).parameters["uhr"].default is time.time


def test_die_signatur_besteht_die_serverpruefung_und_nur_sie(ident):
    ha = FakeHa()
    geraet_nachweis.senden(ha, ident, lambda: 1_700_000_000)
    ereignis = ha.events[0][1]
    assert _gueltig(ident, ereignis)
    assert not _gueltig(ident, ereignis, pfad="/devices/register")
    assert not _gueltig(ident, ereignis, koerper=b"{ }")
    assert not _gueltig(ident, ereignis, methode="GET")


def test_das_ereignis_traegt_kein_geheimnis(ident, tmp_path):
    ha = FakeHa()
    geraet_nachweis.senden(ha, ident, lambda: 5)
    text = repr(ha.events)
    for geheim in (ident.claim_code, (tmp_path / identity.KEY_FILE).read_text(), "PRIVATE", "tls_key"):
        assert geheim not in text


def test_ein_fehler_von_ha_wird_zu_einer_warnung_ohne_inhalt(ident, caplog):
    class Kaputt(FakeHa):
        def fire_event(self, event_type, data):
            raise OSError(f"geheim {data['signatur']}")

    with caplog.at_level(logging.DEBUG):
        geraet_nachweis.senden(Kaputt(), ident, lambda: 5)  # wirft nicht
    warnungen = [r for r in caplog.records if r.name == LOGGER]
    assert len(warnungen) == 1 and warnungen[0].levelno == logging.WARNING
    assert "OSError" in warnungen[0].getMessage()
    assert "geheim" not in caplog.text


def test_ein_fehler_beim_signieren_wird_zu_einer_warnung_und_sendet_nichts(ident, caplog):
    ha = FakeHa()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        geraet_nachweis.senden(ha, ident, lambda: (_ for _ in ()).throw(RuntimeError("uhr kaputt")))
    assert ha.events == []
    assert "RuntimeError" in caplog.text and "uhr kaputt" not in caplog.text


def test_ein_erfolg_schreibt_nichts_ins_log(ident, caplog):
    with caplog.at_level(logging.DEBUG):
        geraet_nachweis.senden(FakeHa(), ident, lambda: 5)
    assert caplog.text == ""


# --- Verdrahtung: stdin_anhaengen ---


class _Geraet:
    """Sicht des Geraetekerns fuer den stdin-Rueckruf: echte Identitaet, Aufgaben im Postfach protokolliert."""

    def __init__(self, ident):
        self.identity = ident
        self.aufgaben = []

    def im_geraete_thread(self, aufgabe):
        self.aufgaben.append(aufgabe)


def _anhaengen(ident, daten: bytes, ha, uhr=lambda: 1_700_000_123.4):
    import io
    geraet = _Geraet(ident)
    host = SimpleNamespace(lokal_abmelden=lambda: None)
    main_module.stdin_anhaengen(host, geraet, io.BytesIO(daten), ha, uhr).join(timeout=2)
    return geraet


def test_die_eigene_nachweis_zeile_sendet_genau_ein_ereignis(ident):
    ha = FakeHa()
    geraet = _anhaengen(ident, f"nachweis {ident.device_id}\n\n".encode(), ha)
    [(typ, ereignis)] = ha.events
    assert typ == "smartheat_nachweis" and ereignis["geraet_id"] == ident.device_id
    assert ereignis["zeitstempel"] == 1_700_000_123 and _gueltig(ident, ereignis)
    assert geraet.aufgaben == []  # der Nachweis ist kein Auftrag an den Geraete-Thread


def test_eine_fremde_nachweis_zeile_erzeugt_kein_ereignis(ident, caplog):
    ha = FakeHa()
    with caplog.at_level(logging.WARNING):
        _anhaengen(ident, b"nachweis shg-bbbbbbbbbbbbbbbb\n", ha)
    assert ha.events == []
    assert "shg-bbbbbbbbbbbbbbbb" not in caplog.text


def test_eine_nachweis_zeile_mit_anderem_befehl_oder_ohne_id_erzeugt_kein_ereignis(ident):
    ha = FakeHa()
    _anhaengen(ident, b"nachweise " + ident.device_id.encode() + b"\nnachweis\nNachweis " + ident.device_id.encode() + b"\n",
               ha)
    assert ha.events == []


def test_der_nachweis_zieht_die_zeit_bei_jeder_zeile_neu(ident):
    ha = FakeHa()
    zeiten = iter([100.0, 200.0])
    _anhaengen(ident, f"nachweis {ident.device_id}\nnachweis {ident.device_id}\n".encode(), ha, lambda: next(zeiten))
    assert [e[1]["zeitstempel"] for e in ha.events] == [100, 200]


def test_ein_nachweis_fehler_beendet_den_zeilenleser_nicht(ident):
    class Einmal(FakeHa):
        def fire_event(self, event_type, data):
            if not self.events and not getattr(self, "schon", False):
                self.schon = True
                raise OSError("HA aus")
            super().fire_event(event_type, data)

    ha = Einmal()
    geraet = _anhaengen(ident, f"nachweis {ident.device_id}\nentfernen {ident.device_id}\nnachweis {ident.device_id}\n".encode(),
                        ha)
    assert len(ha.events) == 1  # die zweite Nachweis-Zeile kam an
    assert len(geraet.aufgaben) == 1  # und das Entfernen dazwischen auch
