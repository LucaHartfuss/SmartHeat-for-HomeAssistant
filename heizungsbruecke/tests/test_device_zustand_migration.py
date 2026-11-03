"""Einmalige Migration des Zustands im Wartungsfenster: vom Stand ohne Kreisordner (Heizungsbruecke 1.1.0: Dateien in
/data, Gateway: Dateien in runtime/) in `runtime/kreis-1/`, mit neuer Kennung und Sicherung `*.pre-<Release>`."""
import json
import os
import stat
from types import SimpleNamespace

import pytest
from geraet_helpers import gateway_konfiguration, ha_konfiguration

from smartheat_core import dateien, kennung, zustandspfade
from smartheat_device import zustand_migration as migration

RELEASE = "1.2.0"
HA, GATEWAY = "ha", "gateway"
BINDUNG_GATEWAY = {"treiber": {"id": "simulation", "parameter": {"lever_set": "viessmann_vicare", "heizkreis": 1}},
                   "thermostat": "0x0000000000000001"}
# Die Kennung der Anlagen vor der Umstellung, woertlich wie die alten Hosts sie schrieben.
ALT = {
    HA: '{"anlage": "S", "nummer": "0", "plattform": "mypyllant"}',
    GATEWAY: '{"heizkreis": 1, "id": "simulation", "lever_set": "viessmann_vicare"}',
}
NEU = {
    HA: '{"anlage":"S","hersteller":"vaillant","nummer":0}',
    GATEWAY: '{"anlage":"viessmann_vicare","hersteller":"simulation","nummer":1}',
}
INHALT = {
    zustandspfade.FAILSAFE: {"seq": 7, "stufe": "normal"},
    zustandspfade.ENTITLEMENT: {"abo": "aktiv", "seit": "2026-10-01"},
    zustandspfade.ROOM_TARGET: {"wert": 21.5},
}


def _alte_pfade(daten, host):
    """Wo der Stand vor der Umstellung die vier Dateien ablegte."""
    runtime = daten / "runtime"
    ort = daten if host == HA else runtime
    return {zustandspfade.BACKUP: ort / "backup.json", zustandspfade.FAILSAFE: ort / "failsafe_state.json",
            zustandspfade.ENTITLEMENT: ort / "entitlement_state.json", zustandspfade.ROOM_TARGET: runtime / "room_target.json"}


def _konfiguration(host):
    return ha_konfiguration() if host == HA else gateway_konfiguration(bindung=BINDUNG_GATEWAY)


def _konfiguration_ablegen(ordner, inhalt):
    dateien.schreiben_json(ordner / "konfiguration.json", {"version": 1, "inhalt": inhalt, "dirty": False},
                           modus=dateien.PRIVAT)


def _backup(host, **felder):
    return {"tenant_id": "kunde3", "plant_id": ALT[host], "lifetime_writes": 5,
            "write_budget": {"writes:total": {"day": "2026-10-30", "count": 3}}, **felder}


@pytest.fixture(params=[HA, GATEWAY])
def anlage(request, tmp_path):
    """Eine Anlage im Stand vor der Umstellung: bestaetigte Konfiguration und alle vier Zustandsdateien."""
    host = request.param
    (tmp_path / "device").mkdir()
    _konfiguration_ablegen(tmp_path / "device", _konfiguration(host))
    alt = _alte_pfade(tmp_path, host)
    for pfad in alt.values():
        pfad.parent.mkdir(parents=True, exist_ok=True)
    alt[zustandspfade.BACKUP].write_text(json.dumps(_backup(host)))
    for name, inhalt in INHALT.items():
        alt[name].write_text(json.dumps(inhalt))
    return SimpleNamespace(host=host, daten=tmp_path, ordner=tmp_path / "device", alt=alt,
                           neu=tmp_path / "runtime" / "kreis-1")


def _lauf(anlage, release=RELEASE):
    return migration.migrieren(anlage.ordner, host=anlage.host, release=release)


def _stand(daten):
    """Alle Dateien unter dem Datenordner mit Inhalt: der Vergleich vorher/nachher."""
    return {str(pfad.relative_to(daten)): pfad.read_bytes() for pfad in sorted(daten.rglob("*")) if pfad.is_file()}


# --- Erfolg -------------------------------------------------------------------------------------------------------

def test_der_zustand_zieht_in_den_kreisordner_um_und_die_kennung_wird_neu(anlage):
    vorher = {name: pfad.read_bytes() for name, pfad in anlage.alt.items()}

    assert _lauf(anlage) == migration.MIGRIERT

    backup = json.loads((anlage.neu / zustandspfade.BACKUP).read_text())
    assert backup == _backup(anlage.host, plant_id=NEU[anlage.host])
    for name in INHALT:
        assert (anlage.neu / name).read_bytes() == vorher[name]
    assert not any(pfad.exists() for pfad in anlage.alt.values())
    for name, pfad in anlage.alt.items():
        assert pfad.with_name(f"{pfad.name}.pre-{RELEASE}").read_bytes() == vorher[name]


def test_die_neuen_dateien_sind_privat(anlage):
    _lauf(anlage)

    for name in zustandspfade.DATEIEN:
        assert stat.S_IMODE((anlage.neu / name).stat().st_mode) == 0o600


def test_ein_zweiter_lauf_aendert_nichts(anlage):
    _lauf(anlage)
    danach = _stand(anlage.daten)

    assert _lauf(anlage, "1.2.1") == migration.BEREITS_AKTUELL
    assert _stand(anlage.daten) == danach


def test_ohne_alten_zustand_gibt_es_nichts_zu_tun(tmp_path):
    (tmp_path / "device").mkdir()

    assert migration.migrieren(tmp_path / "device", host=HA, release=RELEASE) == migration.BEREITS_AKTUELL
    assert not (tmp_path / "runtime").exists()


def test_eine_fremde_kennung_bleibt_der_bindung_ans_konto_ueberlassen(anlage):
    anlage.alt[zustandspfade.BACKUP].write_text(json.dumps(_backup(anlage.host, plant_id="{\"a\": 1}")))

    assert _lauf(anlage) == migration.MIGRIERT
    assert json.loads((anlage.neu / zustandspfade.BACKUP).read_text())["plant_id"] == "{\"a\": 1}"


def test_ohne_kennung_in_der_sicherung_braucht_es_keine_konfiguration(anlage):
    anlage.alt[zustandspfade.BACKUP].write_text(json.dumps({"lifetime_writes": 2}))
    (anlage.ordner / "konfiguration.json").unlink()

    assert _lauf(anlage) == migration.MIGRIERT
    assert json.loads((anlage.neu / zustandspfade.BACKUP).read_text()) == {"lifetime_writes": 2}


def test_fehlende_einzeldateien_werden_ausgelassen(anlage):
    anlage.alt[zustandspfade.ENTITLEMENT].unlink()
    anlage.alt[zustandspfade.ROOM_TARGET].unlink()

    assert _lauf(anlage) == migration.MIGRIERT
    assert sorted(pfad.name for pfad in anlage.neu.iterdir()) == [zustandspfade.BACKUP, zustandspfade.FAILSAFE]


def test_der_alte_ort_des_hosts_wird_gelesen_der_des_anderen_nie(anlage):
    andere = HA if anlage.host == GATEWAY else GATEWAY
    for name, pfad in _alte_pfade(anlage.daten, andere).items():
        if pfad not in anlage.alt.values():
            pfad.parent.mkdir(parents=True, exist_ok=True)
            pfad.write_text(json.dumps({"fremd": name}))

    _lauf(anlage)

    for name in zustandspfade.DATEIEN:
        assert "fremd" not in (anlage.neu / name).read_text()


# --- Abweisungen: nichts ist verandert ------------------------------------------------------------------------------

@pytest.mark.parametrize("release", ["", "a b", "x" * 33, "../x", "1.0/0"])
def test_ein_ungueltiges_release_aendert_nichts(anlage, release):
    vorher = _stand(anlage.daten)

    assert _lauf(anlage, release) == migration.RELEASE_UNGUELTIG
    assert _stand(anlage.daten) == vorher


def test_ein_unbekannter_host_und_ein_fehlender_ordner(anlage, tmp_path):
    assert migration.migrieren(anlage.ordner, host="x", release=RELEASE) == migration.HOST_UNBEKANNT
    assert migration.migrieren(tmp_path / "nichts", host=HA, release=RELEASE) == migration.ORDNER_FEHLT


def test_ohne_eingerichtete_konfiguration_bleibt_alles(anlage):
    (anlage.ordner / "konfiguration.json").unlink()
    vorher = _stand(anlage.daten)

    assert _lauf(anlage) == migration.KONFIGURATION_NICHT_EINGERICHTET
    assert _stand(anlage.daten) == vorher


def test_eine_unbrauchbare_konfiguration_bleibt_stehen(anlage):
    (anlage.ordner / "konfiguration.json").write_text(json.dumps({"version": 1, "inhalt": {"anlage": 5}}))
    vorher = _stand(anlage.daten)

    assert _lauf(anlage) == migration.KONFIGURATION_UNGUELTIG
    assert _stand(anlage.daten) == vorher


def test_eine_kennung_ohne_anlagenparameter_scheitert_laut(tmp_path):
    (tmp_path / "device").mkdir()
    bindung = {"treiber": {"id": "simulation", "parameter": {"heizkreis": 1}}, "thermostat": "0x0000000000000001"}
    _konfiguration_ablegen(tmp_path / "device", gateway_konfiguration(bindung=bindung))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "backup.json").write_text(json.dumps(_backup(GATEWAY)))
    vorher = _stand(tmp_path)

    assert migration.migrieren(tmp_path / "device", host=GATEWAY, release=RELEASE) == migration.KENNUNG_UNGUELTIG
    assert _stand(tmp_path) == vorher


@pytest.mark.parametrize("inhalt", ["kaputt{", "[1]", json.dumps({"plant_id": 5}), json.dumps({"plant_id": ""})])
def test_eine_unbrauchbare_sicherung_bleibt_stehen(anlage, inhalt):
    anlage.alt[zustandspfade.BACKUP].write_text(inhalt)
    vorher = _stand(anlage.daten)

    assert _lauf(anlage) == migration.DATEI_UNGUELTIG
    assert _stand(anlage.daten) == vorher


def test_eine_alte_datei_ueber_einen_symlink_wird_abgewiesen(anlage):
    ziel = anlage.daten / "woanders.json"
    ziel.write_text(json.dumps(INHALT[zustandspfade.FAILSAFE]))
    anlage.alt[zustandspfade.FAILSAFE].unlink()
    anlage.alt[zustandspfade.FAILSAFE].symlink_to(ziel)
    vorher = _stand(anlage.daten)

    assert _lauf(anlage) == migration.DATEI_UNGUELTIG
    assert _stand(anlage.daten) == vorher
    assert not anlage.neu.exists()


@pytest.mark.parametrize("name", zustandspfade.DATEIEN)
def test_eine_vorhandene_zieldatei_wird_nie_ueberschrieben(anlage, name):
    anlage.neu.mkdir()
    (anlage.neu / name).write_text("{}")
    vorher = _stand(anlage.daten)

    assert _lauf(anlage) == migration.ZIEL_VORHANDEN
    assert _stand(anlage.daten) == vorher


def test_eine_vorhandene_sicherung_wird_nie_ueberschrieben(anlage):
    vorhandene = anlage.alt[zustandspfade.FAILSAFE]
    vorhandene.with_name(f"{vorhandene.name}.pre-{RELEASE}").write_text("alt")
    vorher = _stand(anlage.daten)

    assert _lauf(anlage) == migration.SICHERUNG_VORHANDEN
    assert _stand(anlage.daten) == vorher
    assert not anlage.neu.exists()


def test_scheitert_das_schreiben_wird_zurueckgerollt(anlage, monkeypatch):
    vorher = _stand(anlage.daten)
    echt = dateien.schreiben
    aufrufe = []

    def schreiben(pfad, daten, *, modus):
        aufrufe.append(pfad)
        if pfad.parent.name == "kreis-1" and pfad.name == zustandspfade.ENTITLEMENT:
            raise OSError("voll")
        echt(pfad, daten, modus=modus)

    monkeypatch.setattr(dateien, "schreiben", schreiben)

    assert _lauf(anlage) == migration.SCHREIBEN_FEHLGESCHLAGEN
    assert any(pfad.parent.name == "kreis-1" for pfad in aufrufe)
    assert _stand(anlage.daten) == vorher
    assert not anlage.neu.exists()


def test_scheitert_das_entfernen_der_alten_dateien_bleibt_der_neue_stand_vollstaendig(anlage, monkeypatch):
    echt = os.unlink

    def unlink(pfad, *args, **kwargs):
        if str(pfad).endswith("room_target.json"):
            raise OSError("gesperrt")
        return echt(pfad, *args, **kwargs)

    monkeypatch.setattr(migration.os, "unlink", unlink)

    assert _lauf(anlage) == migration.SCHREIBEN_FEHLGESCHLAGEN
    assert anlage.alt[zustandspfade.ROOM_TARGET].exists()
    assert all((anlage.neu / name).exists() for name in zustandspfade.DATEIEN)


# --- Aufruf --------------------------------------------------------------------------------------------------------

def test_der_aufruf_gibt_den_code_aus_und_endet_nur_bei_erfolg_mit_0(anlage, capsys):
    argv = ["--device-dir", str(anlage.ordner), "--host", anlage.host, "--release", RELEASE]

    assert migration.main(argv) == 0
    assert capsys.readouterr().out.strip() == migration.MIGRIERT
    assert migration.main(argv) == 0
    assert capsys.readouterr().out.strip() == migration.BEREITS_AKTUELL
    assert migration.main([*argv[:-1], "x y"]) == 1
    assert capsys.readouterr().out.strip() == migration.RELEASE_UNGUELTIG


def test_die_kennung_stammt_aus_dem_modul_der_kennung(anlage):
    """Der neue Wert ist genau die Kennung, die der Host fuer die Konfiguration bildet."""
    _lauf(anlage)
    neu = json.loads((anlage.neu / zustandspfade.BACKUP).read_text())["plant_id"]

    assert neu == NEU[anlage.host]
    if anlage.host == HA:
        assert neu == kennung.aus_ha_kreis("mypyllant", "S", "0")
    else:
        assert neu == kennung.aus_treiber("simulation", BINDUNG_GATEWAY["treiber"]["parameter"])


# --- Orte ---------------------------------------------------------------------------------------------------------

def test_die_alten_orte_gleichen_denen_der_hosts_vor_der_umstellung(tmp_path):
    """Die Orte stehen in der Migration woertlich, weil kein Host sie mehr kennt (der neue Code liest sie nie)."""
    assert migration.alte_pfade(tmp_path, HA) == _alte_pfade(tmp_path, HA)
    assert migration.alte_pfade(tmp_path, GATEWAY) == _alte_pfade(tmp_path, GATEWAY)


def test_das_ziel_ist_der_kreisordner_der_hosts(tmp_path, monkeypatch):
    from heizungsbruecke import config
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")

    assert migration.zielordner(tmp_path) == config.kreis_dir(1)
    assert migration.zielordner(tmp_path) == zustandspfade.kreis_dir(tmp_path / "runtime", 1)


def test_eine_unlesbare_alte_datei_bricht_den_lauf_vor_jeder_aenderung_ab(anlage):
    if os.geteuid() == 0:
        pytest.skip("root liest jede Datei")
    pfad = anlage.alt[zustandspfade.FAILSAFE]
    pfad.chmod(0)
    vorher = {str(p.relative_to(anlage.daten)) for p in anlage.daten.rglob("*") if p.is_file()}
    try:
        assert _lauf(anlage) == migration.DATEI_UNGUELTIG
    finally:
        pfad.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert {str(p.relative_to(anlage.daten)) for p in anlage.daten.rglob("*") if p.is_file()} == vorher


@pytest.mark.parametrize("treiber", [
    {"id": "simulation", "parameter": [1]},
    {"id": 5, "parameter": {"heizkreis": 1}},
    {"id": "simulation"},
    {"parameter": {"heizkreis": 1}},
])
def test_ein_unbrauchbarer_treiber_der_bindung_ergibt_keine_kennung(treiber):
    inhalt = gateway_konfiguration(bindung={"treiber": treiber, "thermostat": "0x0000000000000001"})
    with pytest.raises(kennung.KennungFehler):
        migration._kennungen(inhalt, GATEWAY)


def test_der_aufruf_verlangt_alle_drei_angaben(anlage, capsys):
    argv = ["--device-dir", str(anlage.ordner), "--host", anlage.host, "--release", RELEASE]
    for weg in (0, 2, 4):
        with pytest.raises(SystemExit) as ende:
            migration.main(argv[:weg] + argv[weg + 2:])
        assert ende.value.code == 2
    capsys.readouterr()


def test_die_hilfe_nennt_den_zweck(capsys):
    with pytest.raises(SystemExit) as ende:
        migration.main(["--help"])
    assert ende.value.code == 0
    assert "Einmalige Migration des Zustands" in capsys.readouterr().out
