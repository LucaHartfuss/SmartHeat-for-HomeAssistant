"""Der Gateway-Host trennt die fachliche von der echten Zeit: Zeitstempel der Nachrichten laufen auf fachlicher Zeit
(mit Zeitraffer-Versatz), was der Host-Dienst auf der Platte mit echter Zeit schreibt (Alter von host/status.json,
Zeitstempel einer Werksreset-Anfrage) wird gegen die echte Zeit gelesen und beantwortet."""
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from fakes import FakeBus

from smartheat_core import dateien
from smartheat_gateway import geraet_host as gh
from smartheat_gateway.paths import Paths
from smartheat_gateway.zigbee import ZigbeeMirror

ECHT = 1_800_000_000.0
VERSATZ = 86_400.0
REPO = Path(__file__).resolve().parents[2]
PYTHONPFAD = os.pathsep.join(str(REPO / pfad) for pfad in ("heizungsbruecke/src", "gateway/src"))


def _iso(epoche: float) -> str:
    return datetime.fromtimestamp(epoche, UTC).isoformat()


def _host(tmp_path, clock, *, wall, echt):
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    host = gh.GatewayDeviceHost(Paths(tmp_path / "data"), ZigbeeMirror(FakeBus(), clock), host_dir=host_dir,
                                clock=clock, wall=wall, echt=echt)
    return host, host_dir


def _hoststatus(host_dir, epoche: float, synchron: bool = True) -> None:
    dateien.schreiben_json(host_dir / "status.json", {"netz": True, "dns": True, "zeit_synchron": synchron,
                                                       "ts": _iso(epoche)}, modus=dateien.LESBAR)


def test_das_alter_des_hoststatus_wird_gegen_die_echte_zeit_gelesen(tmp_path, clock):
    host, host_dir = _host(tmp_path, clock, wall=lambda: ECHT + VERSATZ, echt=lambda: ECHT)
    _hoststatus(host_dir, ECHT)  # der Host-Dienst schreibt echte Zeit
    assert host.uhr_synchron() is True


def test_ein_hoststatus_auf_fachlicher_zeit_gilt_als_veraltet(tmp_path, clock):
    host, host_dir = _host(tmp_path, clock, wall=lambda: ECHT + VERSATZ, echt=lambda: ECHT)
    _hoststatus(host_dir, ECHT + VERSATZ)
    assert host.uhr_synchron() is None


def test_die_antwort_auf_den_werksreset_traegt_die_echte_zeit(tmp_path, clock):
    host, _ = _host(tmp_path, clock, wall=lambda: ECHT + VERSATZ, echt=lambda: ECHT)
    host.paths.data.mkdir(parents=True, exist_ok=True)
    host._reset_antworten((7, _iso(ECHT - 5)), True)
    antwort = json.loads(host.paths.werksreset_bereit.read_text())
    assert antwort["ts"] == _iso(ECHT)
    assert host._reset_beantwortet((7, _iso(ECHT - 5))) is True
    assert host._reset_beantwortet((7, _iso(ECHT + 5))) is False  # eine Anfrage nach der Antwort bleibt offen


def test_zeitstempel_der_nachrichten_bleiben_fachlich(tmp_path, clock):
    host, _ = _host(tmp_path, clock, wall=lambda: ECHT + VERSATZ, echt=lambda: ECHT)
    host.paths.data.mkdir(parents=True, exist_ok=True)
    host._datei.update("regelt")
    assert json.loads(host.paths.agent_state.read_text())["ts"] == _iso(ECHT + VERSATZ)


def test_die_vorgaben_der_gateway_module_folgen_der_zeitquelle_mit_versatz():
    code = (
        "import inspect, time\n"
        "from smartheat_gateway import befehle, geraet_host, quota, zigbee\n"
        "def p(f, name):\n"
        "    return inspect.signature(f).parameters[name].default\n"
        "vor = time.time()\n"
        "werte = [p(geraet_host.GatewayDeviceHost.__init__, 'wall')(), p(geraet_host.GatewayDeviceHost.__init__, 'echt')(),\n"
        "         befehle.Kontext.__dataclass_fields__['wall'].default(), p(zigbee.ZigbeeMirror.__init__, 'wall')(),\n"
        "         p(quota.QuotaGuard.__init__, 'now')()]\n"
        "print(*[w - vor for w in werte])\n"
    )
    basis = {k: v for k, v in os.environ.items() if not k.startswith("SMARTHEAT_")}
    umgebung = {**basis, "PYTHONPATH": PYTHONPFAD, "SMARTHEAT_TESTKENNUNG": "t",
                "SMARTHEAT_ZEITRAFFER_VERSATZ_S": str(int(VERSATZ))}
    ergebnis = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=umgebung)
    assert ergebnis.returncode == 0, ergebnis.stderr
    host_wall, host_echt, kontext_wall, spiegel_wall, quota_jetzt = (float(x) for x in ergebnis.stdout.split())
    for fachlich in (host_wall, kontext_wall):
        assert VERSATZ <= fachlich < VERSATZ + 5
    for echt in (host_echt, spiegel_wall, quota_jetzt):
        assert 0 <= echt < 5


def test_ohne_zeitraffer_sind_alle_vorgaben_die_systemzeit():
    code = (
        "import inspect, time\n"
        "from smartheat_gateway import geraet_host, quota, zigbee\n"
        "vor = time.time()\n"
        "d = inspect.signature(geraet_host.GatewayDeviceHost.__init__).parameters\n"
        "print(d['wall'].default() - vor, d['echt'].default() - vor,\n"
        "      inspect.signature(zigbee.ZigbeeMirror.__init__).parameters['wall'].default() - vor,\n"
        "      inspect.signature(quota.QuotaGuard.__init__).parameters['now'].default() - vor)\n"
    )
    basis = {k: v for k, v in os.environ.items() if not k.startswith("SMARTHEAT_")}
    ergebnis = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                              env={**basis, "PYTHONPATH": PYTHONPFAD})
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert all(0 <= float(x) < 5 for x in ergebnis.stdout.split())
