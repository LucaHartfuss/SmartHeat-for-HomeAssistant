"""Werksreset (Regel 4, GP8, GPC9, GW 7.2): Ablauf, Frist, Loeschliste, Unterbrechungen und die Zusage, dass nur der Knopf ihn
ausloest. Compose, Uhr, Neustart und Journal sind Attrappen; die Dateien liegen in einem echten Ordnerbaum."""
import contextlib
import inspect
import json
import logging
import os
import re
import signal
import subprocess
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from smartheat_device import wire

from smartheat_gateway import befehle
from smartheat_host import bundles, firstboot, knopf, updater, werksreset

SERVICE = Path(__file__).resolve().parents[2]  # gateway/
T0 = 1_800_000_000.0
COMPOSE = "services:\n  runtime:\n    image: x@sha256:" + "a" * 64 + "\n"


class FakeCompose:
    """Haelt die Sperre mit und merkt sich, was beim `down` noch auf der Platte lag."""

    def __init__(self, root: Path, fehler: Exception | None = None) -> None:
        self.root, self.fehler = root, fehler
        self.gehalten, self.downs, self.sperren = False, [], 0
        self.daten_beim_down: list[bool] = []
        self.beim_down = lambda: None

    @contextlib.contextmanager
    def sperre(self):
        self.sperren += 1
        self.gehalten = True
        try:
            yield
        finally:
            self.gehalten = False

    def down(self, version):
        assert self.gehalten, "compose down ohne Compose-Sperre"
        self.downs.append(version)
        self.daten_beim_down.append((self.root / "data" / "device").exists())
        self.beim_down()
        if self.fehler is not None:
            raise self.fehler


class Welt:
    """Ein Geraet mit Kundendaten, ein Container (Attrappe) und ein Host, der den Reset ausfuehrt."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "daten"
        bundles.BundleStore(self.root).install("0.3.0", {"docker-compose.yml": COMPOSE.encode(), "mosquitto.conf": b""})
        updater.State(current="0.3.0").save(self.root / "updater" / "state.json")
        self.compose = FakeCompose(self.root)
        self.uhr = [T0]
        self.neustarts: list[int] = []
        self.journal: list[bool] = []  # beim Leeren: waren die Kundendaten schon weg?
        self.fristen: list[float] = []
        self.antwort: dict | None | bool = True  # True/False = ursprungswerte; None = keine Antwort
        self.angefordert: list[dict] = []
        self._kundendaten()

    def _datei(self, rel: str, inhalt: str = "x") -> None:
        pfad = self.root / rel
        pfad.parent.mkdir(parents=True, exist_ok=True)
        pfad.write_text(inhalt)

    def _kundendaten(self) -> None:
        for rel in ("data/device/identitaet.json", "data/device/sicherheitssatz.json", "data/device/zertifikat.pem",
                    "data/runtime/backup.json", "data/secrets/drivers/vicare.json", "data/inventur.json",
                    "data/agent_state.json", "data/quota/vicare.json", "data/sim/zustand.json",
                    "bus/credentials/runtime/bus.json", "bus/credentials/zigbee2mqtt/bus.json",
                    "bus/mosquitto/passwd", "bus/mosquitto-daten/mosquitto.db", "zigbee2mqtt/configuration.yaml",
                    "zigbee2mqtt/coordinator_backup.json", "zigbee2mqtt/log/2027/z2m.log", "host/knopf.json"):
            self._datei(rel)
        for rel in ("host/werk.json", "host/status.json", "host/geladen/" + "a" * 64, "archive/" + "b" * 64 + ".tar",
                    "bundles/0.3.0/docker-compose.yml.bleibt"):
            self._datei(rel, "bleibt")

    def warte(self, bedingung, frist):
        """Der Container antwortet, wenn `antwort` gesetzt ist (wie GatewayDeviceHost, aber ohne Container)."""
        self.fristen.append(frist)
        anfrage = json.loads((self.root / "host" / "werksreset.json").read_text())
        self.angefordert.append(anfrage)
        if isinstance(self.antwort, bool):
            (self.root / "data" / "werksreset_bereit.json").write_text(json.dumps(
                {"seq": anfrage["seq"], "ts": anfrage["ts"], "ursprungswerte": self.antwort}))
        return bedingung()

    def ausfuehren(self, **ersatz):
        def journal_leeren():
            self.journal.append(not (self.root / "data" / "device").exists())

        argumente = dict(warte=self.warte, jetzt=lambda: self.uhr[0], neustart=lambda: self.neustarts.append(1),
                         journal_leeren=journal_leeren)
        argumente.update(ersatz)
        return werksreset.ausfuehren(self.root, self.compose, **argumente)

    def beim_start(self, **ersatz):
        argumente = dict(jetzt=lambda: self.uhr[0], journal_leeren=lambda: None)
        argumente.update(ersatz)
        return werksreset.beim_start(self.root, self.compose, **argumente)

    def anfrage(self, phase: str, seq: int = 1, **mehr) -> None:
        daten = {"seq": seq, "ts": "2027-01-15T08:00:00+00:00", "phase": phase, **mehr}
        (self.root / "host").mkdir(exist_ok=True)
        (self.root / "host" / "werksreset.json").write_text(json.dumps(daten))

    def protokoll(self) -> list[dict]:
        return json.loads((self.root / "host" / "werksreset_protokoll.json").read_text())

    def baum(self) -> set[str]:
        return {str(p.relative_to(self.root)) for p in self.root.rglob("*")}


@pytest.fixture
def welt(tmp_path):
    return Welt(tmp_path)


KUNDENDATEN = ["data/device", "data/runtime", "data/secrets", "data/inventur.json", "data/agent_state.json", "data/quota",
               "data/sim", "bus/credentials/runtime", "bus/credentials/zigbee2mqtt", "bus/mosquitto/passwd",
               "bus/mosquitto-daten/mosquitto.db", "zigbee2mqtt/configuration.yaml", "zigbee2mqtt/coordinator_backup.json",
               "zigbee2mqtt/log", "host/knopf.json"]
BLEIBT = ["host/werk.json", "host/status.json", "host/geladen/" + "a" * 64, "archive/" + "b" * 64 + ".tar",
          "bundles/0.3.0/docker-compose.yml", "bundles/0.3.0/docker-compose.yml.bleibt", "updater/state.json"]


# --- Ablauf und Frist ---

def test_reset_wartet_hoechstens_120s(welt):
    ergebnis = welt.ausfuehren()
    assert welt.fristen == [120.0] and werksreset.WARTE_SEKUNDEN == 120.0
    assert welt.angefordert[0]["phase"] == "angefordert" and set(welt.angefordert[0]) == {"seq", "ts", "phase"}
    assert welt.angefordert[0]["ts"] == "2027-01-15T08:00:00+00:00"
    assert ergebnis["ursprungswerte"] is True and ergebnis["abgeschlossen"] is True


def test_die_antwort_mit_false_wird_protokolliert(welt):
    welt.antwort = False
    assert welt.ausfuehren()["ursprungswerte"] is False
    assert welt.protokoll()[0]["ursprungswerte"] is False and welt.protokoll()[0]["ergebnis"] == "ausgefuehrt"


def test_reset_ohne_antwort_loescht_trotzdem(welt, caplog):
    welt.antwort = None
    with caplog.at_level(logging.WARNING):
        ergebnis = welt.ausfuehren()
    assert ergebnis["ursprungswerte"] is False and ergebnis["abgeschlossen"] is True
    assert not (welt.root / "data" / "device").exists() and welt.neustarts == [1]
    assert welt.protokoll()[0]["ursprungswerte"] is False
    assert "keine Antwort" in caplog.text


@pytest.mark.parametrize("bereit", [
    {"seq": 2, "ts": "2027-01-15T07:00:00+00:00", "ursprungswerte": True},  # andere seq, gleicher Zeitpunkt
    {"seq": 1, "ts": "2000-01-01T00:00:00+00:00", "ursprungswerte": True},  # gleiche seq, vor der Anfrage
    {"seq": 1, "ts": "kein Zeitpunkt", "ursprungswerte": True},
    {"seq": 1, "ts": "2027-01-15T07:00:00", "ursprungswerte": True},  # ohne Zeitzone
    {"seq": True, "ts": "2099-01-01T00:00:00+00:00", "ursprungswerte": True},
    {"seq": 1, "ts": "2099-01-01T00:00:00+00:00", "ursprungswerte": "ja"},
    {"ts": "2099-01-01T00:00:00+00:00", "ursprungswerte": True},
    ["liste"],
], ids=["andere-seq", "frueher", "kaputte-zeit", "ohne-zone", "bool-seq", "text-ursprungswerte", "ohne-seq", "liste"])
def test_alte_bereit_datei_zaehlt_nicht(welt, bereit):
    welt.antwort = None
    (welt.root / "data" / "werksreset_bereit.json").write_text(json.dumps(bereit))
    ergebnis = welt.ausfuehren(warte=lambda bedingung, frist: bedingung())
    assert ergebnis["ursprungswerte"] is False  # die alte Datei sagt true, gilt aber fuer eine andere Anfrage


def test_eine_bereit_datei_derselben_anfrage_zaehlt_auch_mit_spaeterem_zeitpunkt(welt):
    welt.antwort = None

    def warte(bedingung, frist):
        anfrage = json.loads((welt.root / "host" / "werksreset.json").read_text())
        (welt.root / "data" / "werksreset_bereit.json").write_text(json.dumps(
            {"seq": anfrage["seq"], "ts": "2099-01-01T00:00:00+00:00", "ursprungswerte": True}))
        return bedingung()

    assert welt.ausfuehren(warte=warte)["ursprungswerte"] is True


def test_die_seq_waechst_ueber_das_protokoll(welt):
    erste = welt.ausfuehren()["seq"]
    welt._kundendaten()
    zweite = welt.ausfuehren()["seq"]
    assert (erste, zweite) == (1, 2)
    assert [e["seq"] for e in welt.protokoll()] == [1, 2]


def test_das_protokoll_behaelt_die_letzten_zwanzig(welt):
    for _ in range(werksreset.PROTOKOLL_MAX + 3):
        welt.ausfuehren()
    eintraege = welt.protokoll()
    assert len(eintraege) == 20 and eintraege[-1]["seq"] == 23 and eintraege[0]["seq"] == 4


# --- Loeschen ---

def test_reset_behaelt_werk_bundles_updater_und_images(welt):
    welt.ausfuehren()
    baum = welt.baum()
    for rel in BLEIBT:
        assert rel in baum, rel
    assert (welt.root / "host" / "werk.json").read_text() == "bleibt"
    assert json.loads((welt.root / "updater" / "state.json").read_text())["current"] == "0.3.0"
    for ordner in ("data", "bus/credentials", "bus/mosquitto", "bus/mosquitto-daten", "zigbee2mqtt"):
        assert (welt.root / ordner).is_dir() and list((welt.root / ordner).iterdir()) == [], ordner  # die Ordner bleiben, leer


def test_reset_loescht_schluessel_rastung_treiber_inventur_zigbee_bus_journal(welt):
    welt.ausfuehren()
    baum = welt.baum()
    for rel in KUNDENDATEN:
        assert rel not in baum, rel
    for rel in ("data/device/sicherheitssatz.json", "data/device/zertifikat.pem", "data/secrets/drivers/vicare.json",
                "zigbee2mqtt/coordinator_backup.json", "bus/credentials/runtime/bus.json"):
        assert rel not in baum
    assert welt.journal == [True]  # das Journal wird nach den Kundendaten geleert (genau einmal)
    assert welt.compose.downs == ["0.3.0"] and welt.compose.daten_beim_down == [True]  # erst anhalten, dann loeschen
    assert welt.compose.sperren == 1


def test_ein_gescheitertes_leeren_des_journals_bricht_den_reset_nicht_ab(welt, caplog):
    def kaputt():
        raise subprocess.CalledProcessError(1, "journalctl")

    with caplog.at_level(logging.ERROR):
        ergebnis = welt.ausfuehren(journal_leeren=kaputt)
    assert ergebnis["abgeschlossen"] is True and welt.neustarts == [1]
    assert welt.protokoll()[0]["journal"] is False and "Journal nicht geleert" in caplog.text
    assert not (welt.root / "host" / "werksreset.json").exists()


def test_nach_dem_reset_ist_die_anfrage_weg_und_der_neustart_folgt_zuletzt(welt):
    reihenfolge = []

    def neustart():
        reihenfolge.append((welt.root / "host" / "werksreset.json").exists())
        reihenfolge.append("werksreset_protokoll.json" in {p.name for p in (welt.root / "host").iterdir()})

    welt.ausfuehren(neustart=neustart)
    assert reihenfolge == [False, True]  # Anfrage schon geloescht, Protokoll geschrieben


def test_reset_legt_ordner_mit_rechten_neu_an(welt, tmp_path):
    welt.ausfuehren()
    gesetzt = []
    firstboot.ordner_angleichen(welt.root, fchown=lambda fd, uid, gid: gesetzt.append((uid, gid)))
    assert (welt.root / "bus" / "credentials" / "runtime").is_dir()
    assert (welt.root / "bus" / "credentials" / "zigbee2mqtt").is_dir()
    assert oct((welt.root / "bus" / "credentials" / "runtime").stat().st_mode & 0o777) == "0o700"
    assert oct((welt.root / "data").stat().st_mode & 0o777) == "0o755"
    assert oct((welt.root / "zigbee2mqtt").stat().st_mode & 0o777) == "0o700"
    assert (1000, 1000) in gesetzt


def test_der_erststart_gleicht_nach_einem_zu_ende_gefuehrten_reset_die_ordner_an(welt, tmp_path):
    welt.anfrage("loeschen", ursprungswerte=True)
    gesetzt = []
    werkein = tmp_path / "werkseinstellung"
    werkein.mkdir()

    class Start(FakeCompose):
        ups: list = []

        def up(self, version):
            self.ups.append((version, (welt.root / "bus" / "credentials" / "runtime").is_dir()))

    welt.compose = Start(welt.root)
    angleichen = lambda root: firstboot.ordner_angleichen(root, fchown=lambda fd, uid, gid: gesetzt.append((uid, gid)))  # noqa: E731
    assert firstboot.run(welt.root, welt.compose, werkein, journal_leeren=lambda: None, jetzt=lambda: T0,
                         angleichen=angleichen) == 0
    assert welt.compose.ups == [("0.3.0", True)]  # die Bind-Quellen bestehen, bevor `up` sie als root anlegen laesst
    assert welt.compose.downs == ["0.3.0"] and not (welt.root / "host" / "werksreset.json").exists()
    assert (1000, 1000) in gesetzt


@pytest.mark.parametrize("ziel", ["data", "bus/credentials"])
def test_loeschen_folgt_keinem_symlink(welt, tmp_path, ziel):
    fremd = tmp_path / "fremd"
    fremd.mkdir()
    (fremd / "wichtig.txt").write_text("bleibt")
    if ziel == "data":
        os.symlink(fremd, welt.root / "data" / "link")
        os.symlink(fremd / "wichtig.txt", welt.root / "data" / "datei-link")
        os.symlink(fremd, welt.root / "data" / "device" / "tiefer-link")  # auch in Unterordnern
    else:
        credentials = welt.root / "bus" / "credentials"
        for p in sorted(credentials.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        credentials.rmdir()
        os.symlink(fremd, credentials)  # ein Container hat den Ordner durch einen Symlink ersetzt
    welt.ausfuehren()
    assert (fremd / "wichtig.txt").read_text() == "bleibt"
    assert not os.path.lexists(welt.root / ziel / "link") and not os.path.lexists(welt.root / "data" / "datei-link")
    if ziel == "bus/credentials":
        assert not os.path.lexists(welt.root / "bus" / "credentials")  # der Symlink selbst ist weg, ordner_angleichen legt neu an


def test_ein_nicht_loeschbarer_eintrag_laesst_die_phase_stehen_und_meldet_laut(welt, monkeypatch, caplog):
    echt = os.unlink

    def unlink(name, *a, dir_fd=None, **kw):
        if name == "inventur.json":
            raise PermissionError(13, "nein")
        return echt(name, *a, dir_fd=dir_fd, **kw)

    monkeypatch.setattr(werksreset.os, "unlink", unlink)
    with caplog.at_level(logging.ERROR):
        ergebnis = welt.ausfuehren()
    assert ergebnis["abgeschlossen"] is False and welt.neustarts == [1]  # der Neustart holt es nach
    assert json.loads((welt.root / "host" / "werksreset.json").read_text())["phase"] == "loeschen"
    assert not (welt.root / "data" / "device").exists() and (welt.root / "data" / "inventur.json").exists()
    assert "Werksreset: inventur.json nicht geloescht (PermissionError)" in caplog.text


def test_scheitert_compose_down_wird_nichts_geloescht_und_der_neustart_holt_es_nach(welt):
    welt.compose.fehler = bundles.ComposeError("down: Frist")
    ergebnis = welt.ausfuehren()
    assert ergebnis["abgeschlossen"] is False and welt.neustarts == [1]
    assert (welt.root / "data" / "device" / "sicherheitssatz.json").exists()  # nichts geloescht
    assert json.loads((welt.root / "host" / "werksreset.json").read_text())["phase"] == "loeschen"
    # Neustart: Compose laeuft nun (down klappt), der Erststart fuehrt den Reset zu Ende
    welt.compose.fehler = None
    assert welt.beim_start() is True
    assert not (welt.root / "data" / "device").exists() and not (welt.root / "host" / "werksreset.json").exists()
    assert [e["ergebnis"] for e in welt.protokoll()] == ["ausgefuehrt"] and welt.protokoll()[0]["ursprungswerte"] is True


# --- Unterbrechungen ---

def test_strom_weg_nach_anfrage_verwirft_sie(welt, caplog):
    welt.anfrage("angefordert", seq=4)
    with caplog.at_level(logging.WARNING):
        assert welt.beim_start() is False
    assert not (welt.root / "host" / "werksreset.json").exists()
    assert (welt.root / "data" / "device" / "sicherheitssatz.json").exists() and welt.compose.downs == []
    assert welt.protokoll() == [{"ts": werksreset._iso(T0), "ergebnis": "verworfen", "seq": 4,
                                 "ts_anfrage": "2027-01-15T08:00:00+00:00"}]
    assert "verworfen" in caplog.text


def test_strom_weg_beim_loeschen_fuehrt_es_zu_ende(welt):
    welt.anfrage("loeschen", seq=7, ursprungswerte=True)
    for rel in ("data/device", "zigbee2mqtt/log"):  # ein Teil ist schon weg
        for p in sorted((welt.root / rel).rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
    journal = []
    assert welt.beim_start(journal_leeren=lambda: journal.append(1)) is True
    baum = welt.baum()
    for rel in KUNDENDATEN:
        assert rel not in baum, rel
    for rel in BLEIBT:
        assert rel in baum, rel
    assert journal == [1] and welt.compose.downs == ["0.3.0"]
    assert [(e["seq"], e["ergebnis"], e["ursprungswerte"]) for e in welt.protokoll()] == [(7, "ausgefuehrt", True)]
    assert not (welt.root / "host" / "werksreset.json").exists()
    # ein zweiter Start findet nichts mehr zu tun
    assert welt.beim_start() is False and welt.compose.downs == ["0.3.0"]


def test_ein_zweimal_unterbrochener_reset_protokolliert_einmal(welt):
    welt.anfrage("loeschen", seq=7, ursprungswerte=False)
    welt.beim_start()
    welt.anfrage("loeschen", seq=7, ursprungswerte=False)  # Strom weg nach dem Protokoll, vor dem Loeschen der Anfrage
    welt.beim_start()
    assert len(welt.protokoll()) == 1


@pytest.mark.parametrize("inhalt", ["kaputt{", "[]", '{"seq": 1}', '{"seq": 1, "ts": "x", "phase": "angefordert"}',
                                    '{"seq": 1, "ts": "2027-01-15T08:00:00+00:00", "phase": "unbekannt"}',
                                    '{"seq": true, "ts": "2027-01-15T08:00:00+00:00", "phase": "loeschen"}'])
def test_eine_unbrauchbare_anfrage_loest_nichts_aus_und_wird_entfernt(welt, inhalt):
    (welt.root / "host" / "werksreset.json").write_text(inhalt)
    assert welt.beim_start() is False
    assert not (welt.root / "host" / "werksreset.json").exists()
    assert (welt.root / "data" / "device" / "sicherheitssatz.json").exists() and welt.compose.downs == []


def test_ohne_anfrage_tut_der_erststart_nichts(welt):
    assert welt.beim_start() is False
    assert welt.compose.downs == [] and welt.compose.sperren == 0 and not (welt.root / "host" / "werksreset_protokoll.json").exists()


def test_sigterm_beim_warten_verwirft_die_anfrage_und_loescht_nichts(welt):
    def warte(bedingung, frist):
        raise SystemExit(0)  # der Handler des Dienstes macht aus SIGTERM ein SystemExit

    with pytest.raises(SystemExit):
        welt.ausfuehren(warte=warte)
    assert not (welt.root / "host" / "werksreset.json").exists()
    assert (welt.root / "data" / "device" / "sicherheitssatz.json").exists()
    assert welt.neustarts == [] and welt.compose.downs == [] and welt.journal == []
    assert [e["ergebnis"] for e in welt.protokoll()] == ["verworfen"]


def test_sigterm_beim_loeschen_wird_aufgeschoben_bis_alles_getan_ist(welt):
    def terminate(signum, frame):
        raise SystemExit(0)

    def sigterm_beim_down():
        os.kill(os.getpid(), signal.SIGTERM)
        assert (welt.root / "data" / "device").exists()  # der Handler wirft hier nicht, das Loeschen steht noch aus

    welt.compose.beim_down = sigterm_beim_down
    alt = signal.signal(signal.SIGTERM, terminate)
    try:
        with pytest.raises(SystemExit):
            welt.ausfuehren()
        zurueck = signal.getsignal(signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, alt)
    assert zurueck is terminate  # der Handler des Dienstes ist wieder eingesetzt
    baum = welt.baum()
    for rel in KUNDENDATEN:
        assert rel not in baum, rel
    assert welt.neustarts == [1] and welt.journal == [True]  # alles getan, erst danach kam das SystemExit
    assert not (welt.root / "host" / "werksreset.json").exists() and welt.protokoll()[0]["ergebnis"] == "ausgefuehrt"


def test_ohne_sigterm_bleibt_der_handler_unberuehrt(welt):
    def terminate(signum, frame):
        raise SystemExit(0)

    alt = signal.signal(signal.SIGTERM, terminate)
    try:
        welt.ausfuehren()
        assert signal.getsignal(signal.SIGTERM) is terminate
    finally:
        signal.signal(signal.SIGTERM, alt)


# --- Ein neuer Reset bei liegender Datei (A-02, GA-16) ---

def _teil_loeschen(welt) -> None:
    for rel in ("data/device", "zigbee2mqtt/log"):  # ein Teil der Kundendaten ist schon weg
        for p in sorted((welt.root / rel).rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()


def test_ein_neuer_reset_bei_liegender_phase_loeschen_fuehrt_sie_zu_ende_statt_sie_zu_ueberschreiben(welt, monkeypatch):
    """Der halbe Reset (Phase loeschen, Strom weg oder SIGTERM beim Warten) darf nicht mit `angefordert` ueberschrieben
    werden: ein Verwerfen liesse den Stapel auf teilweise geloeschten Daten starten. Kein Warten, keine neue seq."""
    welt.anfrage("loeschen", seq=7, ursprungswerte=True)
    _teil_loeschen(welt)
    geschrieben = []
    echt = werksreset.dateien.schreiben_json
    monkeypatch.setattr(werksreset.dateien, "schreiben_json",
                        lambda pfad, *a, **kw: geschrieben.append(Path(pfad).name) or echt(pfad, *a, **kw))
    ergebnis = welt.ausfuehren(warte=lambda bedingung, frist: pytest.fail("kein Warten bei liegender Phase loeschen"))
    assert "werksreset.json" not in geschrieben  # die liegende Anfrage wird weder ueberschrieben noch neu geschrieben
    assert ergebnis == {"seq": 7, "ts": "2027-01-15T08:00:00+00:00", "ursprungswerte": True, "abgeschlossen": True}
    baum = welt.baum()
    for rel in KUNDENDATEN:
        assert rel not in baum, rel
    for rel in BLEIBT:
        assert rel in baum, rel
    assert welt.compose.downs == ["0.3.0"] and welt.neustarts == [1]
    assert [(e["seq"], e["ergebnis"], e["ursprungswerte"]) for e in welt.protokoll()] == [(7, "ausgefuehrt", True)]
    assert not (welt.root / "host" / "werksreset.json").exists()


def test_ein_neuer_reset_bei_unlesbarer_anfrage_loescht_sofort(welt, monkeypatch, caplog):
    """Ob ein Reset halb getan ist, weiss der Host nicht; die Bestaetigung am Knopf ist der Wille zum Reset. Er schreibt
    nie `angefordert` (das Verwerfen liesse die unlesbare Datei stehen), wartet nicht und loescht gleich."""
    welt.anfrage("loeschen", seq=3, ursprungswerte=True)
    echt = werksreset.lesen.read_bytes

    def lesen(pfad, *a, **kw):
        if Path(pfad).name == "werksreset.json":
            raise PermissionError(13, "nein")
        return echt(pfad, *a, **kw)

    monkeypatch.setattr(werksreset.lesen, "read_bytes", lesen)
    beim_down = []  # die Datei, am Host-Test vorbei gelesen: die neue Anfrage in Phase loeschen steht vor dem Anhalten
    welt.compose.beim_down = lambda: beim_down.append(json.loads((welt.root / "host" / "werksreset.json").read_text()))
    with caplog.at_level(logging.WARNING):
        ergebnis = welt.ausfuehren(warte=lambda bedingung, frist: pytest.fail("kein Warten bei unlesbarer Anfrage"))
    assert [(a["phase"], a["seq"], a["ursprungswerte"]) for a in beim_down] == [("loeschen", 1, False)]
    assert ergebnis["abgeschlossen"] is True and ergebnis["ursprungswerte"] is False and ergebnis["seq"] == 1
    baum = welt.baum()
    for rel in KUNDENDATEN:
        assert rel not in baum, rel
    assert welt.compose.downs == ["0.3.0"] and welt.neustarts == [1]
    assert [(e["seq"], e["ergebnis"], e["ursprungswerte"]) for e in welt.protokoll()] == [(1, "ausgefuehrt", False)]
    assert not (welt.root / "host" / "werksreset.json").exists()
    assert "unlesbar" in caplog.text


@pytest.mark.parametrize("inhalt", [None, "kaputt{", '{"seq": 1, "ts": "x", "phase": "angefordert"}'])
def test_ein_neuer_reset_bei_fehlender_oder_unbrauchbarer_anfrage_beginnt_wie_immer(welt, inhalt):
    """Gegenprobe zu den beiden Fallen oben: nur eine liegende Phase `loeschen` oder eine unlesbare Datei springt ueber
    das Warten; fehlt die Datei oder ist sie sicher unbrauchbar, wartet der Reset auf die Ursprungswerte."""
    if inhalt is not None:
        (welt.root / "host").mkdir(exist_ok=True)
        (welt.root / "host" / "werksreset.json").write_text(inhalt)
    assert welt.ausfuehren()["abgeschlossen"] is True
    assert welt.fristen == [werksreset.WARTE_SEKUNDEN] and welt.angefordert[0]["phase"] == "angefordert"


def test_eine_liegende_phase_angefordert_wird_nicht_wie_loeschen_behandelt(welt):
    """Gegenprobe zu `loeschen`: eine gueltige liegende Anfrage in Phase `angefordert` (Reset verworfen, Datei geblieben) springt
    nie ueber das Warten auf die Ursprungswerte; ein neuer Reset beginnt wie immer."""
    welt.anfrage("angefordert", seq=4)
    assert welt.ausfuehren()["abgeschlossen"] is True
    assert welt.fristen == [werksreset.WARTE_SEKUNDEN] and welt.angefordert[0]["phase"] == "angefordert"


def test_die_phase_loeschen_steht_vor_dem_anhalten_in_der_datei(welt):
    """Absicht vor Wirkung: beim `compose down` liegt `host/werksreset.json` schon in der Phase `loeschen` (ein Absturz dort
    fuehrt der Erststart zu Ende, statt die Anfrage zu verwerfen)."""
    phasen = []
    welt.compose.beim_down = lambda: phasen.append(json.loads((welt.root / "host" / "werksreset.json").read_text())["phase"])
    welt.ausfuehren()
    assert phasen == ["loeschen"]


@pytest.mark.parametrize(("antwort", "text"), [(True, "zurueckgesetzt: ja"), (False, "zurueckgesetzt: nein")])
def test_das_ergebnis_steht_im_protokoll_und_im_journal(welt, caplog, antwort, text):
    welt.antwort = antwort
    with caplog.at_level(logging.WARNING):
        welt.ausfuehren()
    assert welt.protokoll()[0]["journal"] is True  # das Journal wurde geleert
    assert text in caplog.text


def test_beginnt_ein_systemwechsel_waehrend_des_wartens_nennt_die_abweisung_die_phase(welt, caplog):
    def warte(bedingung, frist):
        state = updater.State.load(welt.root / "updater" / "state.json")
        state.system = {"phase": "installiere", "version": "0.9.1", "slot_alt": "A"}
        state.save(welt.root / "updater" / "state.json")
        return False

    with caplog.at_level(logging.WARNING), pytest.raises(werksreset.Abgewiesen, match="Phase installiere"):
        welt.ausfuehren(warte=warte)
    assert "Phase installiere" in caplog.text and welt.compose.downs == []


# --- Abweisung ---

@pytest.mark.parametrize("phase", ["installiere", "installiert", "probestart"])
def test_reset_waehrend_systemwechsel_abgewiesen(welt, phase, caplog):
    state = updater.State.load(welt.root / "updater" / "state.json")
    state.system = {"phase": phase, "version": "0.9.1", "slot_alt": "A"}
    state.save(welt.root / "updater" / "state.json")
    assert updater.State.load(welt.root / "updater" / "state.json").system["phase"] == phase  # der Updater liest sie so
    with caplog.at_level(logging.WARNING), pytest.raises(werksreset.Abgewiesen):
        welt.ausfuehren(warte=lambda b, f: pytest.fail("abgewiesen, kein Warten"))
    assert not (welt.root / "host" / "werksreset.json").exists()
    assert (welt.root / "data" / "device" / "sicherheitssatz.json").exists()
    assert welt.compose.downs == [] and welt.neustarts == [] and welt.journal == []
    assert [(e["ergebnis"], e["grund"]) for e in welt.protokoll()] == [("abgewiesen", "systemwechsel")]
    assert phase in caplog.text


@pytest.mark.parametrize("phase", ["bestaetigt", "zurueck"])
def test_andere_phasen_des_systemwechsels_weisen_nicht_ab(welt, phase):
    state = updater.State.load(welt.root / "updater" / "state.json")
    state.system = {"phase": phase, "version": "0.9.1", "slot_alt": "A", "grund": "ungesund", "ablehnen": True}
    state.save(welt.root / "updater" / "state.json")
    assert updater.State.load(welt.root / "updater" / "state.json").system is not None
    assert welt.ausfuehren()["abgeschlossen"] is True


def test_die_abweisung_nutzt_die_lesart_des_waechters():
    assert werksreset.waechter.PAUSE_PHASEN == ("installiere", "installiert", "probestart")


# --- Hilfen mit echter Wirkung (Attrappen fuer Uhr und Prozesse) ---

def test_warten_prueft_im_takt_bis_zur_frist():
    uhr, schlaf = [0.0], []

    def schlafen(s):
        schlaf.append(s)
        uhr[0] += s

    versuche = []
    assert werksreset.warten(lambda: versuche.append(1) or False, 2.5, uhr=lambda: uhr[0], schlaf=schlafen, takt=1.0) is False
    assert schlaf == [1.0, 1.0, 0.5] and len(versuche) == 4  # nie ueber die Frist hinaus


def test_warten_prueft_standardmaessig_jede_sekunde():
    """Der Takt, in dem der Host auf die Antwort des Containers schaut (GPC9: hoechstens 120 s)."""
    assert inspect.signature(werksreset.warten).parameters["takt"].default == 1.0 == werksreset.WARTE_TAKT_SEKUNDEN


def test_warten_endet_sobald_die_bedingung_gilt():
    gilt = iter([False, False, True])
    assert werksreset.warten(lambda: next(gilt), 120.0, uhr=lambda: 0.0, schlaf=lambda s: None) is True


@given(st.floats(min_value=0.0, max_value=300.0), st.floats(min_value=0.1, max_value=5.0))
@example(120.0, 1.0)
def test_warten_ueberschreitet_die_frist_nie(frist, takt):
    uhr = [0.0]

    def schlafen(s):
        uhr[0] += s

    assert werksreset.warten(lambda: False, frist, uhr=lambda: uhr[0], schlaf=schlafen, takt=takt) is False
    assert uhr[0] <= frist + 1e-9


def test_neustart_und_journal_rufen_systemctl_und_journalctl(monkeypatch):
    aufrufe = []
    monkeypatch.setattr(werksreset.subprocess, "run", lambda cmd, **kw: aufrufe.append((cmd, kw["timeout"], kw["check"], kw["capture_output"])))
    werksreset.neustart_systemd()
    werksreset.journal_leeren_systemd()
    assert aufrufe == [(["systemctl", "reboot"], 30.0, True, True), (["journalctl", "--rotate"], 60.0, True, True),
                       (["journalctl", "--vacuum-time=1s"], 60.0, True, True)]


# --- Verdrahtung im Knopf-Dienst ---

def test_der_knopf_dienst_ruft_den_werksreset_mit_der_echten_verdrahtung(monkeypatch, tmp_path):
    gesehen = {}
    monkeypatch.setattr(knopf.werksreset, "ausfuehren", lambda root, compose, **kw: gesehen.update(root=root, compose=compose, **kw))
    knopf.werksreset_fuer(tmp_path)()
    assert gesehen["root"] == tmp_path and isinstance(gesehen["compose"], bundles.ComposeRunner)
    assert gesehen["warte"] is werksreset.warten and gesehen["neustart"] is werksreset.neustart_systemd
    assert gesehen["journal_leeren"] is werksreset.journal_leeren_systemd and gesehen["jetzt"]() > 1_700_000_000


# --- Nur der Knopf ---

def _quellen(unterordner: str):
    for pfad in sorted((SERVICE / unterordner).rglob("*.py")):
        if "tests" not in pfad.parts and "__pycache__" not in pfad.parts:
            yield pfad, pfad.read_text(encoding="utf-8")


def test_reset_nur_ueber_knopf():
    """Keine Stelle im Gateway (Host und Container) ruft `werksreset.ausfuehren` ausser dem Knopf-Dienst und `beim_start`
    ausser dem Erststart; der Container kennt weder Befehl noch Route fuer den Reset."""
    aufrufer_ausfuehren, aufrufer_beim_start = set(), set()
    for ordner in ("host/smartheat_host", "src", "release", "compose"):
        for pfad, text in _quellen(ordner):
            if pfad.name == "werksreset.py":
                continue
            if re.search(r"\bwerksreset\.ausfuehren\b", text):
                aufrufer_ausfuehren.add(pfad.name)
            if re.search(r"\bwerksreset\.beim_start\(", text):
                aufrufer_beim_start.add(pfad.name)
            if re.search(r"\bausfuehren\(", text) and "werksreset" in text and pfad.name not in ("knopf.py",):
                pytest.fail(f"{pfad.name} ruft vermutlich den Werksreset")
    assert aufrufer_ausfuehren == {"knopf.py"} and aufrufer_beim_start == {"firstboot.py"}
    # kein Befehl (Server -> Geraet) und keine Route der LAN-Seite
    assert not [name for name in (*wire.COMMANDS, *befehle.HANDLERS) if "reset" in name]
    for pfad, text in _quellen("src"):
        if pfad.name == "diagnose.py":
            assert "reset" not in text.lower()
        if pfad.name not in ("geraet_host.py", "paths.py", "config.py"):
            # der Container kennt nur Anfrage, Antwort und Vermerk im Geraete-Host (config.py: Abmeldung des Servers uebernimmt
            # den Vermerk)
            assert "werksreset" not in text.lower(), pfad.name


def test_die_uebrigen_host_module_schreiben_keine_anfrage():
    """Die Anfrage `host/werksreset.json` entsteht nur in werksreset.py."""
    for pfad, text in _quellen("host/smartheat_host"):
        if pfad.name not in ("werksreset.py", "waechter.py"):
            assert "werksreset.json" not in text, pfad.name
