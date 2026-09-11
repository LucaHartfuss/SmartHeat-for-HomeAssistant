from dataclasses import dataclass


@dataclass(frozen=True)
class FailsafeState:
    active: bool
    recovery_count: int


def enter_failsafe_if_stale(
    current: FailsafeState,
    seconds_since_last_valid: float | None,
    stale_after_seconds: float,
) -> FailsafeState:
    """Evaluates whether to enter fail-safe due to staleness. Only ever transitions
    inactive -> active; never touches an already-active state (recovery only happens
    via `record_valid_message`) -- mirrors the old watchdog automation's split between
    an hourly staleness check and a message-triggered recovery counter.

    `seconds_since_last_valid=None` (no valid message ever received yet) never trips
    fail-safe on its own -- there is nothing "stale" to detect yet.
    """
    if current.active:
        return current
    if seconds_since_last_valid is not None and seconds_since_last_valid >= stale_after_seconds:
        return FailsafeState(active=True, recovery_count=0)
    return current


def record_valid_message(current: FailsafeState) -> FailsafeState:
    """Evaluates the effect of one valid down-message on the fail-safe state. Only
    relevant while active: requires two consecutive calls (anti-flap) before flipping
    back to inactive, mirroring the old counter.reset()/counter.increment() pattern.
    A valid message while already inactive is a no-op.
    """
    if not current.active:
        return current
    new_count = current.recovery_count + 1
    if new_count > 1:
        return FailsafeState(active=False, recovery_count=0)
    return FailsafeState(active=True, recovery_count=new_count)


def build_discovery_config(tenant_id: str) -> dict:
    """Builds the MQTT Discovery config payload for the fail-safe binary_sensor. HA's
    MQTT integration creates the entity from this automatically -- no configuration.yaml
    needed on the customer side.
    """
    return {
        "name": "Fail-Safe",
        "unique_id": f"heizungsbruecke_{tenant_id}_failsafe",
        "state_topic": f"smartheat/{tenant_id}/status/failsafe",
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "problem",
        "device": {
            "identifiers": [f"heizungsbruecke_{tenant_id}"],
            "name": f"Heizungsbruecke ({tenant_id})",
            "manufacturer": "SmartHeat",
        },
    }


def build_state_payload(active: bool) -> str:
    return "ON" if active else "OFF"
