"""Plan 5b D2 Task 7: lokaler Kanal des Updaters (Soll-Software aus der Laufzeit, Ergebnisse an die Laufzeit,
Erreichbarkeit des Brokers)."""
import json
import os
import re
import socket
from pathlib import Path

import pytest

from smartheat_host import kanal, lesen

SOFTWARE = {"version": "0.6.1", "manifest_url": "https://x.test/m.json", "manifest_sha256": "a" * 64,
            "signature": "sig"}
REPO = Path(__file__).resolve().parents[3]


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_desired_is_the_software_of_the_last_configuration(tmp_path):
    _write(tmp_path / "software_soll.json", SOFTWARE)
    assert kanal.LokalerKanal(tmp_path).desired() == SOFTWARE


def test_no_software_in_the_configuration_offers_nothing(tmp_path):
    _write(tmp_path / "software_soll.json", None)
    assert kanal.LokalerKanal(tmp_path).desired() == {}


@pytest.mark.parametrize("inhalt", [None, "{kaputt", "[1]"])
def test_missing_or_broken_desired_is_temporary(tmp_path, inhalt):
    if inhalt is not None:
        (tmp_path / "software_soll.json").write_text(inhalt)
    with pytest.raises(kanal.KanalFehler):
        kanal.LokalerKanal(tmp_path).desired()


def test_results_get_increasing_seq_and_are_world_readable(tmp_path):
    k = kanal.LokalerKanal(tmp_path)
    k.update_result("0.6.1", "rollback", "ungesund")
    k.update_result("0.6.2", "ok", "")
    raw = json.loads((tmp_path / "software_meldungen.json").read_text())
    assert [(e["seq"], e["version"], e["result"], e["reason"]) for e in raw["meldungen"]] == [
        (1, "0.6.1", "rollback", "ungesund"), (2, "0.6.2", "ok", "")]
    assert os.stat(tmp_path / "software_meldungen.json").st_mode & 0o777 == 0o644


def test_only_the_last_results_are_kept(tmp_path):
    k = kanal.LokalerKanal(tmp_path)
    for n in range(kanal.MELDUNGEN_MAX + 5):
        k.update_result(f"0.6.{n}", "ok", "")
    meldungen = json.loads((tmp_path / "software_meldungen.json").read_text())["meldungen"]
    assert len(meldungen) == kanal.MELDUNGEN_MAX and meldungen[-1]["seq"] == kanal.MELDUNGEN_MAX + 5


def test_a_lost_results_file_continues_behind_the_acknowledgement(tmp_path):
    _write(tmp_path / "software_quittung.json", {"seq": 7})
    kanal.LokalerKanal(tmp_path).update_result("0.6.1", "ok", "")
    assert json.loads((tmp_path / "software_meldungen.json").read_text())["meldungen"][0]["seq"] == 8


def test_an_unwritable_data_dir_is_a_channel_error(tmp_path):
    blocker = tmp_path / "datei"
    blocker.write_text("x")
    with pytest.raises(kanal.KanalFehler):
        kanal.LokalerKanal(blocker).update_result("0.6.1", "ok", "")


def test_reachable_means_a_tcp_connection_to_the_broker(tmp_path):
    _write(tmp_path / "device" / "endpoint.json", {"host": "broker.test", "port": 8883, "ca_pem": "x", "alpn": None})
    seen = []

    class Sock:
        def close(self):
            seen.append("closed")

    def connect(address, timeout):
        seen.append((address, timeout))
        return Sock()

    assert kanal.LokalerKanal(tmp_path, connect=connect, timeout=3.0).erreichbar()
    assert seen == [(("broker.test", 8883), 3.0), "closed"]


def test_reachable_uses_a_real_tcp_connection_by_default(tmp_path):
    """Ohne Attrappe: ein lauschender Port ist erreichbar, ein geschlossener nicht (Review Focus 5)."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        _write(tmp_path / "device" / "endpoint.json",
               {"host": "127.0.0.1", "port": listener.getsockname()[1], "ca_pem": "x", "alpn": None})
        assert kanal.LokalerKanal(tmp_path, timeout=2.0).erreichbar()
    # Listener geschlossen: derselbe Port nimmt nichts mehr an.
    assert not kanal.LokalerKanal(tmp_path, timeout=2.0).erreichbar()


def test_no_endpoint_or_no_connection_is_unreachable(tmp_path):
    def refused(address, timeout):
        raise socket.gaierror("kein DNS")

    assert not kanal.LokalerKanal(tmp_path, connect=refused).erreichbar()
    _write(tmp_path / "device" / "endpoint.json", {"host": "broker.test", "port": 8883, "ca_pem": "x", "alpn": None})
    assert not kanal.LokalerKanal(tmp_path, connect=refused).erreichbar()


def test_file_names_match_the_device_core_and_the_gateway_paths():
    bootstrap = (REPO / "heizungsbruecke" / "src" / "smartheat_device" / "bootstrap.py").read_text()
    assert re.search(r'^ENDPOINT_FILE = "endpoint\.json"$', bootstrap, re.MULTILINE)
    from smartheat_gateway.paths import Paths

    paths = Paths(Path("/data"))
    assert paths.software_soll.name == kanal.SOLL_FILE
    assert paths.software_meldungen.name == kanal.MELDUNGEN_FILE
    assert paths.software_quittung.name == kanal.QUITTUNG_FILE


# --- Container-schreibbarer Datenordner: Symlinks, FIFOs, riesige Dateien (Root liest und schreibt dort) ---


def test_planted_symlinks_in_the_data_dir_never_reach_a_foreign_file(tmp_path):
    outside = tmp_path / "fremd.txt"
    outside.write_text("unberuehrt")
    data = tmp_path / "data"
    data.mkdir()
    (data / "software_meldungen.json.tmp").symlink_to(outside)
    kanal.LokalerKanal(data).update_result("0.6.1", "ok", "")
    assert outside.read_text() == "unberuehrt"
    (data / "software_meldungen.json").unlink()
    (data / "software_meldungen.json").symlink_to(outside)
    (data / "software_meldungen.json.tmp").symlink_to(outside)
    kanal.LokalerKanal(data).update_result("0.6.2", "ok", "")
    assert outside.read_text() == "unberuehrt"
    assert not (data / "software_meldungen.json").is_symlink()
    assert json.loads((data / "software_meldungen.json").read_text())["meldungen"][0]["version"] == "0.6.2"


def _fifo(path: Path) -> None:
    os.mkfifo(path)


def _symlink(path: Path) -> None:
    target = path.with_name("ziel.json")
    target.write_text(json.dumps(SOFTWARE))
    path.symlink_to(target)


def _huge(path: Path) -> None:
    path.write_text(json.dumps({"version": "0.6.1", "fuell": "x" * 200_000}))


@pytest.mark.parametrize(("groesse", "erlaubt"), [(65535, True), (65536, True), (65537, False)])
def test_read_stat_reads_exactly_64_kib_and_refuses_one_byte_more(tmp_path, groesse, erlaubt):
    """Lesegrenze 65536 Bytes (Standard von `read_stat`): genau darauf und eins darunter wird gelesen, eins darueber
    abgelehnt (`UnsichereDatei`, ein `OSError`); die Datei wird nie teilweise geliefert."""
    datei = tmp_path / "grenze.json"
    datei.write_bytes(b"x" * groesse)
    if erlaubt:
        daten, info = lesen.read_stat(datei)
        assert daten == b"x" * groesse and info.st_size == groesse
    else:
        with pytest.raises(lesen.UnsichereDatei, match="zu gross"):
            lesen.read_stat(datei)


@pytest.mark.parametrize(("groesse", "erlaubt"), [(65536, True), (65537, False)])
def test_read_stat_counts_the_bytes_it_read_when_the_reported_size_is_too_small(tmp_path, monkeypatch, groesse,
                                                                              erlaubt):
    """Meldet `fstat` eine zu kleine Groesse (Datei waechst, Sonderdatei), zaehlt trotzdem, was wirklich gelesen wurde:
    65537 gelesene Bytes werden abgelehnt, 65536 geliefert (Attrappe nur an der Betriebssystem-Grenze `os.fstat`)."""
    datei = tmp_path / "waechst.json"
    datei.write_bytes(b"y" * groesse)
    echtes_fstat = os.fstat

    def zu_klein(fd):
        info = echtes_fstat(fd)
        return os.stat_result((*tuple(info)[:6], 10, *tuple(info)[7:]))  # Index 6 = st_size

    monkeypatch.setattr(lesen.os, "fstat", zu_klein)
    if erlaubt:
        assert lesen.read_stat(datei)[0] == b"y" * groesse
    else:
        with pytest.raises(lesen.UnsichereDatei, match="zu gross"):
            lesen.read_stat(datei)


@pytest.mark.parametrize(("fuell", "erlaubt"), [(65536, True), (65537, False)])
def test_the_desired_file_is_accepted_up_to_exactly_64_kib(tmp_path, fuell, erlaubt):
    """Dieselbe Grenze am Kanal: ein gueltiges Dokument von genau 65536 Bytes ist die Soll-Software, eins mehr ist
    vorlaeufig (`KanalFehler`)."""
    rumpf = json.dumps({"version": "0.6.1", "fuell": ""})
    dokument = json.dumps({"version": "0.6.1", "fuell": "x" * (fuell - len(rumpf))})
    assert len(dokument.encode()) == fuell
    (tmp_path / "software_soll.json").write_text(dokument)
    if erlaubt:
        assert kanal.LokalerKanal(tmp_path).desired()["version"] == "0.6.1"
    else:
        with pytest.raises(kanal.KanalFehler):
            kanal.LokalerKanal(tmp_path).desired()


@pytest.mark.parametrize("plant", [_fifo, _symlink, _huge])
def test_unsafe_desired_file_is_temporary_and_never_blocks(tmp_path, plant):
    plant(tmp_path / "software_soll.json")
    with pytest.raises(kanal.KanalFehler):
        kanal.LokalerKanal(tmp_path).desired()


@pytest.mark.parametrize("plant", [_fifo, _symlink, _huge])
def test_unsafe_results_and_acknowledgement_files_count_as_lost(tmp_path, plant):
    plant(tmp_path / "software_meldungen.json")
    plant(tmp_path / "software_quittung.json")
    kanal.LokalerKanal(tmp_path).update_result("0.6.1", "ok", "")
    raw = json.loads((tmp_path / "software_meldungen.json").read_text())
    assert [e["seq"] for e in raw["meldungen"]] == [1]  # die manipulierte Datei wurde ersetzt, nicht gelesen


@pytest.mark.parametrize("plant", [_fifo, _symlink, _huge])
def test_unsafe_endpoint_file_is_unreachable_without_connecting(tmp_path, plant):
    (tmp_path / "device").mkdir()
    plant(tmp_path / "device" / "endpoint.json")

    def never(address, timeout):
        raise AssertionError("darf nicht verbinden")

    assert not kanal.LokalerKanal(tmp_path, connect=never).erreichbar()


def test_a_symlinked_device_dir_is_unreachable_without_connecting(tmp_path):
    real = tmp_path / "echt"
    _write(real / "endpoint.json", {"host": "broker.test", "port": 8883, "ca_pem": "x", "alpn": None})
    (tmp_path / "device").symlink_to(real)

    def never(address, timeout):
        raise AssertionError("darf nicht verbinden")

    assert not kanal.LokalerKanal(tmp_path, connect=never).erreichbar()


def test_results_are_read_by_the_device_host_exactly_as_written(tmp_path):
    """Vertrag Updater -> Laufzeit: GatewayDeviceHost liest, was LokalerKanal schreibt (seq, version, result, reason)."""
    from test_geraet_host import Welt

    clock = [1000.0]
    welt = Welt(tmp_path, lambda: clock[0])
    k = kanal.LokalerKanal(welt.paths.data)
    k.update_result("0.6.1", "ok", "")
    k.update_result("0.6.2", "rollback", "ungesund")
    k.update_result("0.6.3", "rollback", "server_unerreichbar")
    welt.host._software_ergebnisse()
    assert [(v, ok, grund) for v, ok, grund in welt.device.ergebnisse] == [
        ("0.6.1", True, None), ("0.6.2", False, "ungesund"), ("0.6.3", False, "server_unerreichbar")]
    assert json.loads(welt.paths.software_quittung.read_text()) == {"seq": 3}
