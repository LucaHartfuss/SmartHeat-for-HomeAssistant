"""vicare_cloud als PlantBinding gegen den Fake-ViCare-Server."""
import json
import threading

import pytest
from bindung_beispiele import VIESSMANN_VICARE_BINDING as BINDING
from fake_vicare.server import GOOD_CODE, FakeVicare
from test_vicare_capabilities import GARBAGE, good_features, number
from test_vicare_capabilities import feature as feature_dict

from smartheat_core.pipeline import DeviceWriteError
from smartheat_device import inventory, wire
from smartheat_gateway import safety_warnings
from smartheat_gateway.drivers.base import DriverError
from smartheat_gateway.drivers.vicare_cloud import capabilities as caps
from smartheat_gateway.drivers.vicare_cloud import driver as vicare_driver
from smartheat_gateway.drivers.vicare_cloud import oauth
from smartheat_gateway.drivers.vicare_cloud.driver import ViCareCloudDriver
from smartheat_gateway.paths import Paths
from smartheat_gateway.quota import QuotaExhausted
from smartheat_runtime import delivery

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
    driver = ViCareCloudDriver(PARAMETER, paths, clock=clock, writer=True, description=BINDING)
    challenge = driver.login_begin({"phase": "begin", "driver_id": "vicare_cloud", "client_id": "c",
                                    "redirect_uri": REDIRECT, "scope": "x"})
    assert set(challenge) == {"code_challenge", "code_challenge_method"} and challenge["code_challenge_method"] == "S256"
    driver.login_finish({"phase": "finish", "driver_id": "vicare_cloud", "code": GOOD_CODE, "redirect_uri": REDIRECT})
    driver.poll_once()
    yield server, driver, paths, clock
    server.stop()


def test_parameters_are_checked_by_the_driver(tmp_path):
    # Parameter ausserhalb der Grenzen oder vom falschen Typ werden abgelehnt
    for bad in ({"poll_seconds": 0}, {"poll_seconds": -5}, {"poll_seconds": 60}, {"poll_seconds": True},
                {"poll_seconds": "300"}, {"installation_id": "x"}, {"installation_id": True}, {"heizkreis": -1},
                {"gateway_serial": ""}, {"device_id": 0}):
        with pytest.raises(ValueError):
            ViCareCloudDriver({**PARAMETER, **bad}, Paths(tmp_path))
    ViCareCloudDriver({}, Paths(tmp_path))  # ohne Parameter (Vergessen der Anmeldedaten) bleibt erlaubt
    assert ViCareCloudDriver({**PARAMETER, "poll_seconds": 120}, Paths(tmp_path)).poll_seconds == 120.0


@pytest.mark.parametrize(("schluessel", "wert"), [
    ("installation_id", -1), ("installation_id", 2012345.0), ("installation_id", 1.5), ("installation_id", "2012345"),
    ("heizkreis", -1), ("heizkreis", 0.0), ("heizkreis", 1.5), ("heizkreis", "0"),
    ("device_id", ""), ("device_id", 0), ("gateway_serial", ""), ("gateway_serial", 7637415000000001)])
def test_identity_parameters_of_the_wrong_kind_are_refused_naming_the_field(tmp_path, schluessel, wert):
    with pytest.raises(ValueError, match=schluessel):
        ViCareCloudDriver({**PARAMETER, schluessel: wert}, Paths(tmp_path))


def test_identity_parameters_on_their_limits_are_accepted(tmp_path):
    """Gegenprobe zu den abgelehnten Werten: 0 ist erlaubt (nicht negativ), ein Text mit einem Zeichen auch."""
    driver = ViCareCloudDriver({**PARAMETER, "installation_id": 0, "heizkreis": 0, "device_id": "0",
                                "gateway_serial": "x"}, Paths(tmp_path))
    assert driver.poll_seconds == 300.0


@pytest.mark.parametrize("wert", [float("nan"), float("inf"), float("-inf"), 10**400, -10**400],
                         ids=["nan", "inf", "minus-inf", "gross-positiv", "gross-negativ"])
def test_a_poll_interval_that_is_not_a_finite_number_is_refused_like_any_invalid_parameter(tmp_path, wert):
    with pytest.raises(ValueError):
        ViCareCloudDriver({**PARAMETER, "poll_seconds": wert}, Paths(tmp_path))


# Entscheidung 22: poll_seconds von ViCare liegt zwischen 120 und 86400 s, beide Grenzen eingeschlossen. Dieselben
# Literale prueft test_gateway_erkennung.py auf dem Server und tools/tests/test_treibergrenzen_gleichlauf.py im Root.
@pytest.mark.parametrize(("wert", "angenommen"), [
    (119.999, False), (120, True), (120.0, True), (86400, True), (86400.0, True), (86400.5, False), (86401, False),
    (1e300, False), (float("nan"), False), (float("inf"), False), (float("-inf"), False), (True, False),
    ("300", False), (0, False), (-120, False)])
def test_the_poll_interval_of_vicare_lies_between_120_and_86400_seconds(tmp_path, wert, angenommen):
    if angenommen:
        assert ViCareCloudDriver({**PARAMETER, "poll_seconds": wert}, Paths(tmp_path)).poll_seconds == float(wert)
    else:
        with pytest.raises(ValueError, match="zwischen 120 und 86400 s"):
            ViCareCloudDriver({**PARAMETER, "poll_seconds": wert}, Paths(tmp_path))


def test_the_driver_uses_the_one_binding_of_the_ha_path(env):
    server, driver, paths, clock = env
    assert dict(driver.description.steps) == dict(BINDING.steps)
    assert driver.description.settle_seconds == 180  # die Wartezeit kommt fertig vom Server (wartezeit_s)
    assert vicare_driver.QUOTA.hard_limit == 1200 and driver.poll_seconds == 300.0


def test_reads_come_from_the_cache_without_a_new_api_call(env):
    server, driver, paths, clock = env
    calls = len(server.calls)
    assert (driver.read("curve"), driver.read("level"), driver.read("room_setpoint")) == (1.4, 0, 20)
    assert driver.read_signal("outdoor_temp") == 5 and len(server.calls) == calls


def test_curve_and_level_are_written_as_one_group_without_reverting_the_first_member(env):
    """Der zweite setCurve nimmt die eben geschriebene Steigung, nicht die aus dem alten Cache."""
    server, driver, paths, clock = env
    driver.write("curve", 1.1)
    driver.write("level", 3.0)
    assert [c["params"] for c in server.commands] == [{"shift": 0, "slope": 1.1}, {"shift": 3, "slope": 1.1}]
    assert driver.physical_writes == 2  # ohne write_group zaehlt die Pipeline beide Schreibvorgaenge


def test_a_write_group_sends_one_setcurve_with_both_values(env):
    # Keine Zwischenstellung mit dem alten Partnerwert, ein Aufruf aus dem Kontingent
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
    # Das Overlay der Gruppe stellt beide Partnerwerte (Cloud zeigt noch 1.4 / 0): jeder Einzelschreibvorgang nimmt
    # den Gruppenwert des anderen Hebels, nicht den veralteten Cloud-Wert.
    driver.write("curve", 1.2)
    assert server.commands[-1]["params"] == {"shift": 3, "slope": 1.2}
    driver.write("level", 1.0)
    assert server.commands[-1]["params"] == {"shift": 1, "slope": 1.2}


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
    for _ in range(1200 - len(driver._guard._load()["aufrufe"])):  # der Abruf der Fixture zaehlt schon mit
        driver._guard.take()
    with pytest.raises(QuotaExhausted):
        driver.read("curve")
    with pytest.raises(QuotaExhausted):
        driver.write("curve", 1.2)


@pytest.mark.parametrize(("alter_s", "frisch"), [(899, True), (900, True), (901, False)])
def test_the_cache_is_fresh_up_to_exactly_three_poll_intervals(env, alter_s, frisch):
    """poll_seconds ist 300: der Cache gilt bis 3 x 300 s einschliesslich, eine Sekunde mehr und er ist veraltet."""
    server, driver, paths, clock = env
    clock.advance(alter_s)
    if frisch:
        assert driver.read("curve") is not None
    else:
        with pytest.raises(ValueError, match="veraltet"):
            driver.read("curve")


def test_an_expired_login_is_not_logged_in_and_writes_nothing(env):
    """Kein Schreibvorgang, Datenfehler (Lesen wirft), Portal-Knopf 'Anmeldung erneuern' existiert."""
    server, driver, paths, clock = env
    server.control({"reset": True})  # der Fake kennt die Tokens nicht mehr
    driver.poll_once()               # 401 -> Erneuerung abgelehnt -> die Anmeldung gilt als abgelaufen
    clock.advance(3 * 300 + 1)
    with pytest.raises(DriverError) as error:
        driver.write("curve", 1.2)
    assert error.value.grund == "nicht_angemeldet" and driver.physical_writes == 0 and server.commands == []
    # Lokale Meldung: der Kunde liest einen deutschen Satz, nie den nackten Code.
    schreibfehler = DeviceWriteError("curve", "treiber:curve", error.value)
    text = delivery.notification_text(delivery.NOTIFY_DATENFEHLER_WRITE, (str(schreibfehler),), {})
    assert "Anmeldung" in text and "erneuern" in text and "nicht_angemeldet" not in text


@pytest.mark.parametrize("grund", ["nicht_angemeldet", "anlage_nicht_erreichbar", "login_fehlgeschlagen",
                                   "login_nicht_noetig", "treiber_unbekannt", "erfunden"])
def test_a_driver_error_carries_a_local_text_and_the_code_for_the_server(grund):
    error = DriverError(grund, {"feld": "x"})
    assert error.grund == grund and error.details == {"feld": "x"}
    assert str(error) != grund and " " in str(error)  # ein Satz, nie der Code


def test_a_feature_removed_by_a_firmware_update_makes_read_raise_not_keyerror(env):
    server, driver, paths, clock = env
    server.features["0"] = [f for f in server.features["0"] if not f["feature"].endswith("heating.curve")]
    clock.advance(301)
    driver.poll_once()               # der neue Cache kennt die Rolle nicht mehr, kein altes Ergebnis bleibt stehen
    with pytest.raises(ValueError):
        driver.read("curve")
    assert driver.read("room_setpoint") == 20  # die uebrigen Hebel bleiben lesbar


def test_erkennung_liefert_gekuerzte_features_mit_praefix(env):
    server, driver, paths, clock = env
    daten = driver.erkennung()
    assert tuple(daten) == wire.INVENTUR_GATEWAY_FIELDS and daten["treiber"] == "vicare_cloud"
    assert daten["features"] and all(f["feature"].startswith(caps.PRAEFIXE) for f in daten["features"])
    assert all(set(f) == set(wire.INVENTUR_FEATURE_FIELDS) for f in daten["features"])
    assert all(set(cmd) <= {"isExecutable", "params"} for f in daten["features"] for cmd in f["commands"].values())
    (anlage,) = daten["anlagen"]
    assert tuple(anlage) == wire.INVENTUR_ANLAGE_FIELDS
    assert anlage["kennung"] == {"installation_id": 2012345, "gateway_serial": "7637415000000001", "device_id": "0",
                                 "poll_seconds": 300.0}
    assert {f["anlage"] for f in daten["features"]} == {0}
    text = json.dumps(daten)
    assert "uri" not in text and "http" not in text
    assert json.loads((paths.driver_secrets_dir / "vicare.json").read_text())["access_token"] not in text


def test_erkennung_hat_die_sicherheitswarnungen_je_heizkreis_aus_dem_aktuellen_stand(env):
    server, driver, paths, clock = env
    (anlage,) = driver.erkennung()["anlagen"]
    assert list(anlage["sicherheitswarnungen"]) == ["0"]
    stand = caps.hebelstand(caps.by_name(server.features["0"]), 0)
    assert anlage["sicherheitswarnungen"]["0"] == safety_warnings.compute({"lever_set": "viessmann_vicare", "hebel": stand})


def test_erkennung_warnt_je_heizkreis_wenn_ein_hebel_ausserhalb_der_lokalen_werte_liegt(tmp_path, clock):
    """G-1: die Warnung entsteht im Geraet aus dem aktuellen Stand; ein Heizkreis ohne alle drei Hebel bekommt keine."""
    features = good_features(circuits=(0, 1))
    for roh in features:
        if roh["feature"] == "heating.circuits.1.heating.curve":
            roh["properties"]["slope"]["value"] = 3.4  # ueber den lokalen Grenzen
    features.append(feature_dict("heating.circuits.2.heating.curve", {"shift": number(0), "slope": number(1.4)}))
    driver = ViCareCloudDriver({}, Paths(tmp_path), clock=clock, api=_EinGeraet(features))  # type: ignore[arg-type]
    warnungen = driver.erkennung()["anlagen"][0]["sicherheitswarnungen"]
    assert set(warnungen) == {"0", "1"}  # Kreis 2 hat weder Kommandos noch Normalprogramm
    assert warnungen["0"] != warnungen["1"] and any(warnungen["1"].values())


def test_erkennung_funktioniert_ohne_parameter_und_ohne_beschreibung(env):
    server, logged_in, paths, clock = env
    driver = ViCareCloudDriver({}, paths, clock=clock)  # wie der Befehl inventory stunden 0 vor der Einrichtung
    assert driver.erkennung()["anlagen"][0]["kennung"]["poll_seconds"] == 300.0


def test_erkennung_mit_offline_cloud_und_abgelaufener_anmeldung_nutzt_die_vertragsgruende(env):
    server, driver, paths, clock = env
    server.control({"offline": True})
    with pytest.raises(DriverError) as offline:
        driver.erkennung()
    assert offline.value.grund == "anlage_nicht_erreichbar"
    server.control({"reset": True})
    with pytest.raises(DriverError) as expired:
        driver.erkennung()
    assert expired.value.grund == "nicht_angemeldet"


def test_limits_come_from_the_command_constraints_of_the_plant(env):
    """Dieselben Grenzen, die die Probe in `hebel` meldet; ohne Constraint None."""
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


# --- Ergaenzungen (Anmeldephasen, Ueberlagerung) ---


def test_quota_exhausted_mid_write_group_keeps_the_cloud_value_as_the_one_to_read(env):
    """Der erste setCurve geht durch, der zweite scheitert an der 429-Sperre; danach schreibt nichts
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
            assert pending["verifier"] not in str(error) and GOOD_CODE not in str(error)
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


def test_erkennung_mehrerer_installationen_gibt_eine_anlage_je_heizgeraet(tmp_path, clock):
    """Getrennte Anlagen mit eigener Kennung und eigenem Index; ein Geraet ohne Heizung zaehlt nicht."""
    api = _Installations(good_features())
    driver = ViCareCloudDriver({}, Paths(tmp_path), clock=clock, api=api)  # type: ignore[arg-type]
    daten = driver.erkennung()
    assert [a["kennung"]["installation_id"] for a in daten["anlagen"]] == [1, 2]
    assert {f["anlage"] for f in daten["features"]} == {0, 1}
    assert "e" not in {call[2] for call in api.feature_calls}


class _EinGeraet(_Installations):
    """Wie _Installations, aber ein einziges Heizgeraet mit den Features einer Aufzeichnung."""

    def installations(self):
        return [{"installation_id": 1, "gateway_serial": "S1", "device_id": "0", "device_type": "heating"}]


def test_erkennung_mit_muell_features_wirft_nicht(tmp_path, clock):
    driver = ViCareCloudDriver({}, Paths(tmp_path), clock=clock, api=_EinGeraet(GARBAGE))  # type: ignore[arg-type]
    daten = driver.erkennung()
    assert daten["anlagen"][0]["sicherheitswarnungen"] == {}
    assert all(f["feature"].startswith(caps.PRAEFIXE) for f in daten["features"])


@pytest.mark.parametrize("huge", [10**400, -10**400], ids=["gross-positiv", "gross-negativ"])
def test_erkennung_ueberspringt_nur_den_heizkreis_mit_einem_wert_ausserhalb_des_zahlenbereichs(tmp_path, clock, huge):
    features = good_features(circuits=(0, 1))
    assert features[0]["feature"] == "heating.circuits.0.heating.curve"
    features[0]["properties"]["slope"]["value"] = huge
    driver = ViCareCloudDriver({}, Paths(tmp_path), clock=clock, api=_EinGeraet(features))  # type: ignore[arg-type]
    warnungen = driver.erkennung()["anlagen"][0]["sicherheitswarnungen"]
    assert set(warnungen) == {"1"}  # Heizkreis 0 wie bei einem nichtnumerischen Wert uebersprungen, 1 bleibt


@pytest.mark.parametrize("huge", [10**400, -10**400], ids=["gross-positiv", "gross-negativ"])
def test_poll_keeps_the_other_roles_when_one_value_is_beyond_float_range(tmp_path, clock, huge):
    features = good_features()
    features[0]["properties"]["slope"]["value"] = huge  # Heizkurve des Heizkreises 0
    features[0]["commands"]["setCurve"]["params"]["shift"]["constraints"]["max"] = huge
    driver = ViCareCloudDriver(PARAMETER, Paths(tmp_path), clock=clock, api=_EinGeraet(features))  # type: ignore[arg-type]
    driver.poll_once()
    assert driver.read_signal("outdoor_temp") == 5 and driver.read("room_setpoint") == 20 and driver.read("level") == 0
    with pytest.raises(ValueError):  # die Rolle fehlt wie bei einem nichtnumerischen Wert
        driver.read("curve")
    assert driver.limits("level") is None and driver.limits("curve") is not None


def test_erkennung_einer_grossen_anlage_wird_in_teile_gestueckelt(tmp_path, clock):
    features = good_features(circuits=range(30)) * 6
    driver = ViCareCloudDriver({}, Paths(tmp_path), clock=clock, api=_EinGeraet(features))  # type: ignore[arg-type]
    daten = driver.erkennung()
    assert inventory.groesse(daten) > inventory.LIMIT_BYTES
    teile = inventory.teilen(daten)
    assert len(teile) > 1
    assert [f for teil in teile for f in teil.get("features", [])] == daten["features"]
    assert teile[0]["treiber"] == "vicare_cloud" and teile[0]["anlagen"] == daten["anlagen"]


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
    driver = ViCareCloudDriver({}, paths, clock=clock)  # ohne Parameter: der Treiber sucht das Heizgeraet selbst
    assert driver.inventory_sample()["werte"]["level_current"] == 0


# --- Programmwechsel als Ueberlagerung mit Zeitstempel (nachlaufende Cloud) ---


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


def test_delivery_signals_are_reported_when_the_cloud_has_the_features(env):
    # Betriebsstunden, Starts, Brennerstatus und Betriebsart als Signale
    server, driver, paths, clock = env
    assert not {"generator_hours", "generator_starts", "generator_state", "operating_mode"} & set(driver.signals())
    for feature in server.features["0"]:
        if feature["feature"] == "heating.burners.0":
            feature["properties"] = {"active": {"type": "boolean", "value": True}}
    server.features["0"] += [
        {"feature": "heating.burners.0.statistics", "isEnabled": True, "commands": {},
         "properties": {"hours": {"type": "number", "value": 4321.0}, "starts": {"type": "number", "value": 98765}}},
        {"feature": "heating.circuits.0.operating.modes.active", "isEnabled": True, "commands": {},
         "properties": {"value": {"type": "string", "value": "dhw"}}},
    ]
    driver.poll_once()
    assert {"generator_hours", "generator_starts", "generator_state", "operating_mode"} <= set(driver.signals())
    assert driver.read_signal("operating_mode") == "dhw" and driver.read_signal("generator_state") == "on"
    assert driver.read_signal("generator_hours") == 4321.0 and driver.read_signal("generator_starts") == 98765.0


def test_aux_zulaessig_accepts_only_the_known_programs(env):
    server, driver, paths, clock = env
    for program in ("comfort", "eco", "normal"):
        assert driver.aux_zulaessig("mode_select", program) is True
    assert driver.aux_zulaessig("mode_select", "reduced; drop") is False
    assert driver.aux_zulaessig("setpoint_comfort", "eco") is False
