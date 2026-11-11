"""Der Gerätekern trennt die fachliche von der echten Zeit: Zeitstempel der Nachrichten, die der Server gegen seine
fachliche Zeit liest, folgen `wall`; Signaturzeiten (Bootstrap-Anfragen, Besitznachweis) folgen der echten Zeit. Mit
Zeitraffer liegt die fachliche Zeit um den Versatz vor der echten, ohne Zeitraffer sind beide gleich."""
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests
from device_helpers import WALL, FakeHost, World
from fakes import FakeHa

from heizungsbruecke import geraet_nachweis
from smartheat_device import bootstrap, device, identity, wire

VERSATZ = 86_400.0
REPO = Path(__file__).resolve().parents[2]
PYTHONPFAD = os.pathsep.join(str(REPO / pfad) for pfad in ("heizungsbruecke/src", "gateway/src"))


def _iso(epoche: float) -> str:
    return datetime.fromtimestamp(epoche, UTC).isoformat(timespec="seconds")


def _welt_mit_versatz(tmp_path) -> World:
    welt = World(tmp_path)
    welt.device = device.Device(welt.host, tmp_path, "https://accounts.example.test", clock=welt.clock,
                                wall=lambda: WALL + VERSATZ, echt=lambda: WALL,
                                bootstrap_client=lambda ident: welt.client, link_factory=welt._link)
    return welt


def test_status_und_meldung_tragen_die_fachliche_zeit(tmp_path):
    welt = _welt_mit_versatz(tmp_path)
    welt.verbinden()
    welt.device.meldung("heizung", "Text", offen=True, stufe="gelb")
    welt.device.schritt()
    [meldung] = welt.link.of("up/notifications")[-1]["items"]
    assert welt.link.of("up/status")[-1]["ts"] == _iso(WALL + VERSATZ)
    assert meldung["ts"] == _iso(WALL + VERSATZ)


def test_ohne_versatz_ist_alles_wie_vorher(tmp_path):
    welt = World(tmp_path)
    welt.verbinden()
    assert welt.link.of("up/status")[-1]["ts"] == _iso(WALL)


def test_die_signaturzeit_der_bootstrap_anfragen_ist_echt(tmp_path, monkeypatch):
    """Der Geraetekern baut seinen Bootstrap-Client selbst: die Kopfzeile der Anfrage traegt die echte Zeit."""
    gesendet = []

    def anfrage(self, method, url, **kwargs):
        gesendet.append(kwargs["headers"])
        raise requests.ConnectionError

    monkeypatch.setattr(requests.Session, "request", anfrage)
    geraet = device.Device(FakeHost(), tmp_path, "https://accounts.example.test",
                           wall=lambda: WALL + VERSATZ, echt=lambda: WALL)
    with pytest.raises(bootstrap.Unavailable):
        geraet._client_for(geraet.identity).certificate()
    [kopf] = gesendet
    assert kopf[wire.HEADER_TIMESTAMP] == str(int(WALL))


def test_der_besitznachweis_signiert_die_echte_zeit_wenn_der_aufrufer_sie_gibt(tmp_path):
    ident = identity.load_or_create(tmp_path)
    ha = FakeHa()
    geraet_nachweis.senden(ha, ident, lambda: WALL)
    [(_, ereignis)] = ha.events
    assert ereignis["zeitstempel"] == int(WALL)


def test_die_vorgaben_folgen_der_zeitquelle_mit_versatz():
    code = (
        "import time\n"
        "import heizungsbruecke.__main__ as haupt\n"
        "from heizungsbruecke import geraet_nachweis\n"
        "from smartheat_device import bootstrap, device\n"
        "import inspect\n"
        "def p(f, name):\n"
        "    return inspect.signature(f).parameters[name].default\n"
        "vor = time.time()\n"
        "werte = [p(device.Device.__init__, 'wall')(), p(device.Device.__init__, 'echt')(),\n"
        "         p(bootstrap.BootstrapClient.__init__, 'now')(), p(geraet_nachweis.senden, 'uhr')(),\n"
        "         p(haupt.stdin_anhaengen, 'uhr')()]\n"
        "print(*[w - vor for w in werte])\n"
    )
    basis = {k: v for k, v in os.environ.items() if not k.startswith("SMARTHEAT_")}
    umgebung = {**basis, "PYTHONPATH": PYTHONPFAD, "SMARTHEAT_TESTKENNUNG": "t",
                "SMARTHEAT_ZEITRAFFER_VERSATZ_S": str(int(VERSATZ))}
    ergebnis = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=umgebung)
    assert ergebnis.returncode == 0, ergebnis.stderr
    wall, echt, bootstrap_now, nachweis, stdin = (float(x) for x in ergebnis.stdout.split())
    assert VERSATZ <= wall < VERSATZ + 5
    for echtwert in (echt, bootstrap_now, nachweis, stdin):
        assert 0 <= echtwert < 5


def test_ohne_zeitraffer_sind_alle_vorgaben_die_systemzeit():
    code = (
        "import time\n"
        "import inspect\n"
        "from smartheat_device import device\n"
        "vor = time.time()\n"
        "d = inspect.signature(device.Device.__init__).parameters\n"
        "print(d['wall'].default() - vor, d['echt'].default() - vor)\n"
    )
    basis = {k: v for k, v in os.environ.items() if not k.startswith("SMARTHEAT_")}
    ergebnis = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                              env={**basis, "PYTHONPATH": PYTHONPFAD})
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert all(0 <= float(x) < 5 for x in ergebnis.stdout.split())
