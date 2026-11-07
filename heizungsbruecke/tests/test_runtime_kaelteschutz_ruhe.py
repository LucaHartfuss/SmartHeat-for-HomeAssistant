"""Kaelteschutz-Lauf in einem Kreis, der ausser Betrieb oder in der Testphase ohne Setwell steht (Vaillant mit Zone): der Start
stellt die Zone auf Manuell, das Ende des Laufs stellt die Hebel auf den Wiederherstellungspunkt; die Zone und die Hilfswerte
bringt erst die Rueckstellung des Kreiszustands zurueck. Sie muss nach dem Lauf einmal wiederholt werden, danach ruht der
Kreis wieder (kein Schreibvorgang). Der Kreis ist von Hand gebaut wie in test_runtime_kaelteschutz.py."""
import dataclasses

import pytest
from bindung_beispiele import VAILLANT_MYPYLLANT
from test_runtime_kaelteschutz import (
    MINUTE,
    REFS,
    SAFETY,
    STUNDE,
    _ist,
    _kreis,
    _lauf,
    _lauft,
    _pruefen,
    uhr,  # noqa: F401 - Fixture
    wall,  # noqa: F401 - Fixture
)

from heizungsbruecke.ha_binding import MANUAL_HVAC_MODE, HaPlantBinding
from smartheat_core.pipeline import LeverPipeline
from smartheat_runtime.roles import ChannelManifest

ZONE = "climate.zone"
ZEITPROGRAMM = "auto"
AUSSER = "ausser_betrieb"
OHNE = "testphase_ohne"
ZONEN_REFS = {**REFS, "shift_current": f"{ZONE}::temperature"}


class ZonenHa:
    """Raumfuehler, zwei Hebel und eine Zone mit Betriebsart; `mode_fehler` laesst das Umstellen der Betriebsart scheitern."""

    def __init__(self, werte):
        self.werte = werte
        self.modes = {ZONE: ZEITPROGRAMM}
        self.ereignisse = []
        self.mode_fehler = False

    def get_state(self, ref):
        wert = self.werte[ref]
        if isinstance(wert, Exception):
            raise wert
        return wert

    def get_raw_state(self, entity_id):
        return self.modes.get(entity_id, "on")

    def get_attribute(self, entity_id, name):
        raise ValueError("kein Wertebereich")

    def set_number_value(self, entity_id, value):
        self.ereignisse.append(("hebel", entity_id, value))
        self.werte[entity_id] = value

    def set_climate_temperature(self, entity_id, value):
        self.ereignisse.append(("hebel", f"{entity_id}::temperature", value))
        self.werte[f"{entity_id}::temperature"] = value

    def set_hvac_mode(self, entity_id, mode):
        if self.mode_fehler:
            raise RuntimeError("Betriebsart nicht schreibbar")
        self.ereignisse.append(("zone", entity_id, mode))
        self.modes[entity_id] = mode


def _zonen_kreis(make_store, wall, uhr, zustand):  # noqa: F811
    backup = {"restore_point": {"curve": 0.3, "room_setpoint": 1.0}, "originals": {"curve": 0.3, "room_setpoint": 1.0},
              "last_room_target": 21.0}
    rt = _kreis(make_store(backup=backup), wall, uhr, {1: 21.0})
    rt.werte.update({f"{ZONE}::temperature": 1.0, "number.curve": 0.3})  # `_ist` schreibt in dieses Woerterbuch
    ha = ZonenHa(rt.werte)
    manifest = ChannelManifest(refs=ZONEN_REFS, raum_refs=rt.manifest.raum_refs)
    rt.manifest, rt.signals, rt.ha_api, rt.ha = manifest, ha, ha, ha
    rt.override = LeverPipeline(rt.store, HaPlantBinding(ha, manifest, VAILLANT_MYPYLLANT), SAFETY, clock=uhr)
    rt.override.capture_originals()
    assert rt.store.state.aux_originals == {"mode_select": ZEITPROGRAMM}
    rt.config = dataclasses.replace(rt.config, zustand_source=lambda: zustand)
    uhr.advance(VAILLANT_MYPYLLANT.settle_seconds + 1)
    return rt


def _frostlauf(rt):
    _pruefen(rt)
    _ist(rt, r1=4.0)
    _lauf(rt, 16 * MINUTE, schritt=MINUTE)
    assert _lauft(rt) and rt.ha.modes[ZONE] == MANUAL_HVAC_MODE  # der Start hat die Zone auf Manuell gestellt


def _ende(rt):
    _ist(rt, r1=9.0)
    _lauf(rt, 10 * MINUTE)
    assert not _lauft(rt) and rt.store.state.kaelteschutz_art is None


@pytest.mark.parametrize("zustand", [AUSSER, OHNE])
def test_nach_dem_lauf_steht_die_zone_wieder_auf_dem_zeitprogramm_und_der_kreis_ruht(make_store, wall, uhr, zustand):  # noqa: F811
    rt = _zonen_kreis(make_store, wall, uhr, zustand)
    _frostlauf(rt)
    _ende(rt)

    _lauf(rt, 10 * MINUTE)

    assert rt.ha.modes[ZONE] == ZEITPROGRAMM
    assert rt.ha.werte["number.curve"] == 0.3 and rt.ha.werte[f"{ZONE}::temperature"] == 1.0
    danach = len(rt.ha.ereignisse)
    _lauf(rt, 2 * STUNDE)
    assert rt.ha.ereignisse[danach:] == []


@pytest.mark.parametrize("zustand", [AUSSER, OHNE])
def test_scheitert_die_rueckstellung_nach_dem_lauf_wiederholt_der_naechste_check_sie(make_store, wall, uhr, zustand):  # noqa: F811
    rt = _zonen_kreis(make_store, wall, uhr, zustand)
    _frostlauf(rt)
    rt.ha.mode_fehler = True
    _ende(rt)
    _lauf(rt, 10 * MINUTE)
    assert rt.ha.modes[ZONE] == MANUAL_HVAC_MODE  # nicht schreibbar: die Zone bleibt, die Rueckstellung offen

    rt.ha.mode_fehler = False
    _lauf(rt, 10 * MINUTE)

    assert rt.ha.modes[ZONE] == ZEITPROGRAMM
    danach = len(rt.ha.ereignisse)
    _lauf(rt, STUNDE)
    assert rt.ha.ereignisse[danach:] == []

