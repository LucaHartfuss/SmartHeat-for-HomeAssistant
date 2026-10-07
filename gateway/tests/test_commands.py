import json

import pytest
from configs import SENSOR, THERMOSTAT
from cryptography import x509
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus

from smartheat_gateway.agent import commands, identity, wire
from smartheat_gateway.agent.commands import Done, Failed, Waiting, execute
from smartheat_gateway.agent.context import AgentContext
from smartheat_gateway.drivers.base import DriverError
from smartheat_gateway.files import write_json
from smartheat_gateway.paths import Paths
from smartheat_gateway.quota import QuotaExhausted
from smartheat_gateway.zigbee import ZigbeeMirror


@pytest.fixture
def ctx(data_dir, clock):
    bus = FakeBus()
    z2m = FakeZigbee2Mqtt(bus)
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    z2m.bridge(online=True)
    z2m.add_sensor(SENSOR)
    z2m.add_thermostat(THERMOSTAT)
    z2m.report(SENSOR, temperature=20.5, battery=80)
    paths = Paths(data_dir)
    context = AgentContext(
        paths, bus, mirror, identity.load_or_create(paths), clock=clock, wall=lambda: 1_700_000_000.0,
    )
    context.start()
    context.z2m = z2m
    return context


def test_unknown_kind_and_bad_payload(ctx):
    assert execute(ctx, "gibtsnicht", {}).grund == "ungueltige_nutzlast"
    assert execute(ctx, "zigbee_permit_join", {"seconds": 999}).grund == "ungueltige_nutzlast"
    assert execute(ctx, "zigbee_permit_join", {"seconds": True}).grund == "ungueltige_nutzlast"
    assert execute(ctx, "diagnostics", []).grund == "ungueltige_nutzlast"


def test_permit_join_and_device_list(ctx):
    assert execute(ctx, "zigbee_permit_join", {"seconds": 120}) == Done({})
    assert ctx.z2m.permit_join_requests == [120]
    devices = execute(ctx, "zigbee_devices", {}).result["devices"]
    sensor = next(d for d in devices if d["ieee"] == SENSOR)
    assert sensor["art"] == "fuehler" and sensor["werte"]["temperature"] == 20.5 and sensor["batterie"] == 80
    assert set(sensor) == set(wire.ZIGBEE_DEVICE_FIELDS)
    assert sensor["last_seen"] == "2023-11-14T22:13:20+00:00"


def test_zigbee_not_ready(ctx):
    ctx.z2m.bridge(online=False)
    assert execute(ctx, "zigbee_devices", {}).grund == "zigbee_nicht_bereit"
    assert execute(ctx, "zigbee_permit_join", {"seconds": 60}).grund == "zigbee_nicht_bereit"
    assert ctx.z2m.permit_join_requests == []


def test_probe_adds_safety_warnings_and_never_writes(ctx):
    result = execute(ctx, "driver_probe", {"driver_id": "simulation"}).result
    assert set(result["kandidaten"][0]["sicherheitswarnungen"]) == {"Heizkoerper", "Fussbodenheizung"}
    assert not (ctx.paths.sim_dir / "plant.json").exists()
    assert execute(ctx, "driver_probe", {"driver_id": "gibtsnicht"}).grund == "treiber_unbekannt"


def test_probe_reports_driver_errors_with_their_reason(ctx):
    ctx.paths.sim_dir.mkdir(parents=True)
    (ctx.paths.sim_dir / "control.json").write_text(json.dumps({"token_expired": True}))
    assert execute(ctx, "driver_probe", {"driver_id": "simulation"}).grund == "nicht_angemeldet"


def test_login_not_needed_for_the_simulation(ctx):
    outcome = execute(ctx, "driver_login", {"phase": "begin", "driver_id": "simulation", "client_id": "x",
                                            "redirect_uri": "https://p.example.test/oauth/callback", "scope": "s"})
    assert outcome.grund == "login_nicht_noetig"
    assert execute(ctx, "driver_login", {"phase": "nonsense", "driver_id": "simulation"}).grund == "ungueltige_nutzlast"


def test_inventory_waits_for_the_window(ctx, clock):
    outcome = execute(ctx, "driver_inventory", {"driver_id": "simulation", "stunden": 1})
    assert isinstance(outcome, Waiting) and outcome.check() is None
    assert outcome.deadline == clock() + 3600 + 3600
    clock.advance(3601)
    done = outcome.check()
    assert isinstance(done, Done) and done.result["stunden"] == 1
    assert execute(ctx, "driver_inventory", {"driver_id": "simulation", "stunden": 49}).grund == "ungueltige_nutzlast"


def test_create_csr_writes_a_private_key_and_returns_only_the_csr(ctx):
    result = execute(ctx, "create_csr", {"tenant_id": "test-tenant"}).result
    csr = x509.load_pem_x509_csr(result["csr"].encode())
    assert csr.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value == "test-tenant"
    assert "PRIVATE KEY" not in result["csr"] and ctx.paths.transport_key.exists()
    assert ctx.paths.transport_key.stat().st_mode & 0o777 == 0o600
    assert execute(ctx, "create_csr", {}).grund == "ungueltige_nutzlast"


def test_create_csr_keeps_the_current_key_when_the_csr_cannot_be_built(ctx):
    ctx.paths.transport_key.parent.mkdir(parents=True, exist_ok=True)
    ctx.paths.transport_key.write_text("test-current-key")
    outcome = execute(ctx, "create_csr", {"tenant_id": "t" * 65})  # CN laenger als 64 Zeichen: ValueError
    assert isinstance(outcome, Failed) and outcome.grund == "intern"
    assert ctx.paths.transport_key.read_text() == "test-current-key"


def test_new_claim_code_only_while_unclaimed(ctx):
    old = ctx.identity.claim_code
    ctx.device_state = "uebernommen"
    assert execute(ctx, "new_claim_code", {}).grund == "bereits_uebernommen"
    ctx.device_state = "nicht_uebernommen"
    assert execute(ctx, "new_claim_code", {}) == Done({})
    assert ctx.identity.claim_code != old and ctx.register_requested


def test_diagnostics_never_contains_secrets(ctx):
    ctx.diagnostics = lambda: {"versionen": {"gateway": "0.1.0"}}
    assert execute(ctx, "diagnostics", {}) == Done({"versionen": {"gateway": "0.1.0"}})


def test_internal_errors_are_reported_not_raised(ctx, monkeypatch):
    monkeypatch.setitem(commands.HANDLERS, "diagnostics", lambda c, p: 1 / 0)
    outcome = execute(ctx, "diagnostics", {})
    assert isinstance(outcome, Failed) and outcome.grund == "intern"
    assert "division" not in outcome.text


def test_quota_exhausted_is_only_reported_for_commands_that_list_it(ctx, monkeypatch):
    def exhausted(context, payload):
        raise QuotaExhausted("Kontingent erschöpft (3/3)")

    for kind in ("driver_probe", "driver_inventory"):
        monkeypatch.setitem(commands.HANDLERS, kind, exhausted)
        assert execute(ctx, kind, {}).grund == "kontingent_erschoepft"
    for kind in ("zigbee_devices", "create_csr", "new_claim_code"):
        monkeypatch.setitem(commands.HANDLERS, kind, exhausted)
        assert execute(ctx, kind, {}).grund == "intern"


def test_reasons_outside_the_command_list_become_internal(ctx, monkeypatch):
    def not_logged_in(context, payload):
        raise DriverError("nicht_angemeldet", "x")

    monkeypatch.setitem(commands.HANDLERS, "driver_login", not_logged_in)  # nicht in der Liste von driver_login
    assert execute(ctx, "driver_login", {}).grund == "intern"
    monkeypatch.setitem(commands.HANDLERS, "driver_probe", not_logged_in)
    assert execute(ctx, "driver_probe", {}).grund == "nicht_angemeldet"


def test_waiting_check_maps_errors_like_the_handler(ctx, clock, monkeypatch):
    from smartheat_gateway.drivers.simulation import SimulationDriver

    outcome = execute(ctx, "driver_inventory", {"driver_id": "simulation", "stunden": 1})

    def unreachable(self, hours, samples):
        raise DriverError("anlage_nicht_erreichbar", "Die Anlage antwortet nicht.")

    monkeypatch.setattr(SimulationDriver, "inventory", unreachable)
    clock.advance(3601)
    assert outcome.check() == Failed("anlage_nicht_erreichbar", "Die Anlage antwortet nicht.")

    def broken(self, hours, samples):
        raise QuotaExhausted("Kontingent erschöpft (3/3)")

    monkeypatch.setattr(SimulationDriver, "inventory", broken)
    assert outcome.check().grund == "kontingent_erschoepft"


def test_every_handler_is_a_contract_command_and_emitted_reasons_are_listed(ctx):
    assert set(commands.HANDLERS) <= set(wire.COMMANDS)
    # Alle Gruende, die diese Handler direkt erzeugen, stehen in der Vertragsliste des jeweiligen Befehls.
    produced = {
        "zigbee_permit_join": {"ungueltige_nutzlast", "zigbee_nicht_bereit"},
        "zigbee_devices": {"zigbee_nicht_bereit"},
        "driver_login": {"ungueltige_nutzlast", "treiber_unbekannt", "login_nicht_noetig"},
        "driver_probe": {"treiber_unbekannt", "nicht_angemeldet", "kontingent_erschoepft"},
        "driver_inventory": {"ungueltige_nutzlast", "treiber_unbekannt", "kontingent_erschoepft"},
        "create_csr": {"ungueltige_nutzlast"},
        "new_claim_code": {"bereits_uebernommen"},
        "diagnostics": set(),
    }
    for kind, reasons in produced.items():
        assert reasons <= set(wire.command_errors(kind)), kind


def server_calls(server) -> int:
    return len(server.calls)


def test_inventory_samples_every_15_minutes_and_returns_the_series(vicare_ctx):
    ctx, server, clock = vicare_ctx
    outcome = commands.execute(ctx, "driver_inventory", {"driver_id": "vicare_cloud", "stunden": 1})
    assert isinstance(outcome, commands.Waiting)
    done = None
    for _ in range(5):  # 5 x 15 min > 1 h
        clock.advance(900)
        ctx.wall = lambda: 1_000_000 + clock()
        done = outcome.check()
        if done is not None:
            break
    assert isinstance(done, commands.Done)
    assert len(done.result["proben"]) == 3  # bei 900, 1800 und 2700 s; bei 3600 s ist das Fenster zu
    assert "Seriennummer" not in str(done.result) and "7637415" not in str(done.result)


def test_a_restart_in_the_middle_resumes_the_series_instead_of_starting_a_second_one(vicare_ctx):
    ctx, server, clock = vicare_ctx
    first = commands.execute(ctx, "driver_inventory", {"driver_id": "vicare_cloud", "stunden": 2})
    clock.advance(900)
    first.check()
    used = server_calls(server)
    second = commands.execute(ctx, "driver_inventory", {"driver_id": "vicare_cloud", "stunden": 2})  # erneute Zustellung
    second.check()
    assert server_calls(server) == used  # nichts doppelt abgerufen
    saved = json.loads(ctx.paths.inventory_samples.read_text())
    assert saved["driver_id"] == "vicare_cloud" and len(saved["samples"]) >= 1


def test_a_series_for_another_driver_or_older_than_its_window_starts_fresh(vicare_ctx):
    ctx, server, clock = vicare_ctx
    write_json(ctx.paths.inventory_samples,
               {"driver_id": "simulation", "started": 0, "hours": 1, "samples": [{}], "next_at": 0})
    outcome = commands.execute(ctx, "driver_inventory", {"driver_id": "vicare_cloud", "stunden": 1})
    outcome.check()
    assert json.loads(ctx.paths.inventory_samples.read_text())["driver_id"] == "vicare_cloud"


def test_the_saved_series_is_private_free_of_ids_and_not_resampled_within_15_minutes(vicare_ctx):
    ctx, server, clock = vicare_ctx
    outcome = commands.execute(ctx, "driver_inventory", {"driver_id": "vicare_cloud", "stunden": 2})
    clock.advance(900)
    outcome.check()
    used = server_calls(server)
    outcome.check()  # erneuter Aufruf derselben Pruefung im selben 15-Minuten-Fenster: keine zweite Probe
    clock.advance(300)
    outcome.check()
    assert server_calls(server) == used
    text = ctx.paths.inventory_samples.read_text()
    assert ctx.paths.inventory_samples.stat().st_mode & 0o777 == 0o600
    for secret in ("7637415", "2012345", "access_token", "refresh_token", "Bearer"):
        assert secret not in text


def test_a_series_that_starts_in_the_future_after_a_backward_clock_jump_starts_fresh(vicare_ctx):
    ctx, server, clock = vicare_ctx
    write_json(ctx.paths.inventory_samples, {
        "driver_id": "vicare_cloud", "started": ctx.wall() + 90_000, "hours": 1, "samples": [{"ts": 1.0}],
        "next_at": ctx.wall() + 90_000,
    })
    commands.execute(ctx, "driver_inventory", {"driver_id": "vicare_cloud", "stunden": 1})
    saved = json.loads(ctx.paths.inventory_samples.read_text())
    assert saved["samples"] == [] and saved["started"] == ctx.wall()


def test_the_simulation_returns_its_series_like_every_driver(ctx, clock):
    outcome = execute(ctx, "driver_inventory", {"driver_id": "simulation", "stunden": 1})
    clock.advance(3601)
    done = outcome.check()
    assert isinstance(done, Done) and isinstance(done.result["proben"], list) and len(done.result["proben"]) == 1


def test_two_live_commands_for_the_same_series_sample_each_boundary_once(vicare_ctx):
    ctx, server, clock = vicare_ctx
    first = commands.execute(ctx, "driver_inventory", {"driver_id": "vicare_cloud", "stunden": 1})
    second = commands.execute(ctx, "driver_inventory", {"driver_id": "vicare_cloud", "stunden": 1})  # neue Command-ID
    clock.advance(900)
    first.check()
    used = server_calls(server)
    second.check()
    assert server_calls(server) == used  # die zweite Pruefung nimmt die Probe der ersten aus der Datei
    assert len(json.loads(ctx.paths.inventory_samples.read_text())["samples"]) == 1
    done = []
    for _ in range(4):
        clock.advance(900)
        ctx.wall = lambda: 1_000_000 + clock()
        done = [first.check(), second.check()]
        if all(d is not None for d in done):
            break
    assert all(isinstance(d, commands.Done) for d in done)
    assert done[0].result["proben"] == done[1].result["proben"] and len(done[0].result["proben"]) == 3


def test_a_pending_inventory_does_not_recreate_its_series_after_the_setup_was_removed(vicare_ctx):
    from smartheat_gateway.agent import lifecycle

    ctx, server, clock = vicare_ctx
    outcome = commands.execute(ctx, "driver_inventory", {"driver_id": "vicare_cloud", "stunden": 2})
    clock.advance(900)
    ctx.wall = lambda: 1_000_000 + clock()
    assert outcome.check() is None and len(json.loads(ctx.paths.inventory_samples.read_text())["samples"]) == 1
    lifecycle.forget_inventory(ctx.paths)  # Abmelden oder Neueinrichtung
    used = server_calls(server)
    clock.advance(900)
    ctx.wall = lambda: 1_000_000 + clock()
    result = outcome.check()
    assert isinstance(result, commands.Failed) and result.grund in wire.command_errors("driver_inventory")
    assert not ctx.paths.inventory_samples.exists() and server_calls(server) == used
    clock.advance(7200)
    ctx.wall = lambda: 1_000_000 + clock()
    assert isinstance(outcome.check(), commands.Failed)  # auch nach Ablauf des Fensters keine alten Proben als Done
    assert not ctx.paths.inventory_samples.exists()


# --- Inventur: eine fehlgeschlagene Probe darf die Reihe nicht beenden ---


def _tick(ctx, clock, outcome, seconds=900):
    clock.advance(seconds)
    ctx.wall = lambda: 1_000_000 + clock()
    return outcome.check()


def _run_to_the_end(ctx, clock, outcome, limit=5):
    """Prueft im 15-Minuten-Takt bis zum Ergebnis; liefert (Ergebnis, Anzahl der Pruefungen)."""
    for ticks in range(1, limit + 1):
        result = _tick(ctx, clock, outcome)
        if result is not None:
            return result, ticks
    return None, limit


def test_a_failed_sample_is_skipped_and_the_inventory_still_finishes_with_the_others(vicare_ctx, caplog):
    ctx, server, clock = vicare_ctx
    outcome = commands.execute(ctx, "driver_inventory", {"driver_id": "vicare_cloud", "stunden": 2})
    assert _tick(ctx, clock, outcome) is None  # 900 s: Probe 1
    server.control({"offline": True})
    with caplog.at_level("DEBUG"):
        assert _tick(ctx, clock, outcome) is None  # 1800 s: Cloud antwortet mit 503, die Probe faellt aus
    server.control({"offline": False})
    saved = json.loads(ctx.paths.inventory_samples.read_text())
    assert len(saved["samples"]) == 1 and saved["next_at"] == ctx.wall() + commands.INVENTORY_EVERY_SECONDS
    done = None
    for _ in range(6):
        done = _tick(ctx, clock, outcome)
        if done is not None:
            break
    assert isinstance(done, commands.Done)
    assert len(done.result["proben"]) == 6  # sieben Zeitpunkte (900 ... 6300 s) minus der ausgefallene
    for secret in ("7637415", "2012345", "access_token", "refresh_token", "Bearer"):
        assert secret not in caplog.text and secret not in str(done.result)
    assert "DriverError" in caplog.text  # nur der Typname der Ausnahme


def test_after_a_failed_sample_the_next_attempt_waits_for_next_at(vicare_ctx):
    ctx, server, clock = vicare_ctx
    outcome = commands.execute(ctx, "driver_inventory", {"driver_id": "vicare_cloud", "stunden": 2})
    server.control({"offline": True})
    assert _tick(ctx, clock, outcome) is None  # 900 s: Fehlversuch
    attempted = server_calls(server)
    assert attempted >= 1
    server.control({"offline": False})
    assert _tick(ctx, clock, outcome, 300) is None  # 1200 s < next_at (1800 s): kein Abruf
    assert server_calls(server) == attempted
    assert _tick(ctx, clock, outcome, 600) is None  # 1800 s: naechster Versuch
    assert server_calls(server) > attempted
    assert len(json.loads(ctx.paths.inventory_samples.read_text())["samples"]) == 1


def test_an_inventory_without_any_sample_fails_at_the_deadline_with_a_contract_reason(vicare_ctx, caplog):
    ctx, server, clock = vicare_ctx
    outcome = commands.execute(ctx, "driver_inventory", {"driver_id": "vicare_cloud", "stunden": 1})
    server.control({"offline": True})
    with caplog.at_level("DEBUG"):
        result, ticks = _run_to_the_end(ctx, clock, outcome)
    assert ticks == 4  # erst am Ende des Fensters (3600 s), nicht schon beim ersten Fehlversuch
    assert isinstance(result, commands.Failed)
    assert result.grund == "anlage_nicht_erreichbar" and result.grund in wire.command_errors("driver_inventory")
    assert json.loads(ctx.paths.inventory_samples.read_text())["samples"] == []
    for secret in ("7637415", "2012345", "access_token", "refresh_token", "Bearer"):
        assert secret not in caplog.text and secret not in result.text


def test_an_inventory_whose_samples_are_all_rate_limited_reports_the_quota_at_the_deadline(vicare_ctx):
    ctx, server, clock = vicare_ctx
    outcome = commands.execute(ctx, "driver_inventory", {"driver_id": "vicare_cloud", "stunden": 1})
    server.control({"rate_limit_after": len(server.calls)})
    result, ticks = _run_to_the_end(ctx, clock, outcome)
    assert ticks == 4
    assert isinstance(result, commands.Failed) and result.grund == "kontingent_erschoepft"
    assert result.grund in wire.command_errors("driver_inventory")
