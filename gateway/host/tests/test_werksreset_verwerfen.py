"""Ein verworfener Werksreset hinterlaesst die Anlage nicht abgemeldet (Ruling R32): Host und Container zusammen, mit dem echten
GatewayDeviceHost. Der Container meldet bei der Anfrage ab (Vermerk), der Host verwirft in vier Faellen; danach hebt der
Container seine Abmeldung auf und startet die Laufzeit neu. Eine Abmeldung des Servers bleibt."""
import contextlib
import json
import logging
from pathlib import Path

import pytest
from geraet_welt import WALL, Welt
from smartheat_core import dateien
from smartheat_device.host import Anzeige
from smartheat_runtime import dokument_config

from smartheat_gateway import geraet_host as gh
from smartheat_gateway.config import abmeldung_vom_server
from smartheat_host import updater, werksreset

ANLAGE = "test-tenant"


class Compose:
    @contextlib.contextmanager
    def sperre(self):
        yield

    def down(self, version):
        pass


class Zusammen:
    """Ein Geraet: Container (GatewayDeviceHost) und Host (werksreset) auf demselben Ordnerbaum."""

    def __init__(self, tmp_path: Path, clock) -> None:
        self.clock, self.root = clock, tmp_path
        self.welt = Welt(tmp_path, clock)
        self.neustarts: list[int] = []
        self.container = self.neuer_container()
        updater.State(current="0.3.0").save(tmp_path / "updater" / "state.json")

    def neuer_container(self) -> gh.GatewayDeviceHost:
        container = gh.GatewayDeviceHost(self.welt.paths, self.welt.mirror, host_dir=self.welt.host_dir, clock=self.clock,
                                         wall=lambda: self.welt.wall["t"], neustart=lambda: self.neustarts.append(1))
        container.verbinden(self.welt.device)
        return container

    def takt(self, container=None) -> None:
        (container or self.container).anzeigen(Anzeige(
            zustand="verbunden", server_ok=True, device_state="uebernommen", eingerichtet=True, hinweise=(), meldungen=(),
            konfiguration_abgelehnt=None))

    def jetzt(self) -> float:
        return WALL

    def anfrage_und_abmeldung(self) -> None:
        """Der Host fordert an, der Container meldet ab (noch keine Antwort)."""
        dateien.schreiben_json(self.root / "host" / "werksreset.json",
                               {"seq": 1, "ts": werksreset._iso(WALL), "phase": "angefordert"}, modus=dateien.LESBAR)
        self.takt()
        assert self.welt.paths.abgemeldet.exists() and self.neustarts == [1]

    def aufgehoben(self) -> bool:
        return (not self.welt.paths.abgemeldet.exists() and not self.welt.paths.werksreset_abmeldung.exists()
                and self.neustarts == [1, 1])


@pytest.fixture
def geraet(tmp_path):
    return Zusammen(tmp_path, lambda: 1000.0)


def test_strom_weg_beim_warten_der_erststart_verwirft_und_der_container_hebt_auf(geraet):
    geraet.anfrage_und_abmeldung()
    # Stromzyklus: der Erststart verwirft vor `up`, danach laeuft der Container (hier derselbe Prozess) weiter
    assert werksreset.beim_start(geraet.root, Compose(), jetzt=geraet.jetzt, journal_leeren=lambda: None) is False
    geraet.takt()
    assert geraet.aufgehoben()


def test_sigterm_beim_warten_der_host_verwirft_und_der_container_hebt_auf(geraet):
    def warte(bedingung, frist):
        geraet.takt()  # der Container meldet ab
        raise SystemExit(0)  # SIGTERM des Knopf-Dienstes

    with pytest.raises(SystemExit):
        werksreset.ausfuehren(geraet.root, Compose(), warte=warte, jetzt=geraet.jetzt,
                              neustart=lambda: pytest.fail("kein Neustart"), journal_leeren=lambda: None)
    assert geraet.welt.paths.abgemeldet.exists()
    geraet.takt()
    assert geraet.aufgehoben()


def test_neustart_des_knopf_dienstes_mit_liegengebliebener_anfrage_verwirft_und_der_container_hebt_auf(geraet, caplog):
    geraet.anfrage_und_abmeldung()
    with caplog.at_level(logging.WARNING):
        assert werksreset.verwerfen_angefordert(geraet.root, geraet.jetzt) is True  # knopf.main beim Start
    geraet.takt()
    assert geraet.aufgehoben() and "Abmeldung der Anfrage aufgehoben" in caplog.text
    assert [e["ergebnis"] for e in json.loads((geraet.root / "host" / "werksreset_protokoll.json").read_text())] == ["verworfen"]


def test_container_startet_vor_dem_verwerfen_und_hebt_erst_danach_auf(geraet):
    geraet.anfrage_und_abmeldung()
    neu = geraet.neuer_container()  # Docker startet den Container, bevor Knopf-Dienst oder Erststart verworfen haben
    for _ in range(3):
        geraet.takt(neu)
    assert geraet.welt.paths.abgemeldet.exists() and geraet.neustarts == [1]
    werksreset.beim_start(geraet.root, Compose(), jetzt=geraet.jetzt, journal_leeren=lambda: None)
    geraet.takt(neu)
    assert geraet.aufgehoben()


def test_server_abmeldung_bleibt_in_allen_faellen(geraet):
    abmeldung_vom_server(geraet.welt.paths)
    dokument_config.abmelden(geraet.welt.paths.abgemeldet, ANLAGE)  # sign_off des Servers, vor der Anfrage
    dateien.schreiben_json(geraet.root / "host" / "werksreset.json",
                           {"seq": 1, "ts": werksreset._iso(WALL), "phase": "angefordert"}, modus=dateien.LESBAR)
    geraet.takt()
    werksreset.verwerfen_angefordert(geraet.root, geraet.jetzt)
    geraet.takt()
    assert geraet.welt.paths.abgemeldet.exists() and geraet.neustarts == []


def test_beginnt_waehrend_des_wartens_ein_systemwechsel_wird_nicht_geloescht_sondern_verworfen(geraet):
    def warte(bedingung, frist):
        geraet.takt()  # der Container meldet ab
        updater.State(current="0.3.0", system={"phase": "installiere", "version": "0.9.1", "slot_alt": "A"}).save(
            geraet.root / "updater" / "state.json")
        return False  # keine Antwort

    (geraet.root / "data" / "device").mkdir(parents=True)
    with pytest.raises(werksreset.Abgewiesen):
        werksreset.ausfuehren(geraet.root, Compose(), warte=warte, jetzt=geraet.jetzt,
                              neustart=lambda: pytest.fail("kein Neustart"), journal_leeren=lambda: pytest.fail("kein Journal"))
    assert (geraet.root / "data" / "device").exists() and not (geraet.root / "host" / "werksreset.json").exists()
    eintrag = json.loads((geraet.root / "host" / "werksreset_protokoll.json").read_text())[0]
    assert (eintrag["ergebnis"], eintrag["grund"]) == ("verworfen", "systemwechsel")
    geraet.takt()
    assert geraet.aufgehoben()


# --- unlesbare Anfrage: nie als „keine Anfrage“ gewertet ---

@pytest.fixture
def unlesbar(monkeypatch):
    echt = werksreset.lesen.read_bytes

    def lesen(pfad, *a, **kw):
        if Path(pfad).name == "werksreset.json":
            raise PermissionError(13, "nein")
        return echt(pfad, *a, **kw)

    monkeypatch.setattr(werksreset.lesen, "read_bytes", lesen)


@pytest.mark.parametrize("phase", ["loeschen", "angefordert"])
def test_eine_vorerst_unlesbare_anfrage_laesst_den_erststart_scheitern_statt_sie_zu_loeschen(geraet, unlesbar, phase, caplog):
    (geraet.root / "data" / "device").mkdir(parents=True)
    dateien.schreiben_json(geraet.root / "host" / "werksreset.json",
                           {"seq": 1, "ts": werksreset._iso(WALL), "phase": phase}, modus=dateien.LESBAR)
    compose = Compose()
    compose.down = lambda version: pytest.fail("nichts anhalten, nichts loeschen")
    with caplog.at_level(logging.ERROR), pytest.raises(werksreset.AnfrageUnlesbar):
        werksreset.beim_start(geraet.root, compose, jetzt=geraet.jetzt, journal_leeren=lambda: None)
    assert (geraet.root / "host" / "werksreset.json").exists() and (geraet.root / "data" / "device").exists()
    assert "nicht lesbar (PermissionError)" in caplog.text and not (geraet.root / "host" / "werksreset_protokoll.json").exists()


def test_verwerfen_loescht_eine_vorerst_unlesbare_anfrage_nicht(geraet, unlesbar):
    dateien.schreiben_json(geraet.root / "host" / "werksreset.json",
                           {"seq": 1, "ts": werksreset._iso(WALL), "phase": "angefordert"}, modus=dateien.LESBAR)
    assert werksreset.verwerfen_angefordert(geraet.root, geraet.jetzt) is False
    assert (geraet.root / "host" / "werksreset.json").exists()


@pytest.mark.parametrize("inhalt", ["kaputt{", "[]", '{"seq": 1}'])
def test_eine_sicher_ungueltige_anfrage_wird_entfernt(geraet, inhalt):
    (geraet.root / "host").mkdir(exist_ok=True)
    (geraet.root / "host" / "werksreset.json").write_text(inhalt)
    assert werksreset.beim_start(geraet.root, Compose(), jetzt=geraet.jetzt, journal_leeren=lambda: None) is False
    assert not (geraet.root / "host" / "werksreset.json").exists()


def test_eine_zu_grosse_anfragedatei_ist_sicher_ungueltig(geraet):
    (geraet.root / "host").mkdir(exist_ok=True)
    (geraet.root / "host" / "werksreset.json").write_text(" " * (werksreset.lesen.MAX_BYTES + 1))
    assert werksreset.beim_start(geraet.root, Compose(), jetzt=geraet.jetzt, journal_leeren=lambda: None) is False
    assert not (geraet.root / "host" / "werksreset.json").exists()


def test_der_erststart_meldet_eine_unlesbare_anfrage_und_startet_nichts(geraet, unlesbar, tmp_path):
    from smartheat_host import firstboot

    class Start(Compose):
        def up(self, version):
            pytest.fail("nichts starten, solange unklar ist, ob ein Reset halb getan ist")

    dateien.schreiben_json(geraet.root / "host" / "werksreset.json",
                           {"seq": 1, "ts": werksreset._iso(WALL), "phase": "loeschen"}, modus=dateien.LESBAR)
    with pytest.raises(OSError):  # firstboot.main faengt OSError: Exit 1, systemd versucht es nach 30 s erneut
        firstboot.run(geraet.root, Start(), tmp_path / "werkein", journal_leeren=lambda: None, jetzt=geraet.jetzt)
