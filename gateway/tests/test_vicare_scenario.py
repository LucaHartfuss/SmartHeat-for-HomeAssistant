"""Szenario vicare_cloud mit der Gateway-Laufzeit (Plan G4 Task 7, Review Focus 1): die Anmeldung laeuft ab."""
import pytest
from configs import SENSOR, apply_config, write_runtime_files
from fake_vicare.server import GOOD_CODE, FakeVicare
from world import GatewayWorld

from smartheat_gateway.drivers.base import DriverError
from smartheat_gateway.drivers.vicare_cloud.driver import ViCareCloudDriver

VICARE = {"id": "vicare_cloud", "parameter": {"installation_id": 2012345, "gateway_serial": "7637415000000001",
                                               "device_id": "0", "heizkreis": 0, "poll_seconds": 300.0}}


def test_an_expired_login_makes_the_runtime_stop_writing(data_dir, monkeypatch, clock):
    """Token widerrufen: die Laufzeit schreibt nichts mehr; der Fake sieht kein weiteres Kommando."""
    server = FakeVicare()
    base = server.start()
    try:
        for key, value in {"SHG_TEST_ENDPOINTS": "1", "SHG_VICARE_IAM_BASE": base, "SHG_VICARE_API_BASE": base}.items():
            monkeypatch.setenv(key, value)
        world = GatewayWorld(data_dir, monkeypatch, clock)
        login = ViCareCloudDriver(VICARE["parameter"], world.paths, clock=clock)  # meldet an wie der Agent
        login.login_begin({"client_id": "c", "redirect_uri": "r"})
        login.login_finish({"code": GOOD_CODE, "redirect_uri": "r"})
        write_runtime_files(world.paths, apply_config(driver=VICARE))
        world.start_runtime()
        world.connect()
        world.answer({"curve": 1.1, "level": 0.0, "room_setpoint": 21.0})  # Antwort auf den Erststart-Tick
        assert server.commands, "der Server-Sollwert muss bei der Anlage ankommen"
        snapshots = len(world.mqtt[-1].snapshots)
        server.control({"reset": True})  # Tokens des Fake ungueltig; reset leert auch die Liste der Kommandos
        for _ in range(16):  # bis ueber die Tagesauswertung um 12:00; der Raumfuehler meldet weiter
            world.z2m.report(SENSOR, temperature=20.0, battery=90)
            world.advance(900)
        world.advance(60)
        with pytest.raises(DriverError) as raised:  # der Treiber meldet den abgelaufenen Zugang als eigenen Grund
            world.host.driver.read("curve")
        assert raised.value.grund == "nicht_angemeldet"
        status = world.status()
        assert status["status"] == "datenfehler"  # die Laufzeit behandelt es wie jeden Lesefehler: lokaler Datenfehler
        assert status["datenfehler"] == {"art": "lokal", "rollen": ["curve", "level"]}
        assert len(world.mqtt[-1].snapshots) == snapshots  # nichts an den Server: es gibt keine gueltigen Werte
        world.answer({"curve": 1.3, "level": 1.0, "room_setpoint": 22.0})
        assert server.commands == []  # nichts geschrieben, auch nicht nach einer Server-Antwort
        assert "access_token" not in str(status) and "7637415" not in str(status)
    finally:
        server.stop()
