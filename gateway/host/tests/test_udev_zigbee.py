"""udev-Regel fuer den Zigbee-Stick (host/udev/99-smartheat-zigbee.rules): jeder bekannte Stick trifft
genau eine Regel, und die schreibt die richtige Adapterart fuer den Init-Schritt. Die Regel wird hier mit einem kleinen
Abgleich nach udev-Art gelesen (Glob, Alternativen mit |, == und !=); die Syntax selbst prueft udevadm verify in
install_checks.sh."""
import re
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

RULES = Path(__file__).resolve().parents[1] / "udev" / "99-smartheat-zigbee.rules"
TOKEN = re.compile(r'([A-Z_]+(?:\{[^}]*\})?)\s*(==|!=|\+=|=)\s*"((?:[^"\\]|\\.)*)"')
ADAPTER = re.compile(r"echo (\w+) > ")


def _matches(pattern: str, value: str) -> bool:
    return any(fnmatchcase(value, alternative) for alternative in pattern.split("|"))


def _adapters(device: dict[str, str]) -> list[str]:
    """Adapterarten aller Regeln, die auf das Geraet passen (in Dateireihenfolge)."""
    found = []
    for line in RULES.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        tokens = TOKEN.findall(line)
        conditions = [(key, op, value) for key, op, value in tokens if op in ("==", "!=")]
        assert conditions, line
        if all(_matches(value, device.get(key, "")) == (op == "==") for key, op, value in conditions):
            run = [value for key, op, value in tokens if key == "RUN" and op == "+="]
            assert len(run) == 1, line
            found.append(ADAPTER.search(run[0]).group(1))
    return found


def _stick(vendor: str, product_id: str, product: str) -> dict[str, str]:
    return {"SUBSYSTEM": "tty", "ATTRS{idVendor}": vendor, "ATTRS{idProduct}": product_id, "ATTRS{product}": product}


@pytest.mark.parametrize("device, adapter", [
    (_stick("10c4", "ea60", "Sonoff Zigbee 3.0 USB Dongle Plus"), "zstack"),  # ZBDongle-P (CC2652P)
    (_stick("10c4", "ea60", "Sonoff Zigbee 3.0 USB Dongle Plus V2"), "ember"),  # ZBDongle-E (EFR32MG21), CP2102N
    (_stick("1a86", "55d4", "SONOFF Zigbee 3.0 USB Dongle Plus V2"), "ember"),  # ZBDongle-E mit CH9102
    # Labor 2026-10-20 (H2): EFR32MG24 mit EmberZNet (EZSP 13); als zstack startete Zigbee2MQTT endlos neu
    (_stick("10c4", "ea60", "SONOFF Dongle Plus MG24"), "ember"),
], ids=["dongle-p", "dongle-e-cp210x", "dongle-e-ch9102", "dongle-plus-mg24"])
def test_known_stick_gets_exactly_one_adapter(device, adapter):
    assert _adapters(device) == [adapter]


@pytest.mark.parametrize("device", [
    _stick("0403", "6001", "FT232R USB UART"),
    {**_stick("10c4", "ea60", "SONOFF Dongle Plus MG24"), "SUBSYSTEM": "block"},
], ids=["other-serial", "not-a-tty"])
def test_other_devices_match_no_rule(device):
    assert _adapters(device) == []
