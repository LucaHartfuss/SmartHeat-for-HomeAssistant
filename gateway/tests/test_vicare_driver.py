"""vicare_cloud als PlantBinding (Spec G4 1.2) gegen den Fake-ViCare-Server."""
import json
import threading

import pytest
from fake_vicare.server import GOOD_CODE, FakeVicare

from smartheat_core.binding import VIESSMANN_VICARE_BINDING as BINDING
from smartheat_gateway.drivers.base import DriverError
from smartheat_gateway.drivers.vicare_cloud import oauth
from smartheat_gateway.drivers.vicare_cloud.driver import ViCareCloudDriver
from smartheat_gateway.paths import Paths
from smartheat_gateway.quota import QuotaExhausted

PARAMETER = {"installation_id": 2012345, "gateway_serial": "7637415000000001", "device_id": "0", "heizkreis": 0,
             "poll_seconds": 300.0}
REDIRECT = "https://portal.test/oauth/callback"


@pytest.fixture
def env(tmp_path, clock, monkeypatch):
    server = FakeVicare()
    base = server.start()
    monkeypatch.setenv("SHG_TEST_ENDPOINTS", "1")
    monkeypatch.setenv("SHG_VICARE_IAM_BASE", base)
    monkeypatch.setenv("SHG_VICARE_API_BASE", base)
    paths = Paths(tmp_path)
    driver = ViCareCloudDriver(PARAMETER, paths, clock=clock, writer=True)
    challenge = driver.login_begin({"phase": "begin", "driver_id": "vicare_cloud", "client_id": "c",
                                    "redirect_uri": REDIRECT, "scope": "x"})
    assert set(challenge) == {"code_challenge", "code_challenge_method"} and challenge["code_challenge_method"] == "S256"
    driver.login_finish({"phase": "finish", "driver_id": "vicare_cloud", "code": GOOD_CODE, "redirect_uri": REDIRECT})
    driver.poll_once()
    yield server, driver, paths, clock
    server.stop()


def test_the_driver_uses_the_one_binding_of_the_ha_path(env):
    server, driver, paths, clock = env
    assert dict(driver.description.steps) == dict(BINDING.steps)
    assert driver.description.settle_seconds == 660  # max(2 x 300 + 60, 180)
    assert driver.quota.hard_limit == 1200 and driver.poll_seconds == 300.0


def test_reads_come_from_the_cache_without_a_new_api_call(env):
    server, driver, paths, clock = env
    calls = len(server.calls)
    assert (driver.read("curve"), driver.read("level"), driver.read("room_setpoint")) == (1.4, 0, 20)
    assert driver.read_signal("outdoor_temp") == 5 and len(server.calls) == calls


def test_curve_and_level_are_written_as_one_group_without_reverting_the_first_member(env):
    """Praezisierung 6: der zweite setCurve nimmt die eben geschriebene Steigung, nicht die aus dem alten Cache."""
    server, driver, paths, clock = env
    driver.write("curve", 1.1)
    driver.write("level", 3.0)
    assert [c["params"] for c in server.commands] == [{"shift": 0, "slope": 1.1}, {"shift": 3, "slope": 1.1}]
    assert driver.physical_writes == 2  # die Pipeline zaehlt die Gruppe als einen Schreibvorgang (min(diff, 1))


def test_a_write_group_sends_one_setcurve_with_both_values(env):
    # Audit 4, A4-31 (GW-4): keine Zwischenstellung mit dem alten Partnerwert, ein Aufruf aus dem Kontingent
    server, driver, paths, clock = env
    driver.write_group({"curve": 1.2, "level": 1.0})
    assert [c["params"] for c in server.commands] == [{"shift": 1, "slope": 1.2}] and driver.physical_writes == 1


def test_a_write_group_sets_the_overlay_for_both_members_and_rejects_foreign_levers(env):
    server, driver, paths, clock = env
    server.control({"settle": 10_000.0})  # die Cloud zeigt den neuen Wert lange nicht
    driver.write_group({"curve": 1.15, "level": 2.4})
    driver.poll_once()
    driver.write("room_setpoint", 21.0)
    driver.write_group({"curve": 1.0, "level": 3.0})
    assert [c["params"] for c in server.commands][1:] == [
        {"targetTemperature": 21.0}, {"shift": 3, "slope": 1.0}] and driver.physical_writes == 3
    with pytest.raises(ValueError):
        driver.write_group({"curve": 1.0})
    with pytest.raises(ValueError):
        driver.write_group({"curve": 1.0, "level": 1.0, "room_setpoint": 20.0})
    assert len(server.commands) == 3


def test_a_rejected_write_group_raises_and_counts_nothing(env):
    server, driver, paths, clock = env
    with pytest.raises(RuntimeError):
        driver.write_group({"curve": 9.9, "level": 1.0})  # ausserhalb der Constraints
    assert driver.physical_writes == 0
    driver.write("level", 1.0)
    assert server.commands[-1]["params"] == {"shift": 1, "slope": 1.4}


def test_room_setpoint_is_the_normal_program_temperature(env):
    server, driver, paths, clock = env
    driver.write("room_setpoint", 21.0)
    assert server.commands[-1] == {"feature": "heating.circuits.0.operating.programs.normal",
                                   "command": "setTemperature", "params": {"targetTemperature": 21.0}}


def test_preparation_leaves_comfort_or_eco_and_restore_reactivates_only_that_program(env):
    server, driver, paths, clock = env
    server.control({})
    for feature in server.features["0"]:
        if feature["feature"].endswith("programs.active"):
            feature["properties"]["value"]["value"] = "eco"
        if feature["feature"].endswith("programs.eco"):
            feature["properties"]["active"]["value"] = True
    driver.poll_once()
    assert driver.needs_preparation() and not driver.is_prepared()
    aux = driver.read_aux()
    assert aux == {"mode_select": "eco"}
    assert driver.prepare() is True and driver.prepare() is False
    assert server.commands[-1]["command"] == "deactivate"
    driver.poll_once()
    driver.restore_aux(aux)
    assert server.commands[-1] == {"feature": "heating.circuits.0.operating.programs.eco", "command": "activate", "params": {}}


def test_a_stale_cache_raises_value_error_and_under_an_exhausted_quota_quota_exhausted(env):
    server, driver, paths, clock = env
    clock.advance(3 * 300 + 1)
    with pytest.raises(ValueError):
        driver.read("curve")
    for _ in range(1200 - driver._guard.used()):  # der Abruf der Fixture zaehlt schon mit
        driver._guard.take()
    with pytest.raises(QuotaExhausted):
        driver.read("curve")
    with pytest.raises(QuotaExhausted):
        driver.write("curve", 1.2)


def test_an_expired_login_is_not_logged_in_and_writes_nothing(env):
    """Review Focus 1: kein Schreibvorgang, Datenfehler (Lesen wirft), Portal-Knopf 'Anmeldung erneuern' existiert."""
    server, driver, paths, clock = env
    server.control({"reset": True})  # der Fake kennt die Tokens nicht mehr
    driver.poll_once()               # 401 -> Erneuerung abgelehnt -> die Anmeldung gilt als abgelaufen
    clock.advance(3 * 300 + 1)
    with pytest.raises(DriverError) as error:
        driver.write("curve", 1.2)
    assert error.value.grund == "nicht_angemeldet" and driver.physical_writes == 0 and server.commands == []


def test_a_feature_removed_by_a_firmware_update_makes_read_raise_not_keyerror(env):
    server, driver, paths, clock = env
    server.features["0"] = [f for f in server.features["0"] if not f["feature"].endswith("heating.curve")]
    clock.advance(301)
    driver.poll_once()               # der neue Cache kennt die Rolle nicht mehr, kein altes Ergebnis bleibt stehen
    with pytest.raises(ValueError):
        driver.read("curve")
    assert driver.read("room_setpoint") == 20  # die uebrigen Hebel bleiben lesbar


def test_probe_lists_candidates_and_never_leaks_tokens(env):
    server, driver, paths, clock = env
    result = driver.probe()
    assert result["driver_id"] == "vicare_cloud" and result["kandidaten"][0]["ablehnung"] is None
    text = json.dumps(result)
    assert json.loads((paths.driver_secrets_dir / "vicare.json").read_text())["access_token"] not in text


def test_probe_of_an_unreachable_cloud_and_of_an_expired_login_use_the_contract_reasons(env):
    server, driver, paths, clock = env
    server.control({"offline": True})
    with pytest.raises(DriverError) as offline:
        driver.probe()
    assert offline.value.grund == "anlage_nicht_erreichbar"
    server.control({"reset": True})
    with pytest.raises(DriverError) as expired:
        driver.probe()
    assert expired.value.grund == "nicht_angemeldet"


def test_limits_come_from_the_command_constraints_of_the_plant(env):
    """Praezisierung 13 (Client2-Bereitschaft): dieselben Grenzen, die die Probe in `hebel` meldet; ohne Constraint None."""
    server, driver, paths, clock = env
    assert driver.limits("curve") == (0.2, 3.5)
    assert driver.limits("level") == (-13.0, 40.0)
    assert driver.limits("room_setpoint") == (3.0, 37.0)
    for feature in server.features["0"]:
        if feature["feature"].endswith("heating.curve"):
            feature["commands"]["setCurve"]["params"]["slope"].pop("constraints", None)
    clock.advance(301)
    driver.poll_once()
    assert driver.limits("curve") is None


def test_forget_credentials_removes_the_token_file(env):
    server, driver, paths, clock = env
    driver.forget_credentials()
    assert not (paths.driver_secrets_dir / "vicare.json").exists()


def test_poll_and_reads_from_many_threads_do_not_corrupt_the_cache(env):
    server, driver, paths, clock = env
    errors = []

    def work():
        try:
            for _ in range(20):
                driver.poll_once()
                driver.read("curve")
                driver.read_signal("outdoor_temp")
        except Exception as error:  # pragma: no cover
            errors.append(error)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


# --- Ergaenzungen (Review Focus 1, 3, 7; Anmeldephasen; Ueberlagerung) ---


def test_quota_exhausted_mid_write_group_keeps_the_cloud_value_as_the_one_to_read(env):
    """Review Focus 3: der erste setCurve geht durch, der zweite scheitert an der 429-Sperre; danach schreibt nichts
    mehr, und read() zeigt weiter den Wert der Cloud, nie die halbe Gruppe (kein Zwischenzustand als Ursprungswert)."""
    server, driver, paths, clock = env
    driver.write("curve", 1.1)
    server.control({"rate_limit_after": len(server.calls)})
    with pytest.raises(QuotaExhausted):
        driver.write("level", 3.0)
    assert driver.physical_writes == 1 and len(server.commands) == 1
    assert driver.read("curve") == 1.4  # Cache der Cloud vor der Gruppe, nicht die Ueberlagerung
    calls = len(server.calls)
    with pytest.raises(QuotaExhausted):  # gesperrt bis Retry-After: kein Aufruf mehr
        driver.write("level", 3.0)
    assert len(server.calls) == calls and driver.physical_writes == 1


def test_the_overlay_ends_when_the_cloud_shows_the_value_so_a_later_app_change_is_kept(env):
    server, driver, paths, clock = env
    driver.write("curve", 1.1)
    driver.poll_once()  # die Cloud zeigt 1.1: die Ueberlagerung endet
    for feature in server.features["0"]:
        if feature["feature"].endswith("heating.curve"):
            feature["properties"]["slope"]["value"] = 1.6  # Kunde aendert in der App
    driver.poll_once()
    driver.write("level", 2.0)
    assert server.commands[-1]["params"] == {"shift": 2, "slope": 1.6}


def test_the_overlay_ends_after_the_settle_time_even_if_the_cloud_never_shows_it(env):
    server, driver, paths, clock = env
    server.control({"settle": 10_000.0})  # die Cloud zeigt den neuen Wert lange nicht
    driver.write("curve", 1.1)
    driver.poll_once()
    driver.write("level", 1.0)
    assert server.commands[-1]["params"] == {"shift": 1, "slope": 1.1}
    clock.advance(driver.description.settle_seconds + 1)
    driver.poll_once()
    driver.write("level", 2.0)
    assert server.commands[-1]["params"] == {"shift": 2, "slope": 1.4}


def test_a_rejected_command_raises_and_counts_nothing(env):
    server, driver, paths, clock = env
    with pytest.raises(RuntimeError) as error:
        driver.write("curve", 9.9)  # ausserhalb der Constraints der Anlage
    assert not isinstance(error.value, DriverError) and driver.physical_writes == 0
    driver.write("level", 1.0)
    assert server.commands[-1]["params"] == {"shift": 1, "slope": 1.4}  # keine Ueberlagerung aus dem Fehlversuch


def test_an_unreachable_cloud_on_write_is_a_contract_reason(env):
    server, driver, paths, clock = env
    server.control({"offline": True})
    with pytest.raises(DriverError) as error:
        driver.write("room_setpoint", 21.0)
    assert error.value.grund == "anlage_nicht_erreichbar" and driver.physical_writes == 0


def test_limits_are_none_for_a_stale_cache_and_never_raise(env):
    server, driver, paths, clock = env
    clock.advance(3 * 300 + 1)
    assert driver.limits("curve") is None and driver.limits("room_setpoint") is None


def test_login_phases_map_every_failure_to_a_contract_reason_without_secrets(tmp_path, clock, monkeypatch):
    server = FakeVicare()
    base = server.start()
    try:
        monkeypatch.setenv("SHG_TEST_ENDPOINTS", "1")
        monkeypatch.setenv("SHG_VICARE_IAM_BASE", base)
        monkeypatch.setenv("SHG_VICARE_API_BASE", base)
        driver = ViCareCloudDriver({}, Paths(tmp_path), clock=clock)
        finish = {"phase": "finish", "driver_id": "vicare_cloud", "code": GOOD_CODE, "redirect_uri": REDIRECT}
        with pytest.raises(DriverError) as not_begun:
            driver.login_finish(finish)
        assert not_begun.value.grund == "login_fehlgeschlagen"
        begin = {"phase": "begin", "driver_id": "vicare_cloud", "client_id": "c", "redirect_uri": REDIRECT, "scope": "x"}
        driver.login_begin(begin)
        with pytest.raises(DriverError) as rejected:
            driver.login_finish({**finish, "code": "falsch"})
        assert rejected.value.grund == "login_fehlgeschlagen"
        driver.login_begin(begin)
        monkeypatch.setenv("SHG_VICARE_IAM_BASE", "http://127.0.0.1:9")  # IAM nicht erreichbar
        with pytest.raises(DriverError) as unreachable:
            driver.login_finish(finish)
        assert unreachable.value.grund == "anlage_nicht_erreichbar"
        assert isinstance(unreachable.value.__cause__, oauth.TokenUnavailable)
        pending = json.loads((Paths(tmp_path).driver_secrets_dir / "vicare.json").read_text())["pending"]
        for error in (not_begun.value, rejected.value, unreachable.value):
            assert pending["verifier"] not in error.text and GOOD_CODE not in error.text
        with pytest.raises(DriverError) as missing:
            driver.login_begin({"phase": "begin", "driver_id": "vicare_cloud"})
        assert missing.value.grund == "login_fehlgeschlagen"
    finally:
        server.stop()


class _Installations:
    """ViCareApi-Ersatz: zwei Installationen (Ferienhaus), je ein Heizgeraet, dazu ein Stromzaehler ohne Heizkreis."""

    def __init__(self, features):
        self._features = features
        self.feature_calls = []

    def installations(self):
        return [
            {"installation_id": 1, "gateway_serial": "S1", "device_id": "0", "device_type": "heating", "model_id": "A"},
            {"installation_id": 1, "gateway_serial": "S1", "device_id": "e", "device_type": "electricityEnergySystem",
             "model_id": "Z"},
            {"installation_id": 2, "gateway_serial": "S2", "device_id": "0", "device_type": "heating", "model_id": "B"},
        ]

    def features(self, installation_id, gateway_serial, device_id):
        self.feature_calls.append((installation_id, gateway_serial, device_id))
        return self._features


def test_probe_of_several_installations_gives_one_candidate_per_heating_device_with_unique_ids(tmp_path, clock):
    """Review Focus 7: getrennte Kandidaten mit eindeutiger kandidat_id; ein Geraet ohne Heizung erzeugt keinen."""
    from fake_vicare.server import DEFAULT

    api = _Installations(DEFAULT)
    driver = ViCareCloudDriver({}, Paths(tmp_path), clock=clock, api=api)  # type: ignore[arg-type]
    result = driver.probe()
    ids = [candidate["kandidat_id"] for candidate in result["kandidaten"]]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert [c["parameter"]["installation_id"] for c in result["kandidaten"]] == [1, 2]
    assert ("e" not in {call[2] for call in api.feature_calls})


def test_inventory_sample_has_values_but_no_ids_serials_or_models(env):
    server, driver, paths, clock = env
    sample = driver.inventory_sample()
    assert set(sample) == {"ts", "werte"}
    assert sample["werte"]["curve_current"] == 1.4 and sample["werte"]["outdoor_temp"] == 5
    assert all(isinstance(v, int | float) for v in sample["werte"].values())
    text = json.dumps(sample)
    for secret in ("7637415000000001", "2012345", "Fake"):
        assert secret not in text
    result = driver.inventory(24, [sample])
    assert result == {"driver_id": "vicare_cloud", "stunden": 24, "proben": [sample], "anlage": {}}


def test_inventory_sample_without_parameters_finds_the_heating_device(env):
    server, logged_in, paths, clock = env
    driver = ViCareCloudDriver({}, paths, clock=clock)  # der Agent erzeugt den Treiber ohne Parameter
    assert driver.inventory_sample()["werte"]["level_current"] == 0


# --- Fix-Runde 1: Programmwechsel als Ueberlagerung mit Zeitstempel (nachlaufende Cloud) ---


def _activate_eco(server):
    for feature in server.features["0"]:
        if feature["feature"].endswith("programs.active"):
            feature["properties"]["value"]["value"] = "eco"
        if feature["feature"].endswith("programs.eco"):
            feature["properties"]["active"]["value"] = True


def test_a_lagging_poll_after_prepare_does_not_lose_the_customers_program(env):
    """(a) prepare() -> Abruf zeigt noch eco -> weiter vorbereitet, und restore_aux aktiviert eco wirklich wieder."""
    server, driver, paths, clock = env
    _activate_eco(server)
    driver.poll_once()
    aux = driver.read_aux()
    server.control({"settle": 10_000.0})  # die Cloud zeigt die Deaktivierung lange nicht
    assert driver.prepare() is True
    driver.poll_once()                     # zeigt noch eco
    assert driver.is_prepared() and driver.read_aux() == {"mode_select": "normal"}
    assert driver.prepare() is False and [c["command"] for c in server.commands] == ["deactivate"]
    driver.restore_aux(aux)
    assert server.commands[-1] == {"feature": "heating.circuits.0.operating.programs.eco", "command": "activate", "params": {}}


def test_a_lagging_poll_after_restore_keeps_the_restored_program_until_the_settle_time(env):
    """(b) restore_aux(eco) -> Abruf zeigt noch normal -> read_aux meldet eco; nach der Wartezeit gewinnt die Cloud."""
    server, driver, paths, clock = env
    server.control({"settle": 10_000.0})
    driver.restore_aux({"mode_select": "eco"})
    driver.poll_once()                     # zeigt noch normal
    assert driver.read_aux() == {"mode_select": "eco"} and not driver.is_prepared()
    clock.advance(driver.description.settle_seconds + 1)
    driver.poll_once()
    assert driver.read_aux() == {"mode_select": "normal"}  # Ueberlagerung verfallen, der Abruf gilt


def test_a_confirming_poll_ends_the_program_overlay_so_a_later_app_change_is_seen(env):
    server, driver, paths, clock = env
    driver.restore_aux({"mode_select": "eco"})
    driver.poll_once()                     # die Cloud zeigt eco: bestaetigt
    for feature in server.features["0"]:
        if feature["feature"].endswith("programs.active"):
            feature["properties"]["value"]["value"] = "comfort"  # Kunde waehlt in der App comfort
    driver.poll_once()
    assert driver.read_aux() == {"mode_select": "comfort"}


def test_after_prepare_any_non_foreign_program_of_the_cloud_confirms(env):
    """Nach dem Deaktivieren kann die Cloud z. B. reduced zeigen (Zeitprogramm): das bestaetigt die Vorbereitung."""
    server, driver, paths, clock = env
    _activate_eco(server)
    driver.poll_once()
    server.control({"settle": 10_000.0})
    driver.prepare()
    for feature in server.features["0"]:
        if feature["feature"].endswith("programs.active"):
            feature["properties"]["value"]["value"] = "reduced"
    driver.poll_once()
    assert driver.read_aux() == {"mode_select": "reduced"} and driver.is_prepared()
