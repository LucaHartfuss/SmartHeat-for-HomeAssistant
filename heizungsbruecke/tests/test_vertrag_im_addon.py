"""Stufe 3 S8a-2 (A5-16, Audit 5 p5-ci): Vertragspruefungen, die nur Module des Add-ons (Kern, Laufzeit, HA-Host,
Geraetekern) lesen. Frueher Checks des Contract-Checks im Dev-Root (tools/contract_check.py): ganz Check 42, dazu der
Add-on-Teil von 4, 30 und 56; was dort ueber Repo-Grenzen geht, bleibt im Contract-Check. Je Pruefung eine Funktion
`_probleme_<nr>` auf den Modulen, ein Test auf den echten Modulen und Gegenproben, die zeigen, dass sie rot werden kann;
nur Check 56 ist eine Einzelzusicherung (ein Mengenvergleich zweier Konstanten) und steht als einzelner Test.

Schon strenger als Modul-Tests vorhanden, darum hier ohne Kopie:
- Check 15 (Hinweis-Kategorien) und die Kopie config_check.HA_ROLLEN aus Check 4: test_core_vertrag_pin.py (Gleichheit
  samt Reihenfolge von wire.HA_HINWEISE mit config_check.HA_HINWEISE, von wire.HA_ROLLEN
  mit config_check.HA_ROLLEN).
- Check 57 (Add-on-Optionen nur zum Starten): test_config_yaml.py (genau bootstrap_url und log_level, beide optional,
  kein options, hassio_api).
Die Pruefungen des Gateways stehen in gateway/tests/test_vertrag_im_gateway.py und
gateway/host/tests/test_vertrag_im_host.py."""
import ast
import inspect
import textwrap
from types import SimpleNamespace

from heizungsbruecke import discovery, dokument
from smartheat_core import levers
from smartheat_device import wire
from smartheat_runtime import delivery, regulation, roles, ticks


def _mit(module, **overrides):
    """Kopie eines Moduls als Namespace mit einzelnen geaenderten Attributen (Gegenproben)."""
    return SimpleNamespace(**{**vars(module), **overrides})


# --- Check 4 (Add-on-Teil): jede Rolle der HA-Bindung ist eine Rolle der Laufzeit ------------------------------------


def _probleme_4(wire, laufzeit_rolle, roles) -> list[str]:
    """Spec 5c: jede Rolle der HA-Bindung (wire.HA_ROLLEN) bildet das Add-on (dokument.laufzeit_rolle) auf eine Rolle
    der Laufzeit (roles.ALL_ROLES) ab. Das Hebel-Vokabular Server = Kern prueft weiter der Contract-Check 4."""
    all_roles = set(roles.ALL_ROLES)
    return [f"Rolle {rolle} -> {laufzeit_rolle(rolle)!r} ist keine Laufzeit-Rolle (roles.ALL_ROLES)"
            for rolle in wire.HA_ROLLEN if laufzeit_rolle(rolle) not in all_roles]


def test_check_4_jede_rolle_der_ha_bindung_ist_eine_laufzeit_rolle():
    assert _probleme_4(wire, dokument.laufzeit_rolle, roles) == []


def test_check_4_meldet_eine_rolle_ohne_laufzeit_rolle():
    ohne = _mit(roles, ALL_ROLES=tuple(r for r in roles.ALL_ROLES if r != "level_current"))
    assert _probleme_4(wire, dokument.laufzeit_rolle, ohne) == [
        "Rolle level -> 'level_current' ist keine Laufzeit-Rolle (roles.ALL_ROLES)"]


def test_check_4_meldet_eine_abbildung_auf_eine_unbekannte_rolle():
    def laufzeit_rolle(rolle):
        return "erfunden" if rolle == "outdoor_temp" else dokument.laufzeit_rolle(rolle)

    assert _probleme_4(wire, laufzeit_rolle, roles) == [
        "Rolle outdoor_temp -> 'erfunden' ist keine Laufzeit-Rolle (roles.ALL_ROLES)"]


# --- Check 30 (Add-on-Teil): Trigger der Ticks ------------------------------------------------------------------------


def _returned_string_constants(function) -> set[str]:
    """String-Literale, die `function` zurueckgibt (auch beide Zweige von `a if c else b`), aus dem Quelltext: die
    Pruefung braucht kein lauffaehiges Runtime-Objekt."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    found: set[str] = set()

    def collect(node) -> None:
        if isinstance(node, ast.IfExp):
            collect(node.body)
            collect(node.orelse)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and node.value is not None:
            collect(node.value)
    return found


def _probleme_30(claim_due_tick, delivery, ticks) -> list[str]:
    """TP12e (AU-022): claim_due_tick und der Start-Tick (ticks.PROBE_TRIGGER) liefern nur Trigger der Zustellung
    (delivery.TRIGGERS). Dass delivery.TRIGGERS gleich den Triggern des Servers ist (V1b), prueft weiter der
    Contract-Check 30; Drift = der Server lernt nie, und kein Alarm schlaegt an."""
    triggers = set(delivery.TRIGGERS)
    problems = []
    returned = _returned_string_constants(claim_due_tick)
    if not returned:
        problems.append("claim_due_tick: keine Trigger-Rueckgabe im Quelltext gefunden")
    elif not returned <= triggers:
        problems.append(
            f"claim_due_tick liefert {sorted(returned - triggers)}, delivery.TRIGGERS ist {sorted(triggers)}"
        )
    if ticks.PROBE_TRIGGER not in triggers:
        problems.append(f"ticks.PROBE_TRIGGER {ticks.PROBE_TRIGGER!r} ist kein Trigger {sorted(triggers)}")
    return problems


def test_check_30_ticks_liefern_nur_trigger_der_zustellung():
    assert _returned_string_constants(regulation.claim_due_tick) == {"daily", "target_change"}
    assert _probleme_30(regulation.claim_due_tick, delivery, ticks) == []


def test_check_30_meldet_einen_unbekannten_trigger_von_claim_due_tick():
    nur_daily = _mit(delivery, TRIGGERS=("daily",))
    assert _probleme_30(regulation.claim_due_tick, nur_daily, _mit(ticks, PROBE_TRIGGER="daily")) == [
        "claim_due_tick liefert ['target_change'], delivery.TRIGGERS ist ['daily']"]

    def beide_zweige(due):
        return "daily" if due else "target_changed"

    assert _probleme_30(beide_zweige, delivery, ticks) == [
        "claim_due_tick liefert ['target_changed'], delivery.TRIGGERS ist ['daily', 'target_change']"]


def test_check_30_meldet_ein_claim_due_tick_ohne_trigger_rueckgabe():
    def ohne(due):
        return None

    assert _probleme_30(ohne, delivery, ticks) == ["claim_due_tick: keine Trigger-Rueckgabe im Quelltext gefunden"]


def test_check_30_meldet_einen_unbekannten_start_trigger():
    assert _probleme_30(regulation.claim_due_tick, delivery, _mit(ticks, PROBE_TRIGGER="probe")) == [
        "ticks.PROBE_TRIGGER 'probe' ist kein Trigger ['daily', 'target_change']"]


# --- Check 42: Hebel-Rollen des Add-ons -----------------------------------------------------------------------------


def _probleme_42(roles, levers, wire) -> list[str]:
    """Plan 3c, Spec 5c: Jeder Hebel des Kerns (smartheat_core.levers.LEVERS) hat eine Laufzeit-Rolle
    (roles.LEVER_ROLES, ueber die das Add-on die HA-Bindung abbildet), diese Rolle kennt die Laufzeit (ALL_ROLES), und
    jeder Hebel ist eine Rolle der HA-Bindung im Vertrag des Add-ons (smartheat_device.wire.HA_ROLLEN)."""
    lever_roles = roles.LEVER_ROLES
    contract, all_roles = set(wire.HA_ROLLEN), set(roles.ALL_ROLES)
    problems = []
    if set(lever_roles) != set(levers.LEVERS):
        problems.append(f"roles.LEVER_ROLES {sorted(lever_roles)} != Hebel des Kerns {sorted(levers.LEVERS)}")
    problems += [f"Hebel {lever}: Rolle {role!r} nicht in roles.ALL_ROLES"
                 for lever, role in lever_roles.items() if role not in all_roles]
    problems += [f"Hebel {lever} ist keine Rolle der HA-Bindung (HA_ROLLEN)"
                 for lever in lever_roles if lever not in contract]
    return problems


def test_check_42_jeder_hebel_hat_eine_rolle_der_laufzeit_und_der_ha_bindung():
    assert _probleme_42(roles, levers, wire) == []


def test_check_42_meldet_einen_hebel_ohne_rolle():
    ohne = _mit(roles, LEVER_ROLES={k: v for k, v in roles.LEVER_ROLES.items() if k != "level"})
    assert _probleme_42(ohne, levers, wire) == [
        "roles.LEVER_ROLES ['curve', 'heat_limit', 'min_flow', 'room_setpoint'] != Hebel des Kerns "
        "['curve', 'heat_limit', 'level', 'min_flow', 'room_setpoint']"]


def test_check_42_meldet_eine_rolle_ausserhalb_der_laufzeit():
    unbekannt = _mit(roles, LEVER_ROLES={**roles.LEVER_ROLES, "min_flow": "min_flow_soll"})
    assert _probleme_42(unbekannt, levers, wire) == ["Hebel min_flow: Rolle 'min_flow_soll' nicht in roles.ALL_ROLES"]


def test_check_42_meldet_einen_hebel_ausserhalb_des_vertrags():
    vertrag = _mit(wire, HA_ROLLEN=tuple(r for r in wire.HA_ROLLEN if r != "min_flow"))
    assert _probleme_42(roles, levers, vertrag) == ["Hebel min_flow ist keine Rolle der HA-Bindung (HA_ROLLEN)"]


# --- Check 56 (Add-on-Teil): Zustand vor der Uebernahme ------------------------------------------------------------


def test_check_56_der_zustand_vor_der_uebernahme_ist_ein_zustand_des_vertrags():
    """Spec 5c 1.1: Die Discovery meldet den Code nur im Zustand discovery.NICHT_UEBERNOMMEN; der ist ein Zustand des
    Geraetevertrags (wire.ZUSTAENDE). Bis Stufe 3 S8a-2 im Contract-Check, dort zusammen mit der Kopie der Integration,
    die sie nicht liest (A5-85)."""
    assert discovery.NICHT_UEBERNOMMEN in wire.ZUSTAENDE
