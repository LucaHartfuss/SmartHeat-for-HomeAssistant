"""werk.json auf der Partition werk: strenge Pruefung (lesen) und der Dienst, der die geprueften Werte fuer den Container
nach SHG_ROOT/host/werk.json kopiert und den fluechtigen Hostnamen setzt (main). Die Regeln stehen so, dass der Container
(Task 10) sie mit eigenem Code spiegeln kann: dieselben Faelle laufen spaeter gegen beide."""
import base64
import copy
import json
import logging
import os
import subprocess

import pytest

from smartheat_host import werk

SALZ = base64.b64encode(bytes(range(16))).decode()
HASH = base64.b64encode(bytes(range(32))).decode()
GUT = {
    "format": "smartheat-werk-1",
    "seriennummer": "SHG-0001",
    "hostname": "smartheat-0001",
    "passwort": {"verfahren": "scrypt", "n": 131072, "r": 8, "p": 1, "salz": SALZ, "hash": HASH},
    "bespielt": "2026-10-27",
    "hardware": "Raspberry Pi 5",
}


def _schreiben(pfad, daten):
    text = daten if isinstance(daten, (str, bytes)) else json.dumps(daten)
    pfad.write_bytes(text if isinstance(text, bytes) else text.encode())
    return pfad


def _lesen(tmp_path, daten):
    return werk.lesen(_schreiben(tmp_path / "werk.json", daten))


def _ohne(daten, *pfad):
    kopie = copy.deepcopy(daten)
    ziel = kopie
    for schluessel in pfad[:-1]:
        ziel = ziel[schluessel]
    del ziel[pfad[-1]]
    return kopie


def _mit(daten, wert, *pfad):
    kopie = copy.deepcopy(daten)
    ziel = kopie
    for schluessel in pfad[:-1]:
        ziel = ziel[schluessel]
    ziel[pfad[-1]] = wert
    return kopie


def test_gueltige_werk_json_wird_gelesen(tmp_path):
    assert _lesen(tmp_path, GUT) == GUT


@pytest.mark.parametrize("pfad", [
    ("format",), ("seriennummer",), ("hostname",), ("passwort",), ("bespielt",), ("hardware",),
    ("passwort", "verfahren"), ("passwort", "n"), ("passwort", "r"), ("passwort", "p"), ("passwort", "salz"),
    ("passwort", "hash"),
], ids=lambda pfad: ".".join(pfad))
def test_jedes_pflichtfeld_fehlt_allein_macht_die_datei_ungueltig(tmp_path, pfad):
    assert _lesen(tmp_path, _ohne(GUT, *pfad)) is None


@pytest.mark.parametrize("pfad, wert", [
    (("format",), 1), (("format",), "smartheat-werk-2"), (("format",), None),
    (("seriennummer",), 1234), (("seriennummer",), None), (("hostname",), 1), (("hostname",), ["a"]),
    (("passwort",), "scrypt"), (("passwort",), []), (("bespielt",), 20261027), (("hardware",), 5),
    (("passwort", "verfahren"), "argon2"), (("passwort", "verfahren"), 1),
    (("passwort", "n"), "131072"), (("passwort", "n"), 131072.0), (("passwort", "n"), True),
    (("passwort", "r"), "8"), (("passwort", "r"), 8.0), (("passwort", "r"), True),
    (("passwort", "p"), "1"), (("passwort", "p"), 1.0), (("passwort", "p"), True),
    (("passwort", "salz"), 5), (("passwort", "hash"), None), (("passwort", "hash"), [HASH]),
], ids=lambda x: str(x))
def test_falscher_typ_oder_wert_in_einem_feld_macht_die_datei_ungueltig(tmp_path, pfad, wert):
    assert _lesen(tmp_path, _mit(GUT, wert, *pfad)) is None


@pytest.mark.parametrize("pfad, wert", [
    (("n",), 65536), (("n",), 131073), (("r",), 4), (("r",), 16), (("p",), 2), (("p",), 0),
], ids=lambda x: str(x))
def test_andere_scrypt_parameter_sind_ungueltig(tmp_path, pfad, wert):
    assert _lesen(tmp_path, _mit(GUT, wert, "passwort", *pfad)) is None


@pytest.mark.parametrize("ort", [(), ("passwort",)], ids=["oben", "passwort"])
def test_ein_zusaetzliches_feld_macht_die_datei_ungueltig(tmp_path, ort):
    assert _lesen(tmp_path, _mit(GUT, "x", *ort, "pilot")) is None


@pytest.mark.parametrize("hostname, gueltig", [
    ("smartheat-0001", True), ("a", True), ("a" * 63, True), ("0-9", True), ("-", True),
    ("", False), ("a" * 64, False), ("Smartheat", False), ("smartHeat-1", False), ("smart_heat", False),
    ("smart.heat", False), ("smart heat", False), ("smartheat\n", False), ("smärtheat", False), ("smаrt", False),
])
def test_hostname_nur_kleinbuchstaben_ziffern_und_bindestrich_bis_63_zeichen(tmp_path, hostname, gueltig):
    daten = _mit(GUT, hostname, "hostname")
    assert (_lesen(tmp_path, daten) is not None) == gueltig


@pytest.mark.parametrize("nummer, gueltig", [
    ("ABCD", True), ("A" * 32, True), ("A-1-2", True), ("0000", True),
    ("ABC", False), ("A" * 33, False), ("abcd", False), ("ABC_1", False), ("ABCD\n", False), ("AB CD", False),
    ("", False), ("ÄBCD", False),
])
def test_seriennummer_grossbuchstaben_ziffern_bindestrich_4_bis_32_zeichen(tmp_path, nummer, gueltig):
    assert (_lesen(tmp_path, _mit(GUT, nummer, "seriennummer")) is not None) == gueltig


@pytest.mark.parametrize("name, laenge, gueltig", [
    ("salz", 16, True), ("salz", 15, False), ("salz", 17, False), ("salz", 0, False), ("salz", 32, False),
    ("hash", 32, True), ("hash", 31, False), ("hash", 33, False), ("hash", 16, False), ("hash", 0, False),
])
def test_salz_hat_16_und_hash_32_byte(tmp_path, name, laenge, gueltig):
    wert = base64.b64encode(bytes(laenge)).decode()
    assert (_lesen(tmp_path, _mit(GUT, wert, "passwort", name)) is not None) == gueltig


@pytest.mark.parametrize("name, wert", [
    ("salz", "!!!!"), ("salz", SALZ[:-1]), ("salz", SALZ + "\n"), ("salz", " " + SALZ),
    ("salz", base64.urlsafe_b64encode(b"\xfb\xff" * 8).decode()),  # URL-sicheres Alphabet statt Standard
    ("salz", SALZ.rstrip("=") + "A"),  # nicht kanonisch / falsche Laenge
    ("hash", "kein base64"), ("hash", HASH[:-2]), ("hash", HASH + "\n"), ("hash", "ä" * 44),
])
def test_salz_und_hash_sind_kanonisches_base64(tmp_path, name, wert):
    assert _lesen(tmp_path, _mit(GUT, wert, "passwort", name)) is None


def test_nicht_kanonisches_base64_wird_abgelehnt(tmp_path):
    """16 Byte mit gesetzten Restbits im letzten Zeichen dekodieren gleich, sind aber ein anderer Text."""
    kanonisch = base64.b64encode(bytes(16)).decode()
    assert kanonisch.endswith("==") and kanonisch[-3] == "A"
    verfaelscht = kanonisch[:-3] + "B" + "=="
    assert base64.b64decode(verfaelscht, validate=True) == bytes(16)
    assert _lesen(tmp_path, _mit(GUT, verfaelscht, "passwort", "salz")) is None


@pytest.mark.parametrize("text, gueltig", [
    ("2026-10-27", True), ("2000-02-29", True), ("0001-01-01", True),
    ("2026-13-01", False), ("2026-00-10", False), ("2026-02-30", False), ("2025-02-29", False),
    ("2026-1-1", False), ("20261027", False), ("2026-10-27T00:00", False), ("2026-10-27\n", False), ("", False),
    ("٢026-10-27", False),
])
def test_bespielt_ist_ein_gueltiges_datum_jjjj_mm_tt(tmp_path, text, gueltig):
    assert (_lesen(tmp_path, _mit(GUT, text, "bespielt")) is not None) == gueltig


@pytest.mark.parametrize("hardware, gueltig", [("", True), ("x" * 32, True), ("x" * 33, False)])
def test_hardware_hoechstens_32_zeichen(tmp_path, hardware, gueltig):
    assert (_lesen(tmp_path, _mit(GUT, hardware, "hardware")) is not None) == gueltig


@pytest.mark.parametrize("text, gueltig", [
    ("x" * 32, True), ("x" * 33, False), ("\u00e4" * 32, True), ("\u00e4" * 33, False), ("\U0001f600" * 32, True),
])
def test_hardware_zaehlt_zeichen_nicht_byte(tmp_path, text, gueltig):
    """Mit Nicht-ASCII-Zeichen liegt die Datei in Bytes ueber der Zeichenzahl: die Grenze zaehlt Zeichen."""
    roh = json.dumps(_mit(GUT, text, "hardware"), ensure_ascii=False).encode()
    assert (_lesen(tmp_path, roh) is not None) == gueltig


def test_genau_4_kib_sind_erlaubt_ein_byte_mehr_nicht(tmp_path):
    text = json.dumps(GUT)
    voll = (text + " " * (werk.MAX_BYTES - len(text))).encode()
    assert len(voll) == 4096 == werk.MAX_BYTES
    assert _lesen(tmp_path, voll) == GUT
    assert _lesen(tmp_path, voll + b" ") is None


def test_eine_riesige_datei_wird_nicht_gelesen(tmp_path):
    pfad = tmp_path / "werk.json"
    with open(pfad, "wb") as handle:
        handle.truncate(64 * 1024 * 1024)
    assert werk.lesen(pfad) is None


def test_ein_symlink_ist_ungueltig_auch_auf_eine_gute_datei(tmp_path):
    echt = _schreiben(tmp_path / "echt.json", GUT)
    link = tmp_path / "werk.json"
    link.symlink_to(echt)
    assert werk.lesen(link) is None
    assert werk.lesen(echt) == GUT


def test_ein_ordner_und_eine_fifo_sind_ungueltig(tmp_path):
    ordner = tmp_path / "ordner"
    ordner.mkdir()
    assert werk.lesen(ordner) is None
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert werk.lesen(fifo) is None  # kein Haengen am Oeffnen


@pytest.mark.parametrize("roh", [
    b"", b"{", b"[]", b"null", b'"text"', b"1", b'{"format": "smartheat-werk-1"}', b"\xff\xfe{}", b"\x00",
    b'{"a": NaN}',
])
def test_kein_json_objekt_ist_ungueltig(tmp_path, roh):
    assert _lesen(tmp_path, roh) is None


def test_ein_doppelter_schluessel_ist_ungueltig(tmp_path):
    text = json.dumps(GUT)
    doppelt = text[:-1] + ', "hostname": "anderer"}'
    assert json.loads(doppelt)["hostname"] == "anderer"  # json.loads nimmt still den letzten Wert
    assert _lesen(tmp_path, doppelt) is None


def test_fehlende_datei_ist_none_ohne_warnung(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        assert werk.lesen(tmp_path / "gibt-es-nicht.json") is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_ungueltige_datei_meldet_eine_warnung_ohne_inhalt(tmp_path, caplog):
    daten = _mit(_mit(GUT, "GEHEIMNIS-SALZ", "passwort", "salz"), "Smartheat-GROSS", "hostname")
    with caplog.at_level(logging.WARNING):
        assert _lesen(tmp_path, daten) is None
    warnungen = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnungen) == 1
    text = warnungen[0].getMessage() + str(warnungen[0].args)
    for inhalt in ("GEHEIMNIS", "Smartheat-GROSS", HASH, "SHG-0001", "smartheat-0001"):
        assert inhalt not in text
    assert "ungueltig" in text


@pytest.mark.parametrize("roh", [b'{"geheim": "TOPSECRET"', b"TOPSECRET", b'{"format": "TOPSECRET"}'])
def test_auch_kaputtes_json_gibt_keinen_inhalt_ins_protokoll(tmp_path, caplog, roh):
    with caplog.at_level(logging.WARNING):
        assert _lesen(tmp_path, roh) is None
    assert [r for r in caplog.records if r.levelno == logging.WARNING]
    assert all("TOPSECRET" not in r.getMessage() for r in caplog.records)


# --- Dienst ------------------------------------------------------------------------------------------------------


class Befehle:
    def __init__(self, returncode=0, fehler=None):
        self.aufrufe, self.returncode, self.fehler = [], returncode, fehler

    def __call__(self, befehl, **kwargs):
        self.aufrufe.append((list(befehl), kwargs))
        if self.fehler:
            raise self.fehler
        return subprocess.CompletedProcess(befehl, self.returncode, "", "")


@pytest.fixture
def umgebung(tmp_path, monkeypatch):
    wurzel = tmp_path / "daten"
    (wurzel / "host").mkdir(parents=True)
    monkeypatch.setenv("SHG_ROOT", str(wurzel))
    return wurzel, tmp_path / "werk.json"


def test_start_kopiert_die_geprueften_werte_und_setzt_den_hostnamen(umgebung):
    wurzel, pfad = umgebung
    _schreiben(pfad, GUT)
    befehle = Befehle()
    assert werk.main(["start"], pfad=pfad, run=befehle) == 0
    kopie = wurzel / "host" / "werk.json"
    assert json.loads(kopie.read_text()) == GUT
    assert (kopie.stat().st_mode & 0o777) == 0o644
    assert [aufruf for aufruf, _ in befehle.aufrufe] == [["hostnamectl", "--transient", "hostname", "smartheat-0001"]]
    assert befehle.aufrufe[0][1]["timeout"] > 0


def test_start_kopiert_nie_etwas_ueber_die_geprueften_werte_hinaus(umgebung):
    """Die Kopie entsteht aus den geprueften Werten, nicht aus den Rohbytes: Leerraum und Reihenfolge der Quelle
    gelangen nicht in die Kopie (kein Unterschieben zwischen Pruefung und Kopie)."""
    wurzel, pfad = umgebung
    _schreiben(pfad, json.dumps(GUT, indent=3) + "\n\n\n")
    assert werk.main(["start"], pfad=pfad, run=Befehle()) == 0
    assert json.loads((wurzel / "host" / "werk.json").read_text()) == GUT
    assert "\n\n" not in (wurzel / "host" / "werk.json").read_text()


@pytest.mark.parametrize("daten", [
    None, b"kein json", _mit(GUT, "GROSS", "hostname"), _ohne(GUT, "hardware"), _mit(GUT, "x", "pilot"),
], ids=["fehlt", "kein-json", "hostname", "pflichtfeld", "zusatzfeld"])
def test_ungueltig_oder_fehlend_loescht_die_alte_kopie_und_laesst_den_hostnamen(umgebung, daten):
    wurzel, pfad = umgebung
    kopie = wurzel / "host" / "werk.json"
    kopie.write_text(json.dumps(GUT))
    if daten is not None:
        _schreiben(pfad, daten)
    befehle = Befehle()
    assert werk.main(["start"], pfad=pfad, run=befehle) == 0
    assert not kopie.exists() and befehle.aufrufe == []


def test_ungueltig_ohne_alte_kopie_ist_kein_fehler(umgebung):
    wurzel, pfad = umgebung
    assert werk.main(["start"], pfad=pfad, run=Befehle()) == 0
    assert not (wurzel / "host" / "werk.json").exists()


def test_ungueltig_loescht_einen_symlink_als_kopie_nicht_sein_ziel(umgebung, tmp_path):
    wurzel, pfad = umgebung
    fremd = tmp_path / "fremd.txt"
    fremd.write_text("bleibt")
    (wurzel / "host" / "werk.json").symlink_to(fremd)
    assert werk.main(["start"], pfad=pfad, run=Befehle()) == 0
    assert not (wurzel / "host" / "werk.json").is_symlink() and fremd.read_text() == "bleibt"


def test_gueltig_ersetzt_einen_symlink_als_kopie_statt_ihm_zu_folgen(umgebung, tmp_path):
    wurzel, pfad = umgebung
    fremd = tmp_path / "fremd.txt"
    fremd.write_text("bleibt")
    (wurzel / "host" / "werk.json").symlink_to(fremd)
    _schreiben(pfad, GUT)
    assert werk.main(["start"], pfad=pfad, run=Befehle()) == 0
    assert fremd.read_text() == "bleibt"
    assert not (wurzel / "host" / "werk.json").is_symlink()


def test_die_neue_kopie_ersetzt_die_alte(umgebung):
    wurzel, pfad = umgebung
    (wurzel / "host" / "werk.json").write_text("alt")
    _schreiben(pfad, _mit(GUT, "anderer-name", "hostname"))
    assert werk.main(["start"], pfad=pfad, run=Befehle()) == 0
    assert json.loads((wurzel / "host" / "werk.json").read_text())["hostname"] == "anderer-name"


@pytest.mark.parametrize("fehler", [None, OSError("kein hostnamectl"), subprocess.TimeoutExpired("hostnamectl", 10)],
                         ids=["rc1", "oserror", "timeout"])
def test_scheitert_der_hostname_endet_der_dienst_mit_1_die_kopie_bleibt(umgebung, fehler):
    wurzel, pfad = umgebung
    _schreiben(pfad, GUT)
    assert werk.main(["start"], pfad=pfad, run=Befehle(returncode=1, fehler=fehler)) == 1
    assert json.loads((wurzel / "host" / "werk.json").read_text()) == GUT


@pytest.mark.parametrize("argv", [[], ["stop"], ["start", "x"], ["--help"]])
def test_nur_start_ist_ein_gueltiger_aufruf(umgebung, argv, capsys):
    wurzel, pfad = umgebung
    _schreiben(pfad, GUT)
    befehle = Befehle()
    assert werk.main(argv, pfad=pfad, run=befehle) == 2
    assert befehle.aufrufe == [] and not (wurzel / "host" / "werk.json").exists()
    assert "start" in capsys.readouterr().err


def test_der_dienst_nutzt_den_standardpfad_auf_werk():
    assert str(werk.WERK_PFAD) == "/werk/werk.json" and werk.MAX_BYTES == 4096
