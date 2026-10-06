import json
import stat

from smartheat_host import hoststatus

ROUTE_WITH_DEFAULT = "Iface\tDestination\tGateway\nwlan0\t00000000\t0102A8C0\n"
ROUTE_WITHOUT = "Iface\tDestination\tGateway\nwlan0\t0002A8C0\t00000000\n"


def test_collect_all_good(tmp_path):
    (tmp_path / "route").write_text(ROUTE_WITH_DEFAULT)
    (tmp_path / "synchronized").write_text("")
    data = hoststatus.collect(route_file=tmp_path / "route", resolve=lambda host, port: [("x",)],
                              api_host="api.example.test", sync_flag=tmp_path / "synchronized",
                              timedatectl=lambda: None)
    assert (data["netz"], data["dns"], data["zeit_synchron"]) == (True, True, True)
    assert isinstance(data["ts"], str)


def test_collect_all_bad(tmp_path):
    (tmp_path / "route").write_text(ROUTE_WITHOUT)

    def fail(host, port):
        raise OSError("dns")

    data = hoststatus.collect(route_file=tmp_path / "route", resolve=fail, api_host="api.example.test",
                              sync_flag=tmp_path / "fehlt", timedatectl=lambda: False)
    assert (data["netz"], data["dns"], data["zeit_synchron"]) == (False, False, False)


def test_missing_route_file_means_no_network(tmp_path):
    data = hoststatus.collect(route_file=tmp_path / "fehlt", resolve=lambda h, p: [("x",)], api_host=None,
                              sync_flag=tmp_path / "fehlt", timedatectl=lambda: None)
    assert data["netz"] is False and data["zeit_synchron"] is False


def test_timedatectl_is_the_fallback(tmp_path):
    (tmp_path / "route").write_text(ROUTE_WITH_DEFAULT)
    data = hoststatus.collect(route_file=tmp_path / "route", resolve=lambda h, p: [("x",)], api_host=None,
                              sync_flag=tmp_path / "fehlt", timedatectl=lambda: True)
    assert data["zeit_synchron"] is True and data["dns"] is False  # ohne Host kein DNS-Test


def test_fields_are_the_ones_the_agent_reads(tmp_path):
    from smartheat_gateway.agent.diagnostics import HOST_FIELDS

    data = hoststatus.collect(route_file=tmp_path / "fehlt", resolve=lambda h, p: [], api_host=None,
                              sync_flag=tmp_path / "fehlt", timedatectl=lambda: None)
    assert set(HOST_FIELDS) <= set(data)


def test_write_is_atomic_and_readable(tmp_path):
    hoststatus.write(tmp_path / "host" / "status.json", {"netz": True, "dns": True, "zeit_synchron": True, "ts": "t"})
    assert json.loads((tmp_path / "host" / "status.json").read_text())["netz"] is True
    assert stat.S_IMODE((tmp_path / "host" / "status.json").stat().st_mode) == 0o644  # Agent (uid 1000) muss lesen
    assert not list((tmp_path / "host").glob("*.tmp"))
