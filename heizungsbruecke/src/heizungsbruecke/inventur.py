"""Inventur des HA-Hosts (Spec 5c 1.3, G-9): nur Entities, die der Filter des Servers nennt (Plattformen der
Hersteller-Integrationen, Domaenen, Domaene/Geraeteklasse), mit den Feldern des Vertrags; Geraete nur, soweit eine
gewaehlte Entity daran haengt; Handys nur mit den genannten Praefixen. Kein Herstellerwissen. Deaktivierte Entities
fehlen; Entities ohne Registry-Eintrag (YAML, Template ohne unique_id) zaehlen nur ueber Domaene bzw. Geraeteklasse.

Die Felder sind genau die, die der Server liest (heizungsserver.generic.ha_inventur.lesen; Gegenprobe in
tests/test_inventur.py): `name` ist der Name der Integration (original_name), an dem der Server die Kreise erkennt,
nicht der vom Nutzer vergebene. config/entity_registry/list liefert keine device_class; dann zaehlt das Attribut des
Zustands. Inhalte der Inventur gehoeren nie in Logs (Regel 6)."""
from smartheat_device import wire

OHNE_REGISTRY = "ohne_registry"
FILTER_TEXT = "Der Filter der Inventur ist ungültig."


def _texte(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def filter_pruefen(filter_) -> str | None:
    """Kundentext, wenn der Filter nicht die Form des Vertrags hat (wire.INVENTUR_FILTER_FIELDS: Listen von Texten,
    geraeteklassen Paare aus Domaene und Klasse), sonst None. Fehlende Felder sind leer, unbekannte zaehlen nicht."""
    if not isinstance(filter_, dict):
        return FILTER_TEXT
    for key in ("plattformen", "domaenen", "dienste"):
        if key in filter_ and filter_[key] is not None and not _texte(filter_[key]):
            return FILTER_TEXT
    klassen = filter_.get("geraeteklassen")
    if klassen is not None and not (isinstance(klassen, list)
                                    and all(_texte(pair) and len(pair) == 2 for pair in klassen)):
        return FILTER_TEXT
    return None


def _klasse(entry: dict, attributes: dict):
    return entry.get("device_class") or entry.get("original_device_class") or attributes.get("device_class")


def inventur(ha_api, filter_: dict) -> dict:
    """filter_ hat die Form aus filter_pruefen (der Befehl prueft vorher)."""
    plattformen = set(filter_.get("plattformen") or [])
    domaenen = set(filter_.get("domaenen") or [])
    klassen = {tuple(pair) for pair in filter_.get("geraeteklassen") or []}
    praefixe = tuple(filter_.get("dienste") or [])
    registry = {e["entity_id"]: e for e in ha_api.entity_registry() if isinstance(e, dict) and "entity_id" in e}
    states = {s["entity_id"]: s for s in ha_api.get_states() if isinstance(s, dict) and "entity_id" in s}
    entities, geraete_ids = [], set()
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
        if entry.get("device_id"):
            geraete_ids.add(entry["device_id"])
        entities.append({
            "entity_id": entity_id, "plattform": plattform, "unique_id": entry.get("unique_id"),
            "name": entry.get("original_name"), "config_entry": entry.get("config_entry_id"),
            "geraet": entry.get("device_id"), "einheit": attributes.get("unit_of_measurement"),
            "device_class": klasse, "state_class": attributes.get("state_class"), "zustand": state.get("state"),
            "attribute": {k: attributes[k] for k in wire.INVENTUR_ATTRIBUTE if k in attributes},
            "aktualisiert": state.get("last_updated"),
        })
    geraete = [{"id": d["id"], "name": d.get("name_by_user") or d.get("name"), "modell": d.get("model"),
                "config_entries": list(d.get("config_entries") or [])}
               for d in ha_api.device_registry() if isinstance(d, dict) and d.get("id") in geraete_ids]
    dienste = sorted(f"{s['domain']}.{name}" for s in ha_api.get_services()
                     if isinstance(s, dict) and s.get("domain") == "notify"
                     for name in (s.get("services") or {}) if f"notify.{name}".startswith(praefixe))
    config = ha_api.get_config()
    units = config.get("unit_system") if isinstance(config.get("unit_system"), dict) else {}
    return {"entities": entities, "geraete": geraete, "dienste": dienste,
            "einheit_temperatur": units.get("temperature"), "zeitzone": config.get("time_zone")}
