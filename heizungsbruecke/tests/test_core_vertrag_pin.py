"""P4: der Kern (smartheat_core) importiert nichts, auch nicht den Vertrag smartheat_device.wire.
Seine Kopien der Vertragslisten sind deshalb hier gepinnt, jede gleich ihrem Gegenstueck in wire.py. Die Pins stehen als
`_VERTRAG_GEPINNT_<NAME> = (wire.<NAME>, <Kopie>)` auf Modulebene (der Leser-Test in test_device_wire.py erkennt sie
daran als gepinnt, nicht als gelesen) und werden danach verglichen. Eine neue Kopie im Kern braucht hier einen Pin.
Das gilt auch fuer die Laufzeit (smartheat_runtime): sie darf smartheat_device nicht importieren (test_modulgrenzen.py).
Die Kopie muss ein anderes Objekt sein als der Vertrag; dass sie aus dem Kern oder der Laufzeit stammt, prueft
test_device_wire.py per AST."""
import pytest

from smartheat_core import bindungsparameter, config_check
from smartheat_device import wire
from smartheat_runtime import dokument_config

_VERTRAG_GEPINNT_ABO = (wire.ABO, config_check.ABO_WERTE)
_VERTRAG_GEPINNT_BINDUNG_GATEWAY_FIELDS = (wire.BINDUNG_GATEWAY_FIELDS, config_check.GATEWAY_BINDUNG_FELDER)
_VERTRAG_GEPINNT_BINDUNG_HA_FIELDS = (wire.BINDUNG_HA_FIELDS, config_check.HA_BINDUNG_FELDER)
_VERTRAG_GEPINNT_BINDUNG_HA_OPTIONAL = (wire.BINDUNG_HA_OPTIONAL, config_check.HA_BINDUNG_OPTIONAL)
_VERTRAG_GEPINNT_TREIBER_FIELDS = (wire.TREIBER_FIELDS, config_check.TREIBER_FELDER)
_VERTRAG_GEPINNT_KREIS_FIELDS = (wire.KREIS_FIELDS, config_check.KREIS_FELDER)
_VERTRAG_GEPINNT_KREIS_OPTIONAL = (wire.KREIS_OPTIONAL, config_check.KREIS_OPTIONAL)
_VERTRAG_GEPINNT_HA_ROLLEN = (wire.HA_ROLLEN, config_check.HA_ROLLEN)
_VERTRAG_GEPINNT_HA_HINWEISE = (wire.HA_HINWEISE, config_check.HA_HINWEISE)
_VERTRAG_GEPINNT_PRUEF_REGELN = (wire.PRUEF_REGELN, config_check.PRUEF_REGELN)
_VERTRAG_GEPINNT_BINDUNGSPARAMETER_FIELDS = (wire.BINDUNGSPARAMETER_FIELDS, bindungsparameter.FELDER)
_VERTRAG_GEPINNT_SCHREIBSCHUTZ_FIELDS = (wire.SCHREIBSCHUTZ_FIELDS, bindungsparameter.SCHREIBSCHUTZ_FELDER)
_VERTRAG_GEPINNT_ZAEHLER = (wire.ZAEHLER, bindungsparameter.ZAEHLER)
_VERTRAG_GEPINNT_ENERGIE_EINHEITEN = (wire.ENERGIE_EINHEITEN, tuple(bindungsparameter.EINHEITEN))

_VERTRAG_GEPINNT_ABO_LAUFZEIT = (wire.ABO, tuple(dokument_config.ABO))  # Schluessel der Abbildung auf entitlement

PINS = {name: wert for name, wert in sorted(globals().items()) if name.startswith("_VERTRAG_GEPINNT_")}


@pytest.mark.parametrize("name", PINS)
def test_die_kopie_im_kern_gleicht_dem_vertrag(name):
    vertrag, kopie = PINS[name]
    assert kopie == vertrag, name
    assert kopie is not vertrag, name  # eine Kopie, kein Verweis auf den Vertrag


def test_es_gibt_pins():
    assert PINS
