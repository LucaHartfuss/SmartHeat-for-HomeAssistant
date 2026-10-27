"""Waechter fuer `runtime` (GA-09, FR-35): drei ungesunde Pruefungen in Folge -> Neustart, hoechstens drei je Stunde, nie
waehrend eines Updater-Auftrags, eines Systemwechsels oder eines Werksresets. `pruefe` ist rein (Zeit und Zustand als
Eingabe); `schritt` verdrahtet es mit Zustandsdatei, Compose-Sperre und Neustart."""
import json
import logging
import subprocess

import pytest

from smartheat_host import bundles, waechter

UNGESUND = {"runtime": "unhealthy", "mosquitto": "healthy"}
GESUND = {"runtime": "healthy", "mosquitto": "healthy"}
LEER: dict = {"unhealthy_folge": 0, "neustarts": [], "aufgegeben": False}


def _durchlauf(zustaende, jetzt=0.0, takt=30.0, pausiert=False):
    """Wendet `pruefe` nacheinander an; Ergebnis: Aktionen (Zustand von Aufruf zu Aufruf weitergereicht)."""
    zustand, aktionen = dict(LEER), []
    for health in zustaende:
        aktion, zustand = waechter.pruefe(health, zustand, jetzt, pausiert)
        aktionen.append(aktion)
        jetzt += takt
    return aktionen, zustand


def test_zweimal_ungesund_nichts_dann_neustart_beim_dritten_mal():
    aktionen, zustand = _durchlauf([UNGESUND] * 3)
    assert aktionen == ["nichts", "nichts", "neustart"]
    assert zustand["neustarts"] == [60.0] and zustand["unhealthy_folge"] == 0


def test_gesund_dazwischen_setzt_die_folge_zurueck():
    aktionen, zustand = _durchlauf([UNGESUND, UNGESUND, GESUND, UNGESUND, UNGESUND])
    assert aktionen == ["nichts"] * 5
    assert zustand["unhealthy_folge"] == 2 and zustand["neustarts"] == []


@pytest.mark.parametrize("health", [{}, {"runtime": "starting"}, {"runtime": ""}, {"mosquitto": "unhealthy"}])
def test_nur_ein_ungesunder_runtime_zaehlt(health):
    """Fehlende Angabe (Container fehlt, Compose antwortet nicht), `starting` und fremde Dienste sind keine Beobachtung eines
    ungesunden `runtime`: die Folge faengt von vorn an."""
    aktionen, zustand = _durchlauf([UNGESUND, UNGESUND, health, UNGESUND, UNGESUND])
    assert aktionen == ["nichts"] * 5 and zustand["unhealthy_folge"] == 2


def test_der_neustart_beginnt_eine_neue_folge():
    """Nach dem Neustart braucht es wieder drei ungesunde Pruefungen (der Container ist `starting`, nicht sofort gesund)."""
    aktionen, _ = _durchlauf([UNGESUND] * 6)
    assert aktionen == ["nichts", "nichts", "neustart", "nichts", "nichts", "neustart"]


def test_der_vierte_neustart_binnen_einer_stunde_wird_aufgegeben_mit_einem_error(caplog):
    aktionen, zustand = _durchlauf([UNGESUND] * 3 * 3)
    assert aktionen.count("neustart") == 3 and aktionen[-1] == "neustart"
    assert zustand["neustarts"] == [60.0, 150.0, 240.0]
    with caplog.at_level(logging.ERROR, logger=waechter.logger.name):
        folgen = []
        for sekunde in (270.0, 300.0, 330.0, 360.0, 390.0):  # dritte ungesunde Pruefung: statt des vierten Neustarts aufgeben
            aktion, zustand = waechter.pruefe(UNGESUND, zustand, sekunde, False)
            folgen.append(aktion)
    assert folgen == ["nichts", "nichts", "aufgegeben", "aufgegeben", "aufgegeben"]
    assert [r.levelno for r in caplog.records] == [logging.ERROR]  # einmal, nicht je Takt
    assert zustand["neustarts"] == [60.0, 150.0, 240.0] and zustand["aufgegeben"] is True  # kein vierter Zeitpunkt


def test_nach_ablauf_der_stunde_ist_ein_neustart_wieder_erlaubt():
    voll = {"unhealthy_folge": 5, "neustarts": [100.0, 200.0, 300.0], "aufgegeben": True}
    aktion, zustand = waechter.pruefe(UNGESUND, voll, 100.0 + 3599.0, False)
    assert aktion == "aufgegeben"  # der erste Zeitpunkt ist noch keine volle Stunde her
    aktion, zustand = waechter.pruefe(UNGESUND, voll, 100.0 + 3600.0, False)
    assert aktion == "neustart" and zustand["neustarts"] == [200.0, 300.0, 3700.0]
    assert zustand["aufgegeben"] is False and zustand["unhealthy_folge"] == 0


def test_ein_neuer_error_nach_erneutem_aufgeben(caplog):
    """Wird `runtime` zwischendurch gesund, beginnt eine neue Lage: ein erneutes Aufgeben meldet wieder einmal."""
    voll = {"unhealthy_folge": 2, "neustarts": [10.0, 20.0, 30.0], "aufgegeben": False}
    with caplog.at_level(logging.ERROR, logger=waechter.logger.name):
        _, zustand = waechter.pruefe(UNGESUND, voll, 40.0, False)
        assert zustand["aufgegeben"] is True
        _, zustand = waechter.pruefe(GESUND, zustand, 70.0, False)
        assert zustand["aufgegeben"] is False
        zustand["unhealthy_folge"] = 2
        waechter.pruefe(UNGESUND, zustand, 100.0, False)
    assert [r.levelno for r in caplog.records] == [logging.ERROR, logging.ERROR]


def test_pausiert_heisst_nichts_und_die_folge_beginnt_neu():
    aktionen, zustand = _durchlauf([UNGESUND] * 2)
    assert aktionen == ["nichts", "nichts"]
    aktion, zustand = waechter.pruefe(UNGESUND, zustand, 100.0, True)
    assert aktion == "nichts" and zustand["unhealthy_folge"] == 0
    aktionen = []
    for sekunde in (130.0, 160.0, 190.0):
        aktion, zustand = waechter.pruefe(UNGESUND, zustand, sekunde, False)
        aktionen.append(aktion)
    assert aktionen == ["nichts", "nichts", "neustart"]  # drei neue Pruefungen nach der Pause, nicht eine


def test_pausiert_vergisst_die_neustarts_der_letzten_stunde_nicht():
    voll = {"unhealthy_folge": 0, "neustarts": [10.0, 20.0, 30.0], "aufgegeben": False}
    _, zustand = waechter.pruefe(UNGESUND, voll, 40.0, True)
    assert zustand["neustarts"] == [10.0, 20.0, 30.0]
    aktion = "nichts"
    for sekunde in (70.0, 100.0, 130.0):
        aktion, zustand = waechter.pruefe(UNGESUND, zustand, sekunde, False)
    assert aktion == "aufgegeben"


def test_pruefe_aendert_den_uebergebenen_zustand_nicht():
    zustand = {"unhealthy_folge": 2, "neustarts": [1.0], "aufgegeben": False}
    kopie = json.loads(json.dumps(zustand))
    waechter.pruefe(UNGESUND, zustand, 5.0, False)
    assert zustand == kopie


# --- Pause aus der Zustandsdatei -------------------------------------------------------------------------------------

@pytest.fixture
def root(tmp_path):
    (tmp_path / "updater").mkdir()
    (tmp_path / "host").mkdir()
    return tmp_path


def _state(root, **felder):
    (root / "updater" / "state.json").write_text(json.dumps({"current": "0.3.0", "in_progress": None, **felder}))


def _system(phase, **mehr):
    return {"system": {"version": "0.9.0", "phase": phase, "slot_alt": "A", **mehr}}


def test_ohne_auftrag_und_ohne_systemwechsel_ist_nicht_pausiert(root):
    _state(root)
    assert waechter.pausiert(root) is False


def test_ein_offener_container_auftrag_pausiert(root):
    _state(root, in_progress={"version": "0.3.1", "previous": "0.3.0", "since": 1.0})
    assert waechter.pausiert(root) is True


@pytest.mark.parametrize("phase", ["installiere", "installiert", "probestart"])
def test_ein_laufender_systemwechsel_pausiert(root, phase):
    _state(root, **_system(phase))
    assert waechter.pausiert(root) is True


@pytest.mark.parametrize("phase,extra", [("bestaetigt", {}), ("zurueck", {"grund": "ungesund", "ablehnen": True})])
def test_nach_bestaetigt_oder_zurueck_ist_nicht_mehr_pausiert(root, phase, extra):
    _state(root, **_system(phase, **extra))
    assert waechter.pausiert(root) is False


def test_ein_werksreset_pausiert(root):
    _state(root)
    (root / "host" / "werksreset.json").write_text("{}")
    assert waechter.pausiert(root) is True


def test_ohne_zustandsdatei_ist_nicht_pausiert(root):
    assert waechter.pausiert(root) is False


# --- Schritt: Zustandsdatei, Sperre, Neustart ------------------------------------------------------------------------

class _Compose:
    """Attrappe des ComposeRunner: Health-Antworten nacheinander, Neustarts und Sperre wie das Original (Datei-flock)."""

    def __init__(self, root, health=UNGESUND, fehler=None):
        self.runner = bundles.ComposeRunner(root, run=self._run)
        self.health_antwort, self.fehler, self.neustarts, self.befehle = health, fehler, [], []

    def _run(self, cmd, **kw):
        self.befehle.append((cmd, kw.get("timeout")))
        if self.fehler is not None:
            raise self.fehler
        return subprocess.CompletedProcess(cmd, 0, json.dumps(
            [{"Service": s, "Health": h} for s, h in self.health_antwort.items()]), "")

    def sperre(self, blockieren=True):
        return self.runner.sperre(blockieren)

    def health(self, version):
        return self.runner.health(version)

    def restart(self, version, dienst):
        self.neustarts.append((version, dienst))
        self.runner.restart(version, dienst)


def test_schritt_startet_nach_drei_ungesunden_pruefungen_die_laufzeit_der_aktuellen_version_neu(root):
    _state(root)
    compose = _Compose(root)
    zustand = dict(LEER)
    for sekunde in (0.0, 30.0, 60.0):
        zustand = waechter.schritt(root, compose, zustand, sekunde)
    assert compose.neustarts == [("0.3.0", "runtime")]
    restart = [b for b in compose.befehle if "restart" in b[0]]
    compose_datei = str(root / "bundles" / "0.3.0" / "docker-compose.yml")
    assert restart == [(["docker", "compose", "-p", "smartheat", "--env-file", "/opt/smartheat/gateway.env", "-f",
                         compose_datei, "restart", "runtime"], 120)]
    assert zustand["neustarts"] == [60.0]


def test_schritt_neustartet_nichts_waehrend_der_updater_die_compose_sperre_haelt(root):
    """Der Updater (`up`) und der Waechter teilen die Sperre: haelt der Updater sie, wird nicht neu gestartet und der
    Versuch nicht gezaehlt - der naechste Takt prueft von neuem."""
    _state(root)
    compose, updater_seite = _Compose(root), bundles.ComposeRunner(root, run=lambda cmd, **kw: None)
    zustand = {"unhealthy_folge": 2, "neustarts": [], "aufgegeben": False}
    with updater_seite.sperre():
        danach = waechter.schritt(root, compose, zustand, 100.0)
    assert compose.neustarts == [] and danach["neustarts"] == []
    assert danach["unhealthy_folge"] == 0  # wie pausiert: nach der Sperre drei neue Beobachtungen
    danach = waechter.schritt(root, compose, {**zustand}, 130.0)  # Sperre frei: jetzt wird neu gestartet
    assert compose.neustarts == [("0.3.0", "runtime")] and danach["neustarts"] == [130.0]


def test_schritt_gibt_die_sperre_nach_dem_neustart_frei(root):
    _state(root)
    compose = _Compose(root)
    waechter.schritt(root, compose, {"unhealthy_folge": 2, "neustarts": [], "aufgegeben": False}, 0.0)
    with bundles.ComposeRunner(root, run=lambda cmd, **kw: None).sperre(blockieren=False) as frei:
        assert frei is True


def test_schritt_liest_die_pause_unter_der_sperre_nochmal(root):
    """Hat der Updater zwischen Pruefung und Sperre einen Auftrag geschrieben, startet der Waechter nicht neu."""
    _state(root)
    compose = _Compose(root)
    echte_sperre = compose.sperre

    def sperre_mit_auftrag(blockieren=True):
        _state(root, in_progress={"version": "0.3.1", "previous": "0.3.0", "since": 1.0})  # Updater war schneller
        return echte_sperre(blockieren)

    compose.sperre = sperre_mit_auftrag
    zustand = waechter.schritt(root, compose, {"unhealthy_folge": 2, "neustarts": [], "aufgegeben": False}, 0.0)
    assert compose.neustarts == [] and zustand["neustarts"] == [] and zustand["unhealthy_folge"] == 0


def test_schritt_pausiert_fragt_docker_gar_nicht(root):
    _state(root, **_system("probestart"))
    compose = _Compose(root)
    zustand = waechter.schritt(root, compose, {"unhealthy_folge": 2, "neustarts": [], "aufgegeben": False}, 0.0)
    assert compose.befehle == [] and zustand["unhealthy_folge"] == 0


def test_schritt_ohne_aktuelle_version_prueft_nichts(root):
    _state(root, current=None)
    compose = _Compose(root)
    zustand = waechter.schritt(root, compose, {"unhealthy_folge": 2, "neustarts": [], "aufgegeben": False}, 0.0)
    assert compose.befehle == [] and zustand["unhealthy_folge"] == 0


def test_ein_gescheiterter_neustart_ist_ein_warning_und_zaehlt_trotzdem(root, caplog):
    """Ein Neustart, der scheitert oder die Frist reisst, darf den Waechter nicht beenden und nicht endlos wiederholt
    werden: er zaehlt zu den drei je Stunde."""
    _state(root)

    class Haengt(_Compose):
        def restart(self, version, dienst):
            self.neustarts.append((version, dienst))
            raise bundles.ComposeError("restart: Frist von 120 s ueberschritten")

    compose = Haengt(root)
    with caplog.at_level(logging.WARNING, logger=waechter.logger.name):
        zustand = waechter.schritt(root, compose, {"unhealthy_folge": 2, "neustarts": [], "aufgegeben": False}, 5.0)
    assert zustand["neustarts"] == [5.0] and compose.neustarts == [("0.3.0", "runtime")]
    assert any(r.levelno == logging.WARNING and "Frist von 120 s" in r.getMessage() for r in caplog.records)


def test_ein_docker_ohne_antwort_gilt_als_keine_beobachtung(root):
    """`compose ps` scheitert (Docker steht): health() liefert {} - nicht ungesund, kein Neustart eines nicht laufenden Docker."""
    _state(root)
    compose = _Compose(root, fehler=OSError("kein docker"))
    zustand = dict(LEER)
    for sekunde in (0.0, 30.0, 60.0, 90.0):
        zustand = waechter.schritt(root, compose, zustand, sekunde)
    assert zustand["neustarts"] == [] and zustand["unhealthy_folge"] == 0
