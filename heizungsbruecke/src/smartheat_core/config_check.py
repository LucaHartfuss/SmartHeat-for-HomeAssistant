"""Lokale Pruefung einer Konfiguration vor dem Uebernehmen (Spec 5b 3.1, Weichenstellung 1), rein: nur
Standardbibliothek und smartheat_core. Regeln in dieser Reihenfolge: (1) Aufbau und Wertebereiche, (2) Hebelsatz und
Verteilsystem haben lokale Sicherheitswerte (safety.py), (3) eingerastet: ein Wechsel von Hebelsatz oder Verteilsystem
nur ueber "nicht eingerichtet", (4) erlaubte Referenzen je Rolle, (5) keine Doppelbelegung, (6) Treiberparameter prueft
der Treiber (Rueckruf des Hosts, zuletzt). Eine leere Konfiguration (anlage None) heisst "nicht eingerichtet" und ist
immer gueltig. Die Gruende sind Dokument-Fehlergruende des Vertrags (smartheat_device.wire.DOKUMENT_ERRORS), die Texte
gehen an Portal und Assistent (Kundensprache). Sicherheitsgrenzen, Boost und Notbetrieb liest diese Pruefung nur
(G-1, Regel 4)."""
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from smartheat_core.safety import resolve_local_safety

UNGUELTIG = "ungueltig"
HEBELSATZ_EINGERASTET = "hebelsatz_eingerastet"
SICHERHEITSWERTE_FEHLEN = "sicherheitswerte_fehlen"
ROLLE_UNZULAESSIG = "rolle_unzulaessig"
DOPPELT_BELEGT = "doppelt_belegt"
TREIBER_PARAMETER = "treiber_parameter"
GRUENDE = (UNGUELTIG, HEBELSATZ_EINGERASTET, SICHERHEITSWERTE_FEHLEN, ROLLE_UNZULAESSIG, DOPPELT_BELEGT,
           TREIBER_PARAMETER)
HOST_GATEWAY = "gateway"
HOST_HA = "ha"
ABO_WERTE = ("aktiv", "inaktiv")
#: Gateway: Raumfuehler aus Zigbee (Temperatur bzw. Isttemperatur eines Thermostats) oder vom Treiber (SHG G2 3.2).
GATEWAY_RAUMFUEHLER = re.compile(r"zigbee:0x[0-9a-f]{16}:(temperature|local_temperature)|treiber:room_temperature")
IEEE = re.compile(r"0x[0-9a-f]{16}")
TAGESTICK = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
#: Intervalle (nur E2E) mit den Grenzen der Add-on-Optionen (smartheat_runtime.options, TP12e); ein Test haelt sie gleich.
PRUEFUNG_S = (1, 3600)
TELEMETRIE_S = (10, 600)
INTERVALLE = {"pruefung_s": PRUEFUNG_S, "telemetrie_s": TELEMETRIE_S}
RAUMFUEHLER_MAX = 8
ANLAGE_MAX_CHARS = 64
TEXT_MAX_CHARS = 200

TreiberPruefung = Callable[[dict], str | None]


@dataclass(frozen=True)
class Ablehnung:
    grund: str
    text: str


def ist_leer(inhalt) -> bool:
    """Leere Konfiguration (Spec 3.1): `anlage` fehlt oder ist None."""
    return isinstance(inhalt, Mapping) and inhalt.get("anlage") is None


def _text(value, limit: int = TEXT_MAX_CHARS) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _zahl(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _aufbau(neu: Mapping) -> str | None:
    if not _text(neu.get("anlage"), ANLAGE_MAX_CHARS):
        return "Die Anlage fehlt oder ist ungültig."
    for key in ("profil", "hebelsatz", "verteilsystem"):
        if not _text(neu.get(key)):
            return f"Das Feld {key} fehlt oder ist ungültig."
    if not isinstance(neu.get("bindung"), Mapping):
        return "Die Bindung fehlt."
    sensors = neu.get("raumfuehler")
    if not isinstance(sensors, list) or not 0 < len(sensors) <= RAUMFUEHLER_MAX or not all(map(_text, sensors)):
        return f"Es braucht einen bis {RAUMFUEHLER_MAX} Raumfühler."
    tick = neu.get("tagestick")
    if not isinstance(tick, str) or not TAGESTICK.fullmatch(tick):
        return "Die Zeit des Tagesticks fehlt oder ist keine Uhrzeit (HH:MM)."
    if neu.get("abo") not in ABO_WERTE:
        return "Der Abo-Status fehlt."
    intervalle = neu.get("intervalle")
    if intervalle is not None:
        if not isinstance(intervalle, Mapping) or not set(intervalle) <= set(INTERVALLE):
            return "Die Intervalle sind ungültig."
        for key, (low, high) in INTERVALLE.items():
            value = intervalle.get(key)
            if value is not None and not (_zahl(value) and low <= value <= high):
                return f"Das Intervall {key} muss zwischen {low} und {high} Sekunden liegen."
    return None


def _gateway_bindung(neu: Mapping) -> Ablehnung | None:
    bindung = neu["bindung"]
    treiber = bindung.get("treiber")
    if not (isinstance(treiber, Mapping) and _text(treiber.get("id")) and isinstance(treiber.get("parameter"), Mapping)):
        return Ablehnung(ROLLE_UNZULAESSIG, "Die Bindung braucht einen Treiber mit Parametern.")
    thermostat = bindung.get("thermostat")
    if thermostat is not None and not (isinstance(thermostat, str) and IEEE.fullmatch(thermostat)):
        return Ablehnung(ROLLE_UNZULAESSIG, "Das Thermostat ist kein Zigbee-Gerät.")
    seen: set[str] = set()
    for ref in neu["raumfuehler"]:
        if not GATEWAY_RAUMFUEHLER.fullmatch(ref):
            return Ablehnung(ROLLE_UNZULAESSIG, f"Raumfühler {ref} ist für das Gateway nicht zulässig.")
        if ref in seen:
            return Ablehnung(DOPPELT_BELEGT, f"Raumfühler {ref} ist doppelt zugeordnet.")
        seen.add(ref)
    return None


def pruefen(neu: Mapping, vorher: Mapping | None, *, host: str,
            treiber_pruefen: TreiberPruefung | None = None) -> Ablehnung | None:
    """None = uebernehmen; sonst der erste verletzte Grund. `vorher` ist die zuletzt uebernommene Konfiguration."""
    if not isinstance(neu, Mapping):
        return Ablehnung(UNGUELTIG, "Die Konfiguration ist kein Objekt.")
    if ist_leer(neu):
        return None
    problem = _aufbau(neu)
    if problem is not None:
        return Ablehnung(UNGUELTIG, problem)
    try:
        resolve_local_safety(neu["hebelsatz"], neu["verteilsystem"])
    except ValueError as error:
        return Ablehnung(SICHERHEITSWERTE_FEHLEN, f"Für diese Anlage fehlen lokale Sicherheitswerte ({error}).")
    if vorher is not None and not ist_leer(vorher) and (
            vorher.get("hebelsatz"), vorher.get("verteilsystem")) != (neu["hebelsatz"], neu["verteilsystem"]):
        return Ablehnung(HEBELSATZ_EINGERASTET, "Hebelsatz und Verteilsystem lassen sich nur ändern, nachdem das Gerät "
                                                "entfernt und neu eingerichtet wurde.")
    if host != HOST_GATEWAY:
        return Ablehnung(ROLLE_UNZULAESSIG, "Die Bindung für Home Assistant folgt mit Teilprojekt 5c.")
    ablehnung = _gateway_bindung(neu)
    if ablehnung is not None:
        return ablehnung
    if treiber_pruefen is not None:
        text = treiber_pruefen(dict(neu["bindung"]))
        if text:
            return Ablehnung(TREIBER_PARAMETER, text)
    return None
