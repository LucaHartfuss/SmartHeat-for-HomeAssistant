from dataclasses import dataclass


@dataclass(frozen=True)
class FailsafeState:
    active: bool
    awaiting_seq: str | None


def register_publish_attempt(current: FailsafeState, seq: str) -> FailsafeState:
    """Records a fresh full-snapshot publish attempt as the one whose ack-timeout
    matters now -- supersedes any earlier still-open attempt (only the latest attempt's
    resolution, ack or timeout, can still change `active`; see Design-Spec 2026-09-23).
    """
    return FailsafeState(active=current.active, awaiting_seq=seq)


def enter_notbetrieb_on_ack_timeout(current: FailsafeState, timed_out_seq: str) -> FailsafeState:
    """The ack-timeout for `timed_out_seq` elapsed with no matching down-message.
    Only acts if `timed_out_seq` is still the attempt being awaited -- a no-op if it was
    already resolved (acked) or superseded by a newer publish attempt in the meantime.
    """
    if current.awaiting_seq != timed_out_seq:
        return current
    return FailsafeState(active=True, awaiting_seq=None)


def exit_notbetrieb_on_ack(current: FailsafeState, acked_seq: str) -> FailsafeState:
    """A down-message with `acked_seq` arrived. Only acts if `acked_seq` is still the
    attempt being awaited -- a stale ack for an already-resolved or superseded attempt
    is ignored (see Design-Spec 2026-09-23 Edge Cases).
    """
    if current.awaiting_seq != acked_seq:
        return current
    return FailsafeState(active=False, awaiting_seq=None)


def build_discovery_config(tenant_id: str) -> dict:
    """Builds the MQTT Discovery config payload for the fail-safe binary_sensor. HA's
    MQTT integration creates the entity from this automatically -- no configuration.yaml
    needed on the customer side.
    """
    return {
        "name": "Fail-Safe",
        "unique_id": f"heizungsbruecke_{tenant_id}_failsafe",
        "state_topic": f"smartheat/{tenant_id}/status/failsafe",
        "availability_topic": f"smartheat/{tenant_id}/status/availability",
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
