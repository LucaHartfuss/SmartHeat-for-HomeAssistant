"""Inventur des HA-Hosts (Spec 5c 1.3, G-9): nur Entities, die der Filter des Servers nennt (Plattformen der
Hersteller-Integrationen, Domaenen, Domaene/Geraeteklasse), mit den Feldern des Vertrags; Geraete nur, soweit eine
gewaehlte Entity daran haengt; Handys nur mit den genannten Praefixen. Kein Herstellerwissen. Deaktivierte Entities
fehlen; Entities ohne Registry-Eintrag (YAML, Template ohne unique_id) zaehlen nur ueber Domaene bzw. Geraeteklasse.

Die Felder sind genau die des Vertrags (wire.INVENTUR_*_FIELDS; pruefen() setzt es beim Bauen durch, ein Abweichen ist
ein Vertragsfehler mit Feldnamen) und die, die der Server liest (heizungsserver.einrichtung.ha_inventur.lesen; Gegenprobe in
tests/test_inventur.py): `name` ist der Name der Integration (original_name), an dem der Server die Kreise erkennt,
nicht der vom Nutzer vergebene. config/entity_registry/list liefert keine device_class; dann zaehlt das Attribut des
Zustands. Inhalte der Inventur gehoeren nie in Logs (Regel 6).

Positivliste (Spec Stufe 3 S5 3.1, A5-30): nach dem Filter des Servers prueft der Host lokal, was ueberhaupt
herausgehen darf; ein kompromittierter Server kann so keine Schloesser, Personen oder Standorte abziehen. Verworfenes
erscheint im Log nur als Anzahl je Domaene."""
import logging
from collections import Counter

from smartheat_device import fehler, wire
from smartheat_device.nachrichten import pruefen

logger = logging.getLogger(__name__)

OHNE_REGISTRY = "ohne_registry"

#: Lokale Positivliste (Spec Stufe 3 S5 3.1, A5-30): was der Server-Filter darueber hinaus verlangt, faellt weg. Keine
#: Plattformliste (G-7): ein Sensor ohne passende Klasse bleibt nur, wenn seine Plattform etwas Einstellbares liefert.
ERLAUBTE_DOMAENEN = frozenset({"climate", "number", "select", "weather"})
ERLAUBTE_BINAER_KLASSEN = frozenset({"battery"})
ERLAUBTE_SENSOR_KLASSEN = frozenset({"temperature", "battery", "energy", "gas", "pressure", "power", "duration"})
EINSTELLBARE_DOMAENEN = frozenset({"climate", "number", "select"})
DIENST_PRAEFIX = "notify.mobile_app_"


def _texte(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def filter_pruefen(filter_) -> dict | None:
    """details, wenn der Filter nicht die Form des Vertrags hat (genau die Felder wire.INVENTUR_FILTER_FIELDS: Listen
    von Texten, geraeteklassen Paare aus Domaene und Klasse), sonst None. Fehlende Felder sind leer, ein Schluessel
    ausserhalb der Liste lehnt den Filter ab (V1 ist beidseitig strikt, Nutzer 2026-10-16)."""
    if not isinstance(filter_, dict):
        return fehler.details("filter", "ungueltig")
    if (fremd := fehler.fremdes_feld(filter_, wire.INVENTUR_FILTER_FIELDS)) is not None:
        return fremd
    for key in ("plattformen", "domaenen", "dienste"):
        if key in filter_ and filter_[key] is not None and not _texte(filter_[key]):
            return fehler.details("filter", "ungueltig")
    klassen = filter_.get("geraeteklassen")
    if klassen is not None and not (isinstance(klassen, list)
                                    and all(_texte(pair) and len(pair) == 2 for pair in klassen)):
        return fehler.details("filter", "ungueltig")
    return None


def _klasse(entry: dict, attributes: dict):
    return entry.get("device_class") or entry.get("original_device_class") or attributes.get("device_class")


def _erlaubt(domain: str, klasse, plattform: str, einstellbar: set[str]) -> bool:
    if domain in ERLAUBTE_DOMAENEN:
        return True
    if domain == "binary_sensor":
        return klasse in ERLAUBTE_BINAER_KLASSEN
    if domain == "sensor":
        return klasse in ERLAUBTE_SENSOR_KLASSEN or plattform in einstellbar
    return False


def inventur(ha_api, filter_: dict) -> dict:
    """filter_ hat die Form aus filter_pruefen (der Befehl prueft vorher); danach greift die lokale Positivliste."""
    plattformen = set(filter_.get("plattformen") or [])
    domaenen = set(filter_.get("domaenen") or [])
    klassen = {tuple(pair) for pair in filter_.get("geraeteklassen") or []}
    praefixe = tuple(filter_.get("dienste") or [])
    registry = {e["entity_id"]: e for e in ha_api.entity_registry() if isinstance(e, dict) and "entity_id" in e}
    states = {s["entity_id"]: s for s in ha_api.get_states() if isinstance(s, dict) and "entity_id" in s}
    treffer = []
    for entity_id in sorted(set(registry) | set(states)):
        entry = registry.get(entity_id, {})
        state = states.get(entity_id)
        if entry.get("disabled_by") or state is None:
            continue
        domain = entity_id.split(".", 1)[0]
        raw = state.get("attributes")
        attributes: dict = raw if isinstance(raw, dict) else {}
        plattform = entry.get("platform") or OHNE_REGISTRY
        klasse = _klasse(entry, attributes)
        if not (plattform in plattformen or domain in domaenen or (domain, klasse) in klassen):
            continue
        treffer.append((entity_id, entry, state, domain, attributes, plattform, klasse))
    einstellbar = {plattform for (_, _, _, domain, _, plattform, _) in treffer
                   if domain in EINSTELLBARE_DOMAENEN and plattform != OHNE_REGISTRY}
    entities, geraete_ids, verworfen = [], set(), Counter()
    for entity_id, entry, state, domain, attributes, plattform, klasse in treffer:
        if not _erlaubt(domain, klasse, plattform, einstellbar):
            verworfen[domain] += 1
            continue
        if entry.get("device_id"):
            geraete_ids.add(entry["device_id"])
        entities.append(pruefen("inventur.entity", {
            "entity_id": entity_id, "plattform": plattform, "unique_id": entry.get("unique_id"),
            "name": entry.get("original_name"), "config_entry": entry.get("config_entry_id"),
            "geraet": entry.get("device_id"), "einheit": attributes.get("unit_of_measurement"),
            "device_class": klasse, "state_class": attributes.get("state_class"), "zustand": state.get("state"),
            "attribute": {k: attributes[k] for k in wire.INVENTUR_ATTRIBUTE if k in attributes},
            "aktualisiert": state.get("last_updated"),
        }, wire.INVENTUR_ENTITY_FIELDS))
    geraete = [pruefen("inventur.geraet", {"id": d["id"], "name": d.get("name_by_user") or d.get("name"),
                                           "modell": d.get("model"), "config_entries": list(d.get("config_entries") or [])},
                       wire.INVENTUR_GERAET_FIELDS)
               for d in ha_api.device_registry() if isinstance(d, dict) and d.get("id") in geraete_ids]
    if verworfen:
        logger.info("Inventur: %d Entities ausserhalb der Positivliste verworfen (%s)", sum(verworfen.values()),
                    ", ".join(f"{domain}={anzahl}" for domain, anzahl in sorted(verworfen.items())))
    dienste = sorted(f"{s['domain']}.{name}" for s in ha_api.get_services()
                     if isinstance(s, dict) and s.get("domain") == "notify"
                     for name in (s.get("services") or {})
                     if f"notify.{name}".startswith(praefixe) and f"notify.{name}".startswith(DIENST_PRAEFIX))
    config = ha_api.get_config()
    units = config.get("unit_system") if isinstance(config.get("unit_system"), dict) else {}
    return pruefen("inventur", {"entities": entities, "geraete": geraete, "dienste": dienste,
                                "einheit_temperatur": units.get("temperature"), "zeitzone": config.get("time_zone")},
                   wire.INVENTUR_HA_FIELDS)
