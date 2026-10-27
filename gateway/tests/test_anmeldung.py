"""LAN-Seite hinter dem Etikett-Passwort (SG-05, S-13, GW 8, GW 21 "LAN-Seite"): Pruefung, globale Drossel mit Lock,
Sitzungen, Host-Header, kein Banner, Gesundheitsserver ohne Sitzung. Die Drossel-Tests laufen gegen eine Attrappe der
Pruefung (zaehlt, wie oft "scrypt" rechnet, und wie viele zugleich); `test_pruefe_mit_echtem_scrypt` rechnet echt."""
import base64
import hashlib
import http.client
import json
import logging
import re
import threading
import time

import pytest

from smartheat_gateway import anmeldung
from smartheat_gateway.anmeldung import Pruefstelle, Sitzungen, Zugang, passwort_eintrag, pruefe, werk_lesen
from smartheat_gateway.diagnose import start_server

SALZ = bytes(range(16))
RICHTIG = "richtiges-Passwort"
EINTRAG = passwort_eintrag(RICHTIG, SALZ)  # einmal echt gerechnet; die Tests unten benutzen den Eintrag nur zum Gueltigsein
WERK = {
    "format": "smartheat-werk-1", "seriennummer": "SHG-0001", "hostname": "smartheat-0001", "passwort": EINTRAG,
    "bespielt": "2026-10-27", "hardware": "Raspberry Pi 5",
}
SNAPSHOT = {
    "geraet": "shg-abcdefghijklmnop", "zustand": "nicht_uebernommen", "eingerichtet": False,
    "versionen": {"gateway": "0.9.0"}, "server_ok": True, "laufzeit": None, "zigbee_stick": True, "host": {},
    "hinweise": [], "meldungen": [], "konfiguration_abgelehnt": None, "code": "ABCDEFGHJKLM",
}
COOKIE = re.compile(r"shg_sitzung=([A-Za-z0-9_-]{43}); HttpOnly; SameSite=Strict; Path=/; Max-Age=3600")


class Uhr:
    def __init__(self) -> None:
        self.jetzt = 1000.0

    def __call__(self) -> float:
        return self.jetzt


class Pruefer:
    """Attrappe fuer anmeldung.pruefe: nur RICHTIG passt; zaehlt Aufrufe und gleichzeitige Rechnungen (max)."""

    def __init__(self) -> None:
        self.aufrufe = 0
        self.gleichzeitig = 0
        self.hoechstens = 0
        self.dauer = 0.0
        self.halt: threading.Event | None = None
        self.drin = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, eintrag: dict, passwort: str) -> bool:
        with self._lock:
            self.aufrufe += 1
            self.gleichzeitig += 1
            self.hoechstens = max(self.hoechstens, self.gleichzeitig)
        try:
            self.drin.set()
            if self.halt is not None:
                assert self.halt.wait(10)
            time.sleep(self.dauer)
            return passwort == RICHTIG
        finally:
            with self._lock:
                self.gleichzeitig -= 1


@pytest.fixture
def pruefer(monkeypatch):
    attrappe = Pruefer()
    monkeypatch.setattr(anmeldung, "pruefe", attrappe)
    return attrappe


@pytest.fixture
def uhr():
    return Uhr()


def stelle(uhr: Uhr) -> Pruefstelle:
    return Pruefstelle(lambda: EINTRAG, uhr)


# --- Pruefstelle: Drossel und Lock -------------------------------------------------------------------------------


def fehlversuche(pruefstelle: Pruefstelle, anzahl: int) -> list[str]:
    return [pruefstelle.anmelden("falsch") for _ in range(anzahl)]


def test_vier_fehlversuche_sperren_noch_nicht(pruefer, uhr):
    pruefstelle = stelle(uhr)
    assert fehlversuche(pruefstelle, 4) == ["falsch"] * 4
    assert pruefstelle.anmelden(RICHTIG) == "ok" and pruefer.aufrufe == 5


def test_drossel_nach_fuenf_fehlversuchen_60s_dann_doppelt_bis_3600(pruefer, uhr):
    """Die n-te Sperre dauert 60 s, 120 s, ... verdoppelt, hoechstens 3600 s: eine Millisekunde vor dem Ende wird
    abgewiesen (auch mit dem richtigen Passwort), genau zum Ende wird wieder geprueft (und ein Fehlversuch loest die
    naechste Sperre aus)."""
    pruefstelle = stelle(uhr)
    assert fehlversuche(pruefstelle, 4) == ["falsch"] * 4
    for dauer in (60, 120, 240, 480, 960, 1920, 3600, 3600):
        assert pruefstelle.anmelden("falsch") == "falsch"  # loest die Sperre aus (5. bzw. der naechste Fehlversuch)
        beginn = uhr.jetzt
        uhr.jetzt = beginn + dauer - 0.001
        assert pruefstelle.anmelden(RICHTIG) == "gesperrt", dauer
        uhr.jetzt = beginn + dauer
    assert pruefstelle.anmelden(RICHTIG) == "ok"


def test_die_sperrdauern_folgen_der_reihe_60_bis_3600(pruefer, uhr):
    """Gemessen ueber sperre_rest: Dauer = 60, dann jeweils doppelt, ab 3840 gedeckelt auf 3600."""
    pruefstelle = stelle(uhr)
    fehlversuche(pruefstelle, 4)
    dauern = []
    for _ in range(9):
        assert pruefstelle.anmelden("falsch") == "falsch"
        dauern.append(pruefstelle.sperre_rest())
        uhr.jetzt += dauern[-1]  # genau bis zum Ablauf (aufgerundet auf ganze Sekunden)
    assert dauern == [60, 120, 240, 480, 960, 1920, 3600, 3600, 3600]


def test_waehrend_sperre_wird_nicht_geprueft(pruefer, uhr):
    pruefstelle = stelle(uhr)
    fehlversuche(pruefstelle, 5)
    assert pruefer.aufrufe == 5
    for passwort in (RICHTIG, "falsch"):
        assert pruefstelle.anmelden(passwort) == "gesperrt"
    uhr.jetzt += 59.9
    assert pruefstelle.anmelden(RICHTIG) == "gesperrt"
    assert pruefer.aufrufe == 5  # kein scrypt in der Sperre
    uhr.jetzt += 0.1
    assert pruefstelle.anmelden(RICHTIG) == "ok" and pruefer.aufrufe == 6


def test_ein_versuch_in_der_sperre_verlaengert_sie_nicht(pruefer, uhr):
    pruefstelle = stelle(uhr)
    fehlversuche(pruefstelle, 5)
    uhr.jetzt += 30
    assert pruefstelle.anmelden("falsch") == "gesperrt"
    uhr.jetzt += 30  # 60 s nach dem 5. Fehlversuch
    assert pruefstelle.anmelden(RICHTIG) == "ok"


def test_erfolg_setzt_drossel_zurueck(pruefer, uhr):
    pruefstelle = stelle(uhr)
    fehlversuche(pruefstelle, 5)  # Sperre 60 s
    uhr.jetzt += 60
    assert pruefstelle.anmelden("falsch") == "falsch"  # Sperre 120 s
    uhr.jetzt += 120
    assert pruefstelle.anmelden(RICHTIG) == "ok"
    assert fehlversuche(pruefstelle, 4) == ["falsch"] * 4  # der Zaehler begann neu: noch keine Sperre
    assert pruefstelle.anmelden("falsch") == "falsch"
    assert pruefstelle.sperre_rest() == 60  # und die Sperre beginnt wieder bei 60 s, nicht bei 240


def test_die_erste_anmeldung_gilt_auch_kurz_nach_dem_start_der_uhr(pruefer, uhr):
    """Die monotone Uhr beginnt beim Start des Systems; keine Sperre steht, auch nicht bei 0,2 s."""
    uhr.jetzt = 0.2
    assert stelle(uhr).anmelden(RICHTIG) == "ok"


def test_retry_after_ist_mindestens_eine_sekunde(pruefer, uhr):
    pruefstelle = stelle(uhr)
    fehlversuche(pruefstelle, 5)
    uhr.jetzt += 59.5
    assert pruefstelle.sperre_rest() == 1
    uhr.jetzt += 10  # lange vorbei
    assert pruefstelle.sperre_rest() == 1


def test_ohne_werk_keine_anmeldung_und_keine_rechnung(pruefer, uhr):
    pruefstelle = Pruefstelle(lambda: None, uhr)
    assert pruefstelle.anmelden(RICHTIG) == "kein_werk" and pruefer.aufrufe == 0


def test_zwei_gleichzeitige_anmeldungen_ein_scrypt(pruefer, uhr):
    """Die zweite Anmeldung, waehrend die erste rechnet, wird als `belegt` abgewiesen: nicht geprueft, nicht gezaehlt."""
    pruefstelle = stelle(uhr)
    pruefer.halt = threading.Event()
    erste: list[str] = []
    faden = threading.Thread(target=lambda: erste.append(pruefstelle.anmelden("falsch")))
    faden.start()
    assert pruefer.drin.wait(5)
    assert [pruefstelle.anmelden(RICHTIG) for _ in range(3)] == ["belegt"] * 3
    assert pruefer.aufrufe == 1
    pruefer.halt.set()
    faden.join(5)
    assert erste == ["falsch"]
    # nur der eine Fehlversuch zaehlt: vier weitere sperren noch nicht, der fuenfte insgesamt schon
    assert fehlversuche(pruefstelle, 3) == ["falsch"] * 3
    assert pruefstelle.anmelden("falsch") == "falsch" and pruefstelle.anmelden(RICHTIG) == "gesperrt"


def test_zehn_parallele_fehlversuche_hoechstens_fuenf_scrypt(pruefer, uhr):
    pruefer.dauer = 0.02
    pruefstelle = stelle(uhr)
    ergebnisse: list[str] = []

    def versuch() -> None:
        while (ergebnis := pruefstelle.anmelden("falsch")) == "belegt":
            time.sleep(0.001)
        ergebnisse.append(ergebnis)

    faeden = [threading.Thread(target=versuch) for _ in range(10)]
    for faden in faeden:
        faden.start()
    for faden in faeden:
        faden.join(20)
    assert sorted(ergebnisse) == ["falsch"] * 5 + ["gesperrt"] * 5
    assert pruefer.aufrufe == 5 and pruefer.hoechstens == 1


# --- Sitzungen ---------------------------------------------------------------------------------------------------


def test_sitzung_laeuft_nach_einer_stunde_ab(uhr):
    sitzungen = Sitzungen(uhr)
    token = sitzungen.neu()
    assert sitzungen.gueltig(token) is True
    uhr.jetzt += 3599.9
    assert sitzungen.gueltig(token) is True
    uhr.jetzt += 0.1
    assert sitzungen.gueltig(token) is False


@pytest.mark.parametrize("token", [None, "", "x", "A" * 43])
def test_unbekannte_tokens_sind_ungueltig(uhr, token):
    sitzungen = Sitzungen(uhr)
    sitzungen.neu()
    assert sitzungen.gueltig(token) is False


def test_siebzehnte_sitzung_verdraengt_die_aelteste(uhr):
    sitzungen = Sitzungen(uhr)
    tokens = [sitzungen.neu() for _ in range(16)]
    assert all(sitzungen.gueltig(token) for token in tokens)
    siebzehnte = sitzungen.neu()
    assert not sitzungen.gueltig(tokens[0])
    assert all(sitzungen.gueltig(token) for token in [*tokens[1:], siebzehnte])
    assert len(set(tokens) | {siebzehnte}) == 17  # secrets.token_urlsafe(32): 43 Zeichen, jedes Mal neu
    assert all(len(token) == 43 for token in tokens)


# --- die echte Pruefung ------------------------------------------------------------------------------------------


def test_pruefe_mit_echtem_scrypt():
    """Kein Fake: Parameter n=131072, r=8, p=1, Hash 32 Byte, Salz 16 Byte; richtig und falsch; unter 5 s."""
    beginn = time.monotonic()
    eintrag = passwort_eintrag("abc DEF 123", SALZ)
    assert pruefe(eintrag, "abc DEF 123") is True
    assert pruefe(eintrag, "abc DEF 124") is False
    assert pruefe(eintrag, "") is False
    assert time.monotonic() - beginn < 5
    erwartet = hashlib.scrypt(b"abc DEF 123", salt=SALZ, n=131072, r=8, p=1, maxmem=268435456, dklen=32)
    assert eintrag == {"verfahren": "scrypt", "n": 131072, "r": 8, "p": 1, "salz": base64.b64encode(SALZ).decode(),
                       "hash": base64.b64encode(erwartet).decode()}


@pytest.mark.parametrize("feld", ["salz", "hash"])
def test_pruefe_nimmt_keinen_eintrag_mit_ungueltigem_base64(feld):
    """Der Eintrag kommt aus werk_lesen und ist kanonisches Base64; ein anderer ist ein Fehler, kein stilles Verwerfen
    von Zeichen."""
    eintrag = passwort_eintrag("abc", SALZ)
    eintrag[feld] += "\n"  # validate=False verwuerfe das Zeichen still
    with pytest.raises(ValueError):
        pruefe(eintrag, "abc")


def test_das_salz_hat_16_byte():
    with pytest.raises(ValueError):
        passwort_eintrag("x", bytes(15))


# --- werk.json im Container --------------------------------------------------------------------------------------


def test_werk_lesen_nimmt_das_gueltige_werk_und_lehnt_ein_zusatzfeld_ab(tmp_path):
    pfad = tmp_path / "werk.json"
    pfad.write_text(json.dumps(WERK))
    assert werk_lesen(pfad) == WERK
    pfad.write_text(json.dumps({**WERK, "pilot": "x"}))
    assert werk_lesen(pfad) is None
    assert werk_lesen(tmp_path / "gibt-es-nicht.json") is None


# --- HTTP ---------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def lan(tmp_path_factory):
    """Ein Server fuer das Modul (das Binden dauert rund 1,4 s); Zugang, Uhr und Werk setzt `seite` je Test zurueck."""
    verzeichnis = tmp_path_factory.mktemp("host")
    pfad = verzeichnis / "werk.json"
    uhr = Uhr()
    zugang = Zugang(pfad, uhr)
    aufrufe: list[int] = []

    def snapshot() -> dict:
        aufrufe.append(1)
        return SNAPSHOT

    server = start_server(0, snapshot, zugang=zugang)
    try:
        yield {"port": server.server_address[1], "zugang": zugang, "uhr": uhr, "pfad": pfad, "aufrufe": aufrufe}
    finally:
        server.shutdown()
        server.server_close()


class Seite:
    def __init__(self, lan: dict) -> None:
        self.__dict__.update(lan)

    def anfrage(self, methode: str, pfad: str, koerper: bytes | None = None, kopf: dict | None = None,
                host: str | None = None, port: int | None = None) -> tuple[int, bytes, http.client.HTTPMessage]:
        verbindung = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=10)
        try:
            verbindung.putrequest(methode, pfad, skip_host=True, skip_accept_encoding=True)
            verbindung.putheader("Host", host or f"127.0.0.1:{self.port}")
            for name, wert in (kopf or {}).items():
                verbindung.putheader(name, wert)
            verbindung.endheaders(koerper)
            antwort = verbindung.getresponse()
            return antwort.status, antwort.read(), antwort.headers
        finally:
            verbindung.close()

    def anmelden(self, passwort: str = RICHTIG, **kwargs) -> tuple[int, bytes, http.client.HTTPMessage]:
        koerper = f"passwort={passwort}".encode()
        return self.formular(koerper, **kwargs)

    def formular(self, koerper: bytes, typ: str | None = "application/x-www-form-urlencoded",
                 laenge: str | None = None, kopf_zusatz: dict | None = None,
                 **kwargs) -> tuple[int, bytes, http.client.HTTPMessage]:
        kopf = {"Content-Length": laenge if laenge is not None else str(len(koerper)), **(kopf_zusatz or {})}
        if typ:
            kopf["Content-Type"] = typ
        return self.anfrage("POST", "/anmelden", koerper, kopf, **kwargs)

    def sitzung(self) -> str:
        status, _, kopf = self.anmelden()
        assert status == 303
        treffer = COOKIE.fullmatch(kopf["Set-Cookie"])
        assert treffer
        return f"shg_sitzung={treffer.group(1)}"


@pytest.fixture
def seite(lan, pruefer):
    lan["pfad"].write_text(json.dumps(WERK))
    lan["uhr"].jetzt = 1000.0
    lan["zugang"].pruefstelle = Pruefstelle(lan["zugang"]._eintrag, lan["uhr"])
    lan["zugang"].sitzungen = Sitzungen(lan["uhr"])
    lan["aufrufe"].clear()
    return Seite(lan)


def test_ohne_sitzung_nur_anmeldung(seite):
    """Ohne Sitzung kommt nur das Formular; Zustand, Seriennummer, Geraete-ID und Code kommen nie (und der Zustand wird
    nicht einmal gelesen). Auch /healthz an diesem Port ist ohne Sitzung 401."""
    status, koerper, _ = seite.anfrage("GET", "/anmelden")
    assert status == 200 and b'name="passwort"' in koerper and b"<form" in koerper
    for pfad in ("/", "/x", "/index.html", "/healthz/"):
        status, koerper, kopf = seite.anfrage("GET", pfad)
        assert (status, koerper, kopf["Location"]) == (303, b"", "/anmelden"), pfad
    assert seite.anfrage("GET", "/healthz")[:2] == (401, b"")
    assert seite.anfrage("POST", "/")[:2] == (401, b"")
    assert seite.anfrage("POST", "/healthz", b"x", {"Content-Length": "1"})[:2] == (401, b"")
    for pfad in ("/anmelden", "/", "/healthz"):
        _, koerper, kopf = seite.anfrage("GET", pfad)
        text = koerper + str(kopf).encode()
        for geheim in (b"ABCDEFGHJKLM", b"shg-abcdefghijklmnop", b"SHG-0001", b"0.9.0"):
            assert geheim not in text, (pfad, geheim)
    assert seite.aufrufe == []


def test_richtiges_passwort_setzt_cookie(seite, pruefer):
    status, koerper, kopf = seite.anmelden()
    assert (status, koerper, kopf["Location"]) == (303, b"", "/")
    assert COOKIE.fullmatch(kopf["Set-Cookie"]) and len(kopf.get_all("Set-Cookie")) == 1
    assert kopf["Cache-Control"] == "no-store" and pruefer.aufrufe == 1


def test_mit_sitzung_zeigt_die_seite_zustand_und_seriennummer(seite):
    cookie = seite.sitzung()
    status, koerper, _ = seite.anfrage("GET", "/", kopf={"Cookie": cookie})
    text = koerper.decode()
    assert status == 200 and "SHG-0001" in text and "ABCDEFGHJKLM" in text  # Code laut Seitenauszug (Task 8)
    assert "0.9.0" not in text and "Version" not in text
    status, koerper, _ = seite.anfrage("GET", "/healthz", kopf={"Cookie": f"a=b; {cookie}; c=d"})
    assert status == 200 and json.loads(koerper)["zustand"] == "nicht_uebernommen"
    assert seite.anfrage("GET", "/x", kopf={"Cookie": cookie})[0] == 404


def test_die_seite_nennt_nichts_von_abwesenheit_plan_oder_leerstand(seite):
    cookie = seite.sitzung()
    _, koerper, _ = seite.anfrage("GET", "/", kopf={"Cookie": cookie})
    text = koerper.decode().lower()
    for wort in ("abwesend", "kuehl", "kühl", "uebersteuert", "übersteuert", "leerstand", "wochenplan", "plan"):
        assert wort not in text, wort


def test_ein_fremdes_oder_abgelaufenes_cookie_gilt_nicht(seite):
    cookie = seite.sitzung()
    assert seite.anfrage("GET", "/", kopf={"Cookie": "shg_sitzung=" + "A" * 43})[0] == 303
    assert seite.anfrage("GET", "/", kopf={"Cookie": "andere=" + cookie.split("=")[1]})[0] == 303
    assert seite.anfrage("GET", "/", kopf={"Cookie": cookie})[0] == 200
    seite.uhr.jetzt += 3600
    assert seite.anfrage("GET", "/", kopf={"Cookie": cookie})[0] == 303


def test_falsches_passwort_zaehlt(seite, pruefer):
    status, koerper, kopf = seite.anmelden("falsch")
    assert status == 401 and b"falsch" in koerper and "Set-Cookie" not in kopf
    assert pruefer.aufrufe == 1
    for _ in range(3):
        assert seite.anmelden("falsch")[0] == 401
    assert seite.anmelden("falsch")[0] == 401  # der fuenfte lost die Sperre aus
    status, _, kopf = seite.anmelden(RICHTIG)
    assert status == 429 and 1 <= int(kopf["Retry-After"]) <= 60 and "Set-Cookie" not in kopf
    assert pruefer.aufrufe == 5  # waehrend der Sperre wird nicht geprueft
    seite.uhr.jetzt += 60
    assert seite.anmelden(RICHTIG)[0] == 303


def test_retry_after_nennt_die_restdauer_der_sperre(seite):
    for _ in range(5):
        seite.anmelden("falsch")
    seite.uhr.jetzt += 20.5
    assert seite.anmelden(RICHTIG)[2]["Retry-After"] == "40"


def test_belegt_ist_429_mit_retry_after_eins(seite, pruefer):
    pruefer.halt = threading.Event()
    erste: list[int] = []
    faden = threading.Thread(target=lambda: erste.append(seite.anmelden("falsch")[0]))
    faden.start()
    assert pruefer.drin.wait(5)
    status, _, kopf = seite.anmelden(RICHTIG)
    assert (status, kopf["Retry-After"]) == (429, "1") and pruefer.aufrufe == 1
    pruefer.halt.set()
    faden.join(5)
    assert erste == [401]


def test_viele_parallele_anmeldungen_rechnen_hoechstens_eine_zugleich(seite, pruefer):
    """Review-Fokus 5: 30 gleichzeitige Anmeldungen aus dem LAN: nie mehr als ein scrypt zugleich (Speicher), hoechstens
    fuenf Fehlversuche kommen ueberhaupt an die Pruefung, alle anderen bekommen 429 (Sperre bzw. belegt)."""
    pruefer.dauer = 0.05
    status: list[int] = []

    def versuch() -> None:
        status.append(seite.anmelden("falsch")[0])

    faeden = [threading.Thread(target=versuch) for _ in range(30)]
    for faden in faeden:
        faden.start()
    for faden in faeden:
        faden.join(20)
    assert len(status) == 30 and set(status) <= {401, 429} and 429 in status
    assert pruefer.hoechstens == 1 and 1 <= pruefer.aufrufe <= 5
    assert status.count(401) == pruefer.aufrufe


def test_ohne_werk_keine_anmeldung(seite, pruefer):
    for inhalt in (None, "{kaputt", json.dumps({**WERK, "pilot": "x"})):
        if inhalt is None:
            seite.pfad.unlink()
        else:
            seite.pfad.write_text(inhalt)
        status, _, kopf = seite.anmelden(RICHTIG)
        assert status == 503 and "Set-Cookie" not in kopf
    assert pruefer.aufrufe == 0
    assert seite.anfrage("GET", "/")[0] == 303  # auch ohne Werk keine Seite ohne Sitzung


def test_fremder_host_header_403(seite):
    cookie = seite.sitzung()
    for host in ("evil.example", f"evil.example:{seite.port}", "smartheat-0001.local", "smartheat-0002"):
        for methode, pfad in (("GET", "/anmelden"), ("GET", "/"), ("GET", "/healthz"), ("POST", "/anmelden")):
            status, koerper, _ = seite.anfrage(methode, pfad, kopf={"Cookie": cookie, "Content-Length": "0"}, host=host)
            assert (status, koerper) == (403, b""), (host, methode, pfad)  # auch mit gueltiger Sitzung
    for host in ("smartheat-0001", f"SMARTHEAT-0001:{seite.port}", "localhost", "192.168.2.5"):
        assert seite.anfrage("GET", "/", kopf={"Cookie": cookie}, host=host)[0] == 200, host


def test_hostname_aus_dem_werk_gilt_erst_mit_dem_werk(seite):
    seite.pfad.write_text(json.dumps({**WERK, "hostname": "anderer-name"}))
    assert seite.anfrage("GET", "/anmelden", host="anderer-name")[0] == 200
    assert seite.anfrage("GET", "/anmelden", host="smartheat-0001")[0] == 403
    seite.pfad.unlink()
    assert seite.anfrage("GET", "/anmelden", host="anderer-name")[0] == 403


def test_kein_server_banner(seite):
    cookie = seite.sitzung()
    antworten = [
        seite.anfrage("GET", "/anmelden"), seite.anfrage("GET", "/", kopf={"Cookie": cookie}),
        seite.anfrage("GET", "/x", kopf={"Cookie": cookie}), seite.anfrage("GET", "/healthz"),
        seite.anfrage("PUT", "/", kopf={"Cookie": cookie}), seite.anfrage("GET", "/", host="evil.example"),
        seite.anmelden("falsch"),
    ]
    assert [a[0] for a in antworten] == [200, 200, 404, 401, 501, 403, 401]
    for _, koerper, kopf in antworten:
        assert "Server" not in kopf and "X-Powered-By" not in kopf
        text = (str(kopf) + koerper.decode()).lower()
        assert "python" not in text and "basehttp" not in text and "0.9.0" not in text


def test_fehlversuch_loggt_ohne_eingabe(seite, caplog):
    geheim = "mein-tippfehler-Passwort-4711"
    with caplog.at_level(logging.WARNING):
        assert seite.anmelden(geheim)[0] == 401
    meldungen = [r for r in caplog.records if r.levelno == logging.WARNING and "fehlgeschlagen" in r.getMessage()]
    assert len(meldungen) == 1
    assert geheim not in caplog.text and "4711" not in caplog.text


def test_der_erfolg_loggt_keinen_fehlversuch(seite, caplog):
    with caplog.at_level(logging.WARNING):
        assert seite.anmelden(RICHTIG)[0] == 303
    assert caplog.records == []


@pytest.mark.parametrize(("typ", "status"), [
    ("text/plain", 415), ("application/json", 415), ("multipart/form-data; boundary=x", 415), (None, 415),
    ("application/x-www-form-urlencoded", 303), ("application/x-www-form-urlencoded; charset=UTF-8", 303),
])
def test_nur_das_formular_wird_gelesen(seite, pruefer, typ, status):
    assert seite.formular(f"passwort={RICHTIG}".encode(), typ=typ)[0] == status
    assert pruefer.aufrufe == (1 if status == 303 else 0)


@pytest.mark.parametrize(("laenge", "status", "geprueft"), [
    (256, 401, 1), (257, 413, 0), (4096, 413, 0), (10**9, 413, 0),
])
def test_der_koerper_ist_auf_256_byte_begrenzt(seite, pruefer, laenge, status, geprueft):
    """Genau 256 Byte werden verarbeitet, ein Byte mehr nicht; zu grosse Koerper werden gar nicht erst gelesen (der
    Server antwortet 413, ohne dass der Client den Koerper je senden muss)."""
    kopf = "passwort=" + "x" * 247
    koerper = kopf.encode() if laenge == 256 else b""
    assert seite.formular(koerper, laenge=str(laenge))[0] == status
    assert pruefer.aufrufe == geprueft


@pytest.mark.parametrize("laenge", [None, "", "-1", "abc", "1.5"])
def test_ohne_gueltige_laenge_411(seite, pruefer, laenge):
    kopf = {"Content-Type": "application/x-www-form-urlencoded"}
    if laenge is not None:
        kopf["Content-Length"] = laenge
    assert seite.anfrage("POST", "/anmelden", b"", kopf)[0] == 411
    assert pruefer.aufrufe == 0


@pytest.mark.parametrize(("laenge", "status"), [
    ("9" * 5000, 413), ("0" * 5000 + "1", 413), ("9" * 4301, 413), ("1" * 4, 413), ("0010", 413),
    ("-1", 411), ("abc", 411), ("+10", 411), ("1_0", 411), ("\u00b2", 411),
])
def test_eine_unsinnige_laenge_wird_ohne_ausnahme_beantwortet(seite, pruefer, capsys, laenge, status):
    """Ueber 4300 Ziffern wirft int() seit Python 3.11; ohne Pruefung der Stellenzahl bliebe die Anfrage ohne Antwort
    und jedes Mal stuende ein Traceback im Journal (unangemeldet aus dem LAN). Die Zeichenfolge wird vor dem Rechnen
    geprueft: 411 (keine Ziffernfolge) bzw. 413 (zu gross), nie eine Rechnung, nie ein Fehlversuch."""
    kopf = {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": laenge}
    assert seite.anfrage("POST", "/anmelden", b"", kopf)[0] == status
    assert pruefer.aufrufe == 0
    assert "Traceback" not in capsys.readouterr().err
    assert seite.anmelden(RICHTIG)[0] == 303  # und nichts wurde gezaehlt oder gesperrt


def test_eine_laenge_mit_fuehrendem_leerraum_wird_wie_ohne_gelesen(seite, pruefer):
    kopf = {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": " 10"}
    assert seite.anfrage("POST", "/anmelden", b"passwort=a", kopf)[0] == 401
    assert pruefer.aufrufe == 1


@pytest.mark.parametrize("zeichen", ["cross-site", "same-site", "Cross-Site", " cross-site "])
def test_cross_site_anmeldung_wird_ohne_pruefung_und_ohne_zaehlung_abgewiesen(seite, pruefer, zeichen):
    """M-1/R35: eine fremde Webseite im Browser eines Bewohners darf weder pruefen noch die globale Sperre ausloesen."""
    for _ in range(10):
        status, _, kopf = seite.anmelden("falsch", kopf_zusatz={"Sec-Fetch-Site": zeichen})
        assert status == 403 and "Set-Cookie" not in kopf
    assert pruefer.aufrufe == 0
    assert seite.anmelden(RICHTIG)[0] == 303  # die Drossel ist unveraendert: weder gezaehlt noch gesperrt


@pytest.mark.parametrize("zeichen", ["same-origin", "none"])
def test_gleicher_ursprung_darf_sich_anmelden(seite, pruefer, zeichen):
    assert seite.anmelden(RICHTIG, kopf_zusatz={"Sec-Fetch-Site": zeichen})[0] == 303


@pytest.mark.parametrize("ursprung", [
    "http://evil.example", "https://evil.example:443", "null", "http://smartheat-0001.local", "ftp://127.0.0.1",
    "http://[::1", "http://nutzer@127.0.0.1", "evil.example", "", "http://127.0.0.1.evil.example",
])
def test_fremder_origin_wird_abgewiesen(seite, pruefer, ursprung):
    for _ in range(6):
        assert seite.anmelden("falsch", kopf_zusatz={"Origin": ursprung})[0] == 403, ursprung
    assert pruefer.aufrufe == 0
    assert seite.anmelden(RICHTIG)[0] == 303


def test_ein_zweiter_origin_kopf_kann_den_ersten_nicht_verdecken(seite, pruefer):
    verbindung = http.client.HTTPConnection("127.0.0.1", seite.port, timeout=10)
    try:
        verbindung.putrequest("POST", "/anmelden", skip_accept_encoding=True)
        koerper = f"passwort={RICHTIG}".encode()
        for name, wert in (("Content-Type", "application/x-www-form-urlencoded"),
                           ("Content-Length", str(len(koerper))), ("Origin", f"http://127.0.0.1:{seite.port}"),
                           ("Origin", "http://evil.example")):
            verbindung.putheader(name, wert)
        verbindung.endheaders(koerper)
        assert verbindung.getresponse().status == 403
    finally:
        verbindung.close()
    assert pruefer.aufrufe == 0


@pytest.mark.parametrize(("ursprung", "host"), [
    ("http://127.0.0.1", "127.0.0.1:{port}"), ("http://127.0.0.1:{port}", "127.0.0.1:{port}"),
    ("https://127.0.0.1:{port}", "127.0.0.1"), ("http://localhost", "localhost"), ("http://[::1]:80", "[::1]"),
    ("http://192.168.2.5", "192.168.2.5"), ("http://smartheat-0001", "smartheat-0001"),
    ("http://SMARTHEAT-0001:80", "smartheat-0001."),
])
def test_passender_origin_laeuft_normal(seite, pruefer, ursprung, host):
    """Origin und Host-Header nennen denselben Host (Gross-/Kleinschreibung, Punkt am Ende und Port zaehlen nicht)."""
    ursprung = ursprung.format(port=seite.port)
    host = host.format(port=seite.port)
    assert seite.anmelden("falsch", kopf_zusatz={"Origin": ursprung}, host=host)[0] == 401
    assert seite.anmelden(RICHTIG, kopf_zusatz={"Origin": ursprung}, host=host)[0] == 303
    assert pruefer.aufrufe == 2


@pytest.mark.parametrize(("ursprung", "host"), [
    ("http://203.0.113.5", "192.168.1.50"), ("http://203.0.113.5", "127.0.0.1:{port}"),
    ("http://192.168.2.5", "192.168.1.50"), ("http://localhost", "127.0.0.1:{port}"),
    ("http://smartheat-0001", "localhost"), ("http://[2001:db8::1]", "[::1]"),
])
def test_origin_eines_anderen_hosts_wird_auch_bei_erlaubtem_namen_abgewiesen(seite, pruefer, ursprung, host):
    """A-03: ein IP-Literal oder erlaubter Name besteht die Namensliste, aber eine Seite unter einer fremden Adresse (Safari
    vor 16.4 ohne Sec-Fetch-Site) sendet einen anderen Host als der Host-Header der Anfrage: 403 ohne Zaehlung."""
    for _ in range(6):
        status = seite.anmelden("falsch", kopf_zusatz={"Origin": ursprung.format(port=seite.port)},
                                host=host.format(port=seite.port))[0]
        assert status == 403, (ursprung, host)
    assert pruefer.aufrufe == 0
    assert seite.anmelden(RICHTIG)[0] == 303


def test_ohne_origin_laeuft_der_post_normal(seite, pruefer):
    """curl und aeltere Clients senden keinen Origin."""
    assert seite.anmelden("falsch")[0] == 401 and seite.anmelden(RICHTIG)[0] == 303
    assert pruefer.aufrufe == 2


def test_chunked_wird_abgelehnt(seite, pruefer):
    kopf = {"Content-Type": "application/x-www-form-urlencoded", "Transfer-Encoding": "chunked",
            "Content-Length": "5"}
    assert seite.anfrage("POST", "/anmelden", b"5\r\nabcde\r\n0\r\n\r\n", kopf)[0] == 411
    assert pruefer.aufrufe == 0


@pytest.mark.parametrize("koerper", [b"", b"passwort", b"anderes=1", b"passwort=a&passwort=b", b"passwort=%ff",
                                     b"passwort=a&b=1&c=2&d=3&e=4", b"passwort=a&&"])
def test_ein_ungueltiges_formular_ist_400_und_kein_versuch(seite, pruefer, koerper):
    assert seite.formular(koerper)[0] == 400
    assert pruefer.aufrufe == 0


def test_ein_leeres_passwort_zaehlt_als_fehlversuch(seite, pruefer):
    assert seite.anmelden("")[0] == 401 and pruefer.aufrufe == 1


def test_gesundheitsserver_ohne_sitzung(pruefer):
    """Der Gesundheitsserver (Port 8081, nur Loopback des Hosts) antwortet ohne Sitzung auf /healthz und sonst nichts,
    auch auf POST /anmelden (kein Anmeldeweg dort)."""
    server = start_server(0, lambda: SNAPSHOT, nur_health=True)
    port = server.server_address[1]
    try:
        verbindung = Seite({"port": port})
        status, koerper, kopf = verbindung.anfrage("GET", "/healthz")
        assert status == 200 and json.loads(koerper) == {"server_ok": True, "runtime_status": None,
                                                         "zustand": "nicht_uebernommen"}
        assert "Server" not in kopf
        for pfad in ("/", "/anmelden", "/x"):
            assert verbindung.anfrage("GET", pfad)[:2] == (404, b""), pfad
        assert verbindung.anmelden(RICHTIG)[0] == 501
    finally:
        server.shutdown()
        server.server_close()
    assert pruefer.aufrufe == 0


def test_die_seite_startet_nie_ohne_zugang():
    with pytest.raises(ValueError):
        start_server(0, lambda: SNAPSHOT)


def test_zugang_liest_das_werk_bei_jedem_aufruf(tmp_path):
    pfad = tmp_path / "werk.json"
    zugang = Zugang(pfad)
    assert zugang.werk() is None and zugang.pruefstelle.anmelden(RICHTIG) == "kein_werk"
    pfad.write_text(json.dumps(WERK))
    assert zugang.werk() == WERK and zugang.pruefstelle.anmelden(RICHTIG) == "ok"
