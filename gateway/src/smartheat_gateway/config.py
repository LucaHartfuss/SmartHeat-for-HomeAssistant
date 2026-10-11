"""Konfiguration des Gateways als Dokument vom Server (Spec 5b 3.1/5.2, Plan D2): die Laufzeit-Konfiguration liest
smartheat_runtime.dokument_config, die Bindung (Treiber, Thermostat, Raumfuehler) dieses Modul. Dazu die Pruefung der
Treiberparameter fuer config_check, der Startwert des Soll-Speichers und die Abmelde-Markierung (sign_off)."""
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from smartheat_device.documents import im_bedienbereich
from smartheat_gateway.drivers import registry
from smartheat_gateway.drivers.registry import driver_ids
from smartheat_gateway.files import read_json, write_json
from smartheat_gateway.paths import Paths
from smartheat_runtime import dokument_config, options
from smartheat_runtime.options import ConfigError
from smartheat_runtime.runtime_config import RuntimeConfig

IEEE = re.compile(r"0x[0-9a-f]{16}")
ROOM_SENSOR_REF = re.compile(r"zigbee:0x[0-9a-f]{16}:(temperature|local_temperature)|treiber:room_temperature")
DEFAULT_RAUM_SOLL = 20.0
DRIVER_UNKNOWN_TEXT = "Diese Anlagenanbindung (Treiber) kennt das Gateway nicht."
DRIVER_PARAMETER_TEXT = "Die Treiberparameter passen nicht zu dieser Anlagenanbindung ({art})."


@dataclass(frozen=True)
class GatewayBindung:
    driver_id: str
    driver_parameter: dict
    room_sensors: tuple[str, ...]
    thermostat: str | None

    def driver_spec(self) -> dict:
        return {"id": self.driver_id, "parameter": dict(self.driver_parameter)}


def bindung(konfiguration) -> GatewayBindung:
    """Bindung des Gateways aus dem Konfigurationsdokument (Spec 5b 3.1): bindung.treiber, bindung.thermostat,
    raumfuehler. Wirft ConfigError (StartFailure beim Start); config_check hat das Dokument beim Uebernehmen geprueft."""
    raw = konfiguration.get("bindung") if isinstance(konfiguration, Mapping) else None
    treiber = raw.get("treiber") if isinstance(raw, Mapping) else None
    driver_id = treiber.get("id") if isinstance(treiber, Mapping) else None
    parameter = treiber.get("parameter") if isinstance(treiber, Mapping) else None
    if driver_id not in driver_ids() or not isinstance(parameter, Mapping):
        raise ConfigError(f"Treiber {driver_id!r} unbekannt oder ohne Parameter – {options.RECONFIGURE_HINT}")
    sensors = konfiguration.get("raumfuehler")
    if not isinstance(sensors, list) or not sensors or any(
            not isinstance(ref, str) or not ROOM_SENSOR_REF.fullmatch(ref) for ref in sensors):
        raise ConfigError(f"Raumfühler ({sensors!r}) ungültig – {options.RECONFIGURE_HINT}")
    thermostat = raw.get("thermostat") if isinstance(raw, Mapping) else None
    if thermostat is not None and (not isinstance(thermostat, str) or not IEEE.fullmatch(thermostat)):
        raise ConfigError(f"Thermostat ({thermostat!r}) ist keine IEEE-Adresse – {options.RECONFIGURE_HINT}")
    return GatewayBindung(driver_id, dict(parameter), tuple(sensors), thermostat)


def parse_dokument(konfiguration, paths: Paths,
                   abo_source: Callable[[], str]) -> tuple[RuntimeConfig, GatewayBindung]:
    runtime = dokument_config.runtime_config(konfiguration, entitlement_path=paths.entitlement, abo_source=abo_source)
    return runtime, bindung(konfiguration)


def treiber_pruefen(paths: Paths, bindung_inhalt) -> str | None:
    """Rueckruf fuer config_check (DeviceHost.treiber_pruefen): der Treiber nimmt die Parameter an. Ohne Schreiben
    (writer=False, die Simulation legt keine Anlage an), ohne Netz; der Text nennt nur die Fehlerart (Regel 6)."""
    treiber = bindung_inhalt.get("treiber") if isinstance(bindung_inhalt, Mapping) else None
    driver_id = treiber.get("id") if isinstance(treiber, Mapping) else None
    if driver_id not in registry.DRIVERS:
        return DRIVER_UNKNOWN_TEXT
    parameter = treiber.get("parameter") if isinstance(treiber, Mapping) else None
    try:
        registry.create(driver_id, dict(parameter or {}), paths, writer=False)
    except (KeyError, TypeError, ValueError) as error:
        return DRIVER_PARAMETER_TEXT.format(art=type(error).__name__)
    return None


def raum_soll_start(bedienung_inhalt) -> float:
    """Startwert des Soll-Speichers: die Bedienung, sonst 20 °C (Bedienbereich, Spec 3.2)."""
    value = bedienung_inhalt.get("raum_soll") if isinstance(bedienung_inhalt, Mapping) else None
    return float(value) if isinstance(value, int | float) and im_bedienbereich(value) else DEFAULT_RAUM_SOLL


def abgemeldet(paths: Paths, konfiguration) -> bool:
    """Abmelde-Markierung (sign_off) fuer die Anlage der aktuellen Konfiguration?"""
    marker = read_json(paths.abgemeldet)
    anlage = konfiguration.get("anlage") if isinstance(konfiguration, Mapping) else None
    return isinstance(marker, dict) and anlage is not None and marker.get("anlage") == anlage


def abmelden(paths: Paths, anlage: str) -> bool:
    """Markierung setzen (Befehl sign_off); True, wenn sie neu ist (dann startet die Laufzeit neu)."""
    marker = read_json(paths.abgemeldet)
    if isinstance(marker, dict) and marker.get("anlage") == anlage:
        return False
    write_json(paths.abgemeldet, {"anlage": anlage}, private=True)
    return True
