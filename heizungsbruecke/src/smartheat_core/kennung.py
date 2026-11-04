"""Kennung eines Heizkreises auf dem Geraet: `{hersteller, anlage, nummer}` ohne Weg, im kanonischen Text des Servers
(`gemeinsam/kennung.py`: Schluessel sortiert, kompakte Trenner, kein ASCII-Zwang).

Ein Weg (Cloud, Modbus, Gateway-Treiber) gehoert nicht zur Kennung: dieselbe Anlage bleibt dieselbe, wenn ihr Weg
wechselt. Die Tabellen unten spiegeln den Wegekatalog des Servers (`gemeinsam/wege.py`); der Contract-Check vergleicht
sie. Nur Standardbibliothek, weil Add-on und Gateway dieses Modul gemeinsam nutzen."""
import json

ANLAGE_MAX = 100
NUMMER_MAX = 9999
TEXT_MAX = 200

# Plattform der HA-Integration -> Hersteller
HERSTELLER_DER_PLATTFORM = {"mypyllant": "vaillant", "vicare": "viessmann", "weishaupt_modbus": "weishaupt"}

# Gateway-Treiber -> (Hersteller, Parameter, die die Anlage benennen; der Heizkreis kommt aus `heizkreis`)
TREIBER = {
    "vicare_cloud": ("viessmann", ("installation_id", "gateway_serial", "device_id")),
    "simulation": ("simulation", ("lever_set",)),
}


class KennungFehler(ValueError):
    """Die Angaben ergeben keine gueltige Kennung."""


def _text(hersteller, anlage, nummer):
    if not isinstance(anlage, str) or not anlage or anlage != anlage.strip() or not anlage.isprintable():
        raise KennungFehler("Anlage ist kein brauchbarer Text")
    if len(anlage) > ANLAGE_MAX:
        raise KennungFehler("Anlage ist zu lang")
    if isinstance(nummer, bool) or not isinstance(nummer, int) or not 0 <= nummer <= NUMMER_MAX:
        raise KennungFehler("Nummer liegt ausserhalb des Bereichs")
    text = json.dumps({"hersteller": hersteller, "anlage": anlage, "nummer": nummer}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)
    if len(text) > TEXT_MAX:
        raise KennungFehler("Kennung ist zu lang")
    return text


def aus_ha_kreis(plattform, anlage, nummer) -> str:
    """Kennung eines HA-Kreises: Hersteller aus der Plattform, `nummer` als Ziffernfolge wie im Kreisdokument."""
    hersteller = HERSTELLER_DER_PLATTFORM.get(plattform) if isinstance(plattform, str) else None
    if hersteller is None:
        raise KennungFehler("unbekannte Plattform")
    if not isinstance(nummer, str) or not (nummer.isascii() and nummer.isdigit()):
        raise KennungFehler("Nummer ist keine Ziffernfolge")
    return _text(hersteller, anlage, int(nummer))


def aus_treiber(treiber_id, parameter) -> str:
    """Kennung eines Gateway-Kreises: Hersteller aus dem Treiber, Anlage aus seinen Anlagenparametern, Nummer aus
    `heizkreis`."""
    eintrag = TREIBER.get(treiber_id) if isinstance(treiber_id, str) else None
    if eintrag is None:
        raise KennungFehler("unbekannter Treiber")
    if not isinstance(parameter, dict):
        raise KennungFehler("Parameter fehlen")
    hersteller, namen = eintrag
    teile = []
    for name in namen:
        wert = parameter.get(name)
        if isinstance(wert, bool) or not isinstance(wert, (str, int)) or wert == "":
            raise KennungFehler("Anlagenparameter fehlt oder ist unbrauchbar")
        teile.append(str(wert))
    return _text(hersteller, "-".join(teile), parameter.get("heizkreis"))
