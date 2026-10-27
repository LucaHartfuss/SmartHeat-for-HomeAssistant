"""Firewall des Gateways (host/nftables.conf): die Datei wird als Regeln der Kette `input` gelesen (ohne
Kommentare), nicht als Stichwortsuche. Das ist Konfigurationstext: der Test belegt "so konfiguriert"; ob die Regeln auf
dem Pi wirken und syntaktisch gueltig sind (`nft -c`), prueft nur der Docker-Lauf `install_checks.sh` bzw. das Labor.

`install.sh` ersetzt den Platzhalter `@PILOT_SSH@` per sed durch `tcp dport 22 accept` (mit `--pilot-ssh`) oder durch
nichts; der Test bildet das nach, liest die Regel aber aus `install.sh` und prueft den sed-Ausdruck dort."""
import json
import re
from pathlib import Path

import pytest

HOST = Path(__file__).resolve().parents[1]
NFTABLES = HOST / "nftables.conf"
INSTALL = HOST / "install.sh"
DOCKER_DAEMON = HOST / "docker-daemon.json"

# Genau die vorgesehene Menge, als Literal, normalisiert (einfache Leerzeichen).
ERLAUBT = {
    "iif lo accept",
    "ct state established,related accept",
    "ip protocol icmp accept",
    "ip6 nexthdr ipv6-icmp accept",
    "udp dport { 68, 546 } accept",
    "tcp dport 80 accept",
}
PILOT_REGEL = "tcp dport 22 accept"


KOPF = "type filter hook input priority 0"
KETTEN = [("smartheat", "input"), ("smartheat", "prerouting")]
# prerouting (GW-Weiterleitung): Kopf ohne Policy (Standard accept), genau diese Regeln in dieser Reihenfolge
VORROUTING_KOPF = "type filter hook prerouting priority raw"
VORROUTING_REGELN = ['iifname != "lo" ip daddr 127.0.0.0/8 drop', 'iifname "docker0" return', 'iifname "br-*" return',
                     "fib daddr type local return", "ip daddr 172.30.0.0/16 drop"]


def _ohne_kommentare(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _block(text: str, anfang: int) -> str:
    """Rumpf ab der Position hinter einer oeffnenden Klammer bis zur passenden schliessenden."""
    tiefe, position = 1, anfang
    while tiefe:
        tiefe += (text[position] == "{") - (text[position] == "}")
        position += 1
    return text[anfang:position - 1]


def _ketten(text: str) -> list[tuple[str, str, list[str]]]:
    """(Tabelle, Kette, Anweisungen) in Dateireihenfolge; gleichnamige Ketten verschiedener Tabellen bleiben getrennt.
    Klammern in Mengen `{ 68, 546 }` bleiben beieinander."""
    text = _ohne_kommentare(text)
    ergebnis = []
    for tabelle in re.finditer(r"\btable\s+\w+\s+(\w+)\s*\{", text):
        rumpf = _block(text, tabelle.end())
        for kette in re.finditer(r"\bchain\s+(\w+)\s*\{", rumpf):
            anweisungen = re.split(r"[;\n]", _block(rumpf, kette.end()))
            ergebnis.append((tabelle.group(1), kette.group(1), [" ".join(a.split()) for a in anweisungen if a.strip()]))
    return ergebnis


def _eingang(text: str) -> tuple[str, str, set[str]]:
    """Kopfzeile (`type ... hook ... priority ...`), Policy und Regeln der einen Kette, die am Hook `input` haengt."""
    am_hook = [a for _, _, a in _ketten(text) if any(re.match(r"type \w+ hook input\b", x) for x in a)]
    assert len(am_hook) == 1, "genau eine Kette am Hook input"
    anweisungen = am_hook[0]
    kopf = [a for a in anweisungen if a.startswith("type ")]
    policy = [a.split()[1] for a in anweisungen if a.startswith("policy ")]
    assert len(kopf) == 1 and len(policy) == 1
    return kopf[0], policy[0], {a for a in anweisungen if not a.startswith(("type ", "policy "))}


def _wirksame_datei(mit_pilot_ssh: bool) -> str:
    """Wie install.sh: `sed -e "s|@PILOT_SSH@|$ssh_rule|"`, ssh_rule leer oder die Regel fuer --pilot-ssh."""
    skript = INSTALL.read_text()
    assert 's|@PILOT_SSH@|$ssh_rule|' in skript and 'local unit ssh_rule=""' in skript
    regel = re.search(r'\[ -n "\$PILOT_KEY" \] && ssh_rule="([^"]*)"', skript)
    assert regel is not None and regel.group(1) == PILOT_REGEL
    return NFTABLES.read_text().replace("@PILOT_SSH@", regel.group(1) if mit_pilot_ssh else "")


def _nur_vorgesehene_regeln(text: str, mit_pilot_ssh: bool = False) -> None:
    kopf, policy, regeln = _eingang(text)
    assert kopf == KOPF, "Kopf der Kette input"
    assert policy == "drop", "Policy der Kette input"
    assert regeln == ERLAUBT | ({PILOT_REGEL} if mit_pilot_ssh else set()), "Regelmenge der Kette input"
    assert [(t, n) for t, n, _ in _ketten(text)] == KETTEN, "Ketten der Datei"


def test_input_chain_drops_by_default_and_opens_exactly_the_intended_services():
    """Kette `input` haengt am Hook `input` (Filter, Prioritaet 0) mit Policy `drop`; offen nur Loopback, bestehende
    Verbindungen, ICMP, ICMPv6, DHCP (UDP 68, 546) und TCP 80."""
    text = _wirksame_datei(mit_pilot_ssh=False)
    kopf, policy, regeln = _eingang(text)
    assert kopf == "type filter hook input priority 0"
    assert policy == "drop"
    assert regeln == {
        "iif lo accept", "ct state established,related accept", "ip protocol icmp accept",
        "ip6 nexthdr ipv6-icmp accept", "udp dport { 68, 546 } accept", "tcp dport 80 accept"}
    assert [(t, n) for t, n, _ in _ketten(text)] == KETTEN


_ABWEICHUNGEN = {
    "policy accept": (lambda t: t.replace("policy drop", "policy accept"), "Policy der Kette"),
    "hook forward": (lambda t: t.replace("hook input", "hook forward"), "Hook input"),
    "andere Prioritaet": (lambda t: t.replace("priority 0", "priority 10"), "Kopf der Kette"),
    "nackter accept": (lambda t: t.replace("tcp dport 80 accept", "tcp dport 80 accept\n    accept"), "Regelmenge"),
    "Regel 1883": (lambda t: t.replace("tcp dport 80 accept", "tcp dport 80 accept\n    tcp dport 1883 accept"),
                   "Regelmenge"),
    "Menge 80 und 443": (lambda t: t.replace("tcp dport 80 accept", "tcp dport { 80, 443 } accept"), "Regelmenge"),
    "Regel 80 fehlt": (lambda t: t.replace("    tcp dport 80 accept\n", ""), "Regelmenge"),
    "zweite Kette": (lambda t: t.replace("  chain input {", "  chain extra {\n    accept\n  }\n  chain input {"),
                     "Ketten der Datei"),
    "gleichnamige Kette in zweiter Tabelle": (
        lambda t: t.replace("  chain input {", "  chain input {\n    type filter hook input priority 0; policy accept;\n"
                            "    accept\n  }\n}\ntable inet zweite {\n  chain input {", 1), "Hook input"),
}


def test_the_intended_chain_passes_the_assertion():
    _nur_vorgesehene_regeln(_wirksame_datei(mit_pilot_ssh=False))


@pytest.mark.parametrize("name", list(_ABWEICHUNGEN))
def test_a_changed_hook_policy_or_rule_is_found(name):
    """Gegenprobe: dieselbe Zusicherung faellt bei jeder Kopie mit einer Abweichung, und zwar an der gemeinten Stelle."""
    text = _wirksame_datei(mit_pilot_ssh=False)
    aendern, meldung = _ABWEICHUNGEN[name]
    kopie = aendern(text)
    assert kopie != text
    with pytest.raises(AssertionError, match=meldung):
        _nur_vorgesehene_regeln(kopie)


@pytest.mark.parametrize("dienst", ["tcp dport 1883 accept", "tcp dport 8883 accept", "tcp dport 8080 accept",
                                    "tcp dport { 1883, 8883 } accept", "iifname \"bus\" accept",
                                    "iifname bus accept", "ip saddr 172.16.0.0/12 accept"])
def test_no_rule_opens_the_bus_mosquitto_or_zigbee2mqtt(dienst):
    """Keine Regel fuer `bus`, Mosquitto (1883, 8883) oder das Frontend von Zigbee2MQTT (8080): die Datei hat keine; eine
    eingefuegte Regel wird gefunden."""
    text = _wirksame_datei(mit_pilot_ssh=False)
    regeln = _eingang(text)[2]
    zeichen = " ".join(regeln)
    assert not re.search(r"\b(1883|8883|8080)\b|\bbus\b|\bsaddr\b", zeichen)
    eingefuegt = text.replace("tcp dport 80 accept", f"tcp dport 80 accept\n    {dienst}")
    assert dienst in _eingang(eingefuegt)[2]
    with pytest.raises(AssertionError, match="Regelmenge"):
        _nur_vorgesehene_regeln(eingefuegt)


def test_comments_neither_open_nor_close_a_port():
    """Ein Kommentar mit `accept` oder `22` macht den Test weder rot noch gruen; eine auskommentierte Regel ist keine."""
    text = _wirksame_datei(mit_pilot_ssh=False)
    kopie = text.replace("tcp dport 80 accept", "tcp dport 80 accept  # tcp dport 22 accept")
    kopie = kopie.replace("    iif lo accept", "    # tcp dport 22 accept\n    # policy accept\n    iif lo accept")
    assert kopie != text
    assert _eingang(kopie) == _eingang(text)
    _nur_vorgesehene_regeln(kopie)
    auskommentiert = text.replace("    tcp dport 80 accept", "    # tcp dport 80 accept")
    assert auskommentiert != text
    assert "tcp dport 80 accept" not in _eingang(auskommentiert)[2]
    with pytest.raises(AssertionError, match="Regelmenge"):
        _nur_vorgesehene_regeln(auskommentiert)


def test_port_22_is_open_only_with_pilot_ssh():
    """Ohne `--pilot-ssh` kein `dport 22` und der Platzhalter ist ersetzt; mit `--pilot-ssh` genau die eine Regel dazu."""
    ohne = _wirksame_datei(mit_pilot_ssh=False)
    assert "@PILOT_SSH@" not in ohne and "22" not in " ".join(_eingang(ohne)[2])
    _nur_vorgesehene_regeln(ohne)
    mit = _wirksame_datei(mit_pilot_ssh=True)
    assert "@PILOT_SSH@" not in mit
    assert _eingang(mit)[2] == ERLAUBT | {PILOT_REGEL}
    _nur_vorgesehene_regeln(mit, mit_pilot_ssh=True)
    with pytest.raises(AssertionError, match="Regelmenge"):
        _nur_vorgesehene_regeln(mit)  # Gegenprobe: die Regel fuer 22 faellt ohne Pilot auf


def test_the_placeholder_is_not_replaced_by_anything_else():
    """Bliebe der Platzhalter stehen (sed vergessen), wuerde er als Regel auffallen."""
    roh = NFTABLES.read_text()
    assert roh.count("@PILOT_SSH@") == 1
    with pytest.raises(AssertionError, match="Regelmenge"):
        _nur_vorgesehene_regeln(roh)


def _vorrouting(text: str) -> list[str]:
    """Anweisungen der einen Kette `prerouting` der Tabelle smartheat (Kopf zuerst, dann die Regeln in Dateireihenfolge)."""
    gefunden = [a for t, n, a in _ketten(text) if (t, n) == ("smartheat", "prerouting")]
    assert len(gefunden) == 1, "genau eine Kette prerouting"
    return gefunden[0]


def _pool() -> dict:
    """Der eine Pool von `default-address-pools` in docker-daemon.json: Basis und Groesse."""
    pools = json.loads(DOCKER_DAEMON.read_text())["default-address-pools"]
    assert len(pools) == 1, "genau ein Adresspool"
    return pools[0]


def test_prerouting_drops_loopback_and_container_addresses_from_outside_the_host():
    """Raw-Kette vor Conntrack und DNAT: Ziel 127.0.0.0/8 nur ueber `lo`, Ziel im Docker-Pool nur aus `docker0`/`br-*`
    (zwei `return`-Regeln vor dem `drop`, keine Menge mit Platzhalter); Adressen des Hosts selbst (`fib daddr type local`,
    R27) gehen vor dem `drop` weiter: auch ein Gateway in einem LAN aus 172.30.0.0/16 erreicht seinen Broker.
    Alles andere (DNAT auf veroeffentlichte Ports laeuft ueber die Host-Adresse) bleibt unberuehrt: keine Policy, kein accept."""
    for mit_pilot_ssh in (False, True):
        anweisungen = _vorrouting(_wirksame_datei(mit_pilot_ssh))
        assert anweisungen == [VORROUTING_KOPF, *VORROUTING_REGELN]


def test_the_prerouting_range_equals_the_docker_address_pool():
    """Der Bereich der zweiten Regel ist der Pool, aus dem Docker alle Compose-Netze vergibt (sonst waere ein Netz ungeschuetzt
    oder die Regel schnitte fremden Verkehr ab). Groesse 24: je Compose-Netz ein /24 aus dem /16."""
    pool = _pool()
    assert pool == {"base": "172.30.0.0/16", "size": 24}
    regel = _vorrouting(NFTABLES.read_text())[-1]
    assert re.fullmatch(r"ip daddr (\S+) drop", regel).group(1) == pool["base"]
    assert json.loads(DOCKER_DAEMON.read_text())["features"] == {"containerd-snapshotter": True}  # Speicherweg bleibt


_VORROUTING_ABWEICHUNGEN = {
    "Prioritaet filter": lambda t: t.replace("priority raw", "priority filter"),
    "Hook input statt prerouting": lambda t: t.replace("hook prerouting", "hook input"),
    "Pool geaendert": lambda t: t.replace("172.30.0.0/16", "172.31.0.0/16"),
    "br-Bruecken nicht ausgenommen": lambda t: t.replace('    iifname "br-*" return\n', ""),
    "docker0 nicht ausgenommen": lambda t: t.replace('    iifname "docker0" return\n', ""),
    "Ausnahmen hinter dem drop": lambda t: t.replace(
        '    iifname "docker0" return\n    iifname "br-*" return\n    fib daddr type local return\n    ip daddr 172.30.0.0/16 drop\n',
        '    ip daddr 172.30.0.0/16 drop\n    iifname "docker0" return\n    iifname "br-*" return\n    fib daddr type local return\n'),
    "Menge mit Platzhalter": lambda t: t.replace('    iifname "docker0" return\n    iifname "br-*" return\n',
                                                 '    iifname != { "docker0", "br-*" } return\n'),
    "Loopback-Regel entfernt": lambda t: t.replace('    iifname != "lo" ip daddr 127.0.0.0/8 drop\n', ""),
    "Loopback ueber alle Schnittstellen": lambda t: t.replace('iifname != "lo" ip daddr 127.0.0.0/8', "ip daddr 127.0.0.0/8"),
    "Pool-Regel entfernt": lambda t: t.replace("    ip daddr 172.30.0.0/16 drop\n", ""),
    "eigene Adressen nicht ausgenommen": lambda t: t.replace("    fib daddr type local return\n", ""),
    "eigene Adressen hinter dem drop": lambda t: t.replace("    fib daddr type local return\n    ip daddr 172.30.0.0/16 drop\n",
                                                           "    ip daddr 172.30.0.0/16 drop\n    fib daddr type local return\n"),
    "eigene Adressen vor der Loopback-Regel": lambda t: t.replace("    fib daddr type local return\n", "").replace(
        '    iifname != "lo" ip daddr 127.0.0.0/8 drop\n', '    fib daddr type local return\n    iifname != "lo" ip daddr 127.0.0.0/8 drop\n'),
    "accept statt drop": lambda t: t.replace("172.30.0.0/16 drop", "172.30.0.0/16 accept"),
    "zusaetzliche Regel": lambda t: t.replace("172.30.0.0/16 drop", "172.30.0.0/16 drop\n    tcp dport 8081 accept"),
}


@pytest.mark.parametrize("name", list(_VORROUTING_ABWEICHUNGEN))
def test_a_changed_prerouting_rule_is_found(name):
    """Gegenprobe: jede Abweichung veraendert die Kette (die Zusicherung oben faellt), keine ist ein No-op."""
    text = _wirksame_datei(mit_pilot_ssh=False)
    kopie = _VORROUTING_ABWEICHUNGEN[name](text)
    assert kopie != text
    if name == "Hook input statt prerouting":  # die Kette heisst noch so, haengt aber am Hook input: input-Zusicherung faellt
        with pytest.raises(AssertionError, match="genau eine Kette am Hook input"):
            _eingang(kopie)
        return
    assert _vorrouting(kopie) != [VORROUTING_KOPF, *VORROUTING_REGELN]

