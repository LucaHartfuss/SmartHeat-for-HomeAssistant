"""Die Units des Gateways (gateway/host/systemd): eine Quelle fuer die Umgebung (gateway.env unter /opt/smartheat) und die
Reihenfolge beim Start. Der Updater darf nach einem Systemwechsel nicht hinter dem Laden der Container-Archive warten (R10):
die lokale Gesundheitsfrist laeuft ab seinem Start, der Frist-Timer schaltet 20 min nach dem Boot zurueck."""
import re
from pathlib import Path

import pytest

SYSTEMD = Path(__file__).resolve().parents[1] / "systemd"
UNITS = sorted(p.name for p in SYSTEMD.glob("smartheat-*.service"))
GATEWAY_ENV = "EnvironmentFile=/opt/smartheat/gateway.env"
MIT_ROOT = ("smartheat-ablage", "smartheat-werk", "smartheat-firstboot", "smartheat-updater", "smartheat-hoststatus",
            "smartheat-led", "smartheat-waechter", "smartheat-knopf")


def _eintraege(name: str) -> dict[str, list[str]]:
    """Schluessel -> alle Werte der Unit (Leerraum getrennt, ueber mehrere Zeilen), Kommentare ausgenommen."""
    werte: dict[str, list[str]] = {}
    for zeile in (SYSTEMD / name).read_text().splitlines():
        treffer = re.fullmatch(r"([A-Za-z]+)=(.*)", zeile.strip())
        if treffer and not zeile.lstrip().startswith("#"):
            werte.setdefault(treffer.group(1), []).extend(treffer.group(2).split())
    return werte


def _nach(name: str, gesehen: set[str] | None = None) -> set[str]:
    """Alle Units dieses Ordners, hinter denen `name` (ueber After= oder fremdes Before=) beim Start wartet, ueber alle Stufen."""
    gesehen = set() if gesehen is None else gesehen
    direkt = set(_eintraege(name).get("After", []))
    for andere in UNITS:
        if name in _eintraege(andere).get("Before", []):
            direkt.add(andere)
    for unit in direkt & set(UNITS):
        if unit not in gesehen:
            gesehen.add(unit)
            _nach(unit, gesehen)
    return gesehen | direkt


def test_der_updater_wartet_nicht_hinter_dem_laden_der_archive():
    wartet = _nach("smartheat-updater.service")
    assert "smartheat-firstboot.service" not in wartet
    assert {u for u in wartet if u in UNITS} == {"smartheat-ablage.service"}  # nur die schnelle, lokale Ablage
    assert "smartheat-ablage.service" in _eintraege("smartheat-updater.service")["Wants"]


@pytest.mark.parametrize("name", ["smartheat-updater.service", "smartheat-hoststatus.service", "smartheat-firstboot.service",
                                  "smartheat-waechter.service"])
def test_eine_gescheiterte_ablage_haelt_updater_hoststatus_und_erststart_nicht_auf(name):
    """Die Ablage ist nur Wants=+After=: scheitert sie (ein Eintrag des Containers unter bus/), laufen die Dienste trotzdem,
    sonst waere der Updater-Weg fuer immer blockiert."""
    eintraege = _eintraege(name)
    assert "smartheat-ablage.service" in eintraege["Wants"] and "smartheat-ablage.service" in eintraege["After"]
    for art in ("Requires", "BindsTo", "PartOf", "Requisite"):
        assert "smartheat-ablage.service" not in eintraege.get(art, []), (name, art)


def test_nichts_wartet_auf_den_start_der_archive():
    """smartheat-firstboot hat kein Before= und keine Unit ordnet sich dahinter (After=): sonst wartete sie auf docker load."""
    assert "Before" not in _eintraege("smartheat-firstboot.service")
    for name in UNITS:
        assert "smartheat-firstboot.service" not in _eintraege(name).get("After", []), name
        assert "smartheat-firstboot.service" not in _eintraege(name).get("Requires", []), name


def test_die_ablage_ist_schnell_und_laeuft_vor_den_anderen_in_der_kette():
    ablage = _eintraege("smartheat-ablage.service")
    assert ablage["Type"] == ["oneshot"] and ablage["RemainAfterExit"] == ["yes"]
    assert "Restart" not in ablage and "After" not in ablage and "Before" not in ablage  # keine eigene Wartekette
    assert ablage["ExecStart"] == ["/usr/bin/python3", "-m", "smartheat_host.firstboot", "ablage"]
    for name in ("smartheat-updater.service", "smartheat-hoststatus.service", "smartheat-firstboot.service",
                 "smartheat-led.service", "smartheat-werk.service"):
        assert "smartheat-ablage.service" in _eintraege(name)["After"], name


def test_der_waechter_wartet_nur_auf_docker_und_ablage_und_lebt_immer():
    """Wie der Updater nie hinter dem Laden der Archive; Restart=always, damit ein Absturz ihn nicht dauerhaft beendet."""
    waechter = _eintraege("smartheat-waechter.service")
    assert set(waechter["After"]) == {"docker.service", "smartheat-ablage.service"}
    assert "smartheat-firstboot.service" not in _nach("smartheat-waechter.service")
    assert waechter["ExecStart"] == ["/usr/bin/python3", "-m", "smartheat_host.waechter"]
    assert waechter["Restart"] == ["always"] and waechter["WantedBy"] == ["multi-user.target"]
    assert set(waechter["Wants"]) == {"docker.service", "smartheat-ablage.service"}
    assert "Requires" not in waechter  # Docker oder Ablage in Stoerung: der Waechter bleibt eine eigene Barriere


def test_der_waechter_schreibt_nur_die_sperrdatei_und_nutzt_den_docker_socket():
    waechter = _eintraege("smartheat-waechter.service")
    assert waechter["ReadWritePaths"] == ["@ROOT@/updater", "/run/docker.sock"]
    assert waechter["ProtectSystem"] == ["strict"] and waechter["NoNewPrivileges"] == ["yes"]


def test_der_updater_wartet_nur_auf_docker_netz_und_ablage():
    assert set(_eintraege("smartheat-updater.service")["After"]) == {"network-online.target", "docker.service",
                                                                      "smartheat-ablage.service"}


def test_die_werk_unit_laeuft_vor_dem_netz_nach_dem_einhaengen_von_werk():
    werk = _eintraege("smartheat-werk.service")
    assert {"werk.mount", "dbus.service", "systemd-hostnamed.service"} <= set(werk["After"])
    assert werk["Before"] == ["network-pre.target"] and "network-pre.target" in werk["Wants"]
    assert werk["ExecStart"] == ["/usr/bin/python3", "-m", "smartheat_host.werk", "start"]
    assert werk["ReadWritePaths"] == ["@ROOT@/host"]
    assert "Requires" not in werk or "werk.mount" not in werk["Requires"]  # eine fehlende Partition haelt nichts auf
    assert "RequiresMountsFor" not in werk or "/werk" not in werk["RequiresMountsFor"]


def test_der_erststart_laedt_mit_docker_und_bleibt_bei_fehlern_im_wiederholen():
    start = _eintraege("smartheat-firstboot.service")
    assert start["Requires"] == ["docker.service"] and start["Wants"] == ["smartheat-ablage.service"]
    assert start["ExecStart"] == ["/usr/bin/python3", "-m", "smartheat_host.firstboot", "start"]
    assert start["Restart"] == ["on-failure"] and start["RestartSec"] == ["30"]
    assert "ConditionDirectoryNotEmpty" not in start  # jeder Start ruft compose.up (GPC31), nicht nur der erste


@pytest.mark.parametrize("name", [f"{unit}.service" for unit in MIT_ROOT])
def test_eine_quelle_fuer_die_umgebung(name):
    text = (SYSTEMD / name).read_text()
    assert GATEWAY_ENV in text.splitlines()
    assert "Environment=SHG_ROOT" not in text and "host/gateway.env" not in text


@pytest.mark.parametrize("name", [f"{unit}.service" for unit in MIT_ROOT])
def test_der_root_ordner_muss_eingehaengt_sein(name):
    assert "@ROOT@" in _eintraege(name)["RequiresMountsFor"]


def test_keine_unit_nennt_host_gateway_env_mehr():
    for pfad in SYSTEMD.rglob("*"):  # auch die Drop-ins (emergency.service.d, rescue.service.d)
        if pfad.is_file():
            assert "host/gateway.env" not in pfad.read_text(), pfad.name


def test_der_frist_timer_hat_ein_install_und_laeuft_nach_20_minuten():
    timer = (SYSTEMD / "smartheat-probestart-frist.timer").read_text()
    assert "OnBootSec=20min" in timer and "WantedBy=timers.target" in timer


def test_der_knopf_dienst_liest_das_geraet_und_schreibt_nur_host_und_run():
    """GP8: schreibt host/knopf.json (ReadWritePaths) und /run/smartheat/led_knopf.json (RuntimeDirectory, bleibt bei einem
    Neustart des Dienstes erhalten, die LED liest es); sonst nur lesend. Keine Abhaengigkeit von Docker oder Netz."""
    knopf = _eintraege("smartheat-knopf.service")
    assert knopf["ExecStart"] == ["/usr/bin/python3", "-m", "smartheat_host.knopf"]
    assert knopf["Restart"] == ["always"] and knopf["WantedBy"] == ["multi-user.target"]
    assert knopf["ReadWritePaths"] == ["@ROOT@/host"]
    assert knopf["RuntimeDirectory"] == ["smartheat"] and knopf["RuntimeDirectoryPreserve"] == ["yes"]
    assert knopf["ProtectSystem"] == ["strict"] and knopf["NoNewPrivileges"] == ["yes"]
    assert set(knopf["After"]) == {"local-fs.target", "smartheat-ablage.service"}
    assert "Requires" not in knopf and "docker.service" not in _nach("smartheat-knopf.service")
    assert "smartheat-firstboot.service" not in _nach("smartheat-knopf.service")


def test_logind_ignoriert_den_einschaltknopf():
    """Fehlt der Knopf-Dienst (Absturz, Neustart), faehrt ein Druck das Geraet nicht herunter."""
    text = (SYSTEMD.parent / "logind" / "smartheat.conf").read_text().splitlines()
    assert text[0] == "[Login]"
    assert "HandlePowerKey=ignore" in text and "HandlePowerKeyLongPress=ignore" in text
