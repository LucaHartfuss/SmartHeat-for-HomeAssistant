"""Firewall des Gateways (host/nftables.conf, Spec G2b-1 6): die Datei wird als Regeln der Kette `input` gelesen (ohne
Kommentare), nicht als Stichwortsuche. Das ist Konfigurationstext: der Test belegt "so konfiguriert"; ob die Regeln auf
dem Pi wirken und syntaktisch gueltig sind (`nft -c`), prueft nur der Docker-Lauf `install_checks.sh` bzw. das Labor.

`install.sh` ersetzt den Platzhalter `@PILOT_SSH@` per sed durch `tcp dport 22 accept` (mit `--pilot-ssh`) oder durch
nichts; der Test bildet das nach, liest die Regel aber aus `install.sh` und prueft den sed-Ausdruck dort."""
import re
from pathlib import Path

import pytest

HOST = Path(__file__).resolve().parents[1]
NFTABLES = HOST / "nftables.conf"
INSTALL = HOST / "install.sh"

# Genau die vorgesehene Menge (Spec G2b-1 6), als Literal, normalisiert (einfache Leerzeichen).
ERLAUBT = {
    "iif lo accept",
    "ct state established,related accept",
    "ip protocol icmp accept",
    "ip6 nexthdr ipv6-icmp accept",
    "udp dport { 68, 546 } accept",
    "tcp dport 80 accept",
}
PILOT_REGEL = "tcp dport 22 accept"


def _ohne_kommentare(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _ketten(text: str) -> dict[str, list[str]]:
    """Name der Kette -> Anweisungen im Rumpf (Klammern in Mengen `{ 68, 546 }` bleiben beieinander)."""
    text = _ohne_kommentare(text)
    ergebnis: dict[str, list[str]] = {}
    for treffer in re.finditer(r"\bchain\s+(\w+)\s*\{", text):
        tiefe, position, rumpf = 1, treffer.end(), []
        while tiefe:
            zeichen = text[position]
            tiefe += (zeichen == "{") - (zeichen == "}")
            rumpf.append(zeichen)
            position += 1
        anweisungen = re.split(r"[;\n]", "".join(rumpf[:-1]))
        ergebnis[treffer.group(1)] = [" ".join(a.split()) for a in anweisungen if a.strip()]
    return ergebnis


def _eingang(text: str) -> tuple[str, set[str]]:
    """Policy und Regeln (alles ausser `type ...` und `policy ...`) der Kette `input`."""
    anweisungen = _ketten(text)["input"]
    policy = [a.split()[1] for a in anweisungen if a.startswith("policy ")]
    assert len(policy) == 1
    regeln = {a for a in anweisungen if not a.startswith(("type ", "policy "))}
    return policy[0], regeln


def _wirksame_datei(mit_pilot_ssh: bool) -> str:
    """Wie install.sh: `sed -e "s|@PILOT_SSH@|$ssh_rule|"`, ssh_rule leer oder die Regel fuer --pilot-ssh."""
    skript = INSTALL.read_text()
    assert 's|@PILOT_SSH@|$ssh_rule|' in skript and 'local unit ssh_rule=""' in skript
    regel = re.search(r'\[ -n "\$PILOT_KEY" \] && ssh_rule="([^"]*)"', skript)
    assert regel is not None and regel.group(1) == PILOT_REGEL
    return NFTABLES.read_text().replace("@PILOT_SSH@", regel.group(1) if mit_pilot_ssh else "")


def _nur_vorgesehene_regeln(text: str, mit_pilot_ssh: bool = False) -> None:
    policy, regeln = _eingang(text)
    assert policy == "drop"
    assert regeln == ERLAUBT | ({PILOT_REGEL} if mit_pilot_ssh else set())
    assert list(_ketten(text)) == ["input"]


def test_input_chain_drops_by_default_and_opens_exactly_the_intended_services():
    """Policy `drop`; offen nur Loopback, bestehende Verbindungen, ICMP, ICMPv6, DHCP (UDP 68, 546) und TCP 80."""
    policy, regeln = _eingang(_wirksame_datei(mit_pilot_ssh=False))
    assert policy == "drop"
    assert regeln == {
        "iif lo accept", "ct state established,related accept", "ip protocol icmp accept",
        "ip6 nexthdr ipv6-icmp accept", "udp dport { 68, 546 } accept", "tcp dport 80 accept"}
    assert list(_ketten(_wirksame_datei(mit_pilot_ssh=False))) == ["input"]


def test_a_changed_policy_or_an_added_accept_rule_is_found():
    """Gegenprobe: dieselbe Zusicherung faellt bei einer Kopie mit `policy accept` bzw. einer zusaetzlichen Regel."""
    text = _wirksame_datei(mit_pilot_ssh=False)
    _nur_vorgesehene_regeln(text)
    for kopie in (text.replace("policy drop", "policy accept"),
                  text.replace("tcp dport 80 accept", "tcp dport 80 accept\n    tcp dport 1883 accept"),
                  text.replace("tcp dport 80 accept", "tcp dport 80 accept\n    accept"),
                  text.replace("tcp dport 80 accept", "tcp dport { 80, 443 } accept"),
                  text.replace("    tcp dport 80 accept\n", ""),
                  text.replace("  chain input {", "  chain extra {\n    accept\n  }\n  chain input {")):
        with pytest.raises(AssertionError):
            _nur_vorgesehene_regeln(kopie)


@pytest.mark.parametrize("dienst", ["tcp dport 1883 accept", "tcp dport 8883 accept", "tcp dport 8080 accept",
                                    "tcp dport { 1883, 8883 } accept", "iifname \"bus\" accept",
                                    "iifname bus accept", "ip saddr 172.16.0.0/12 accept"])
def test_no_rule_opens_the_bus_mosquitto_or_zigbee2mqtt(dienst):
    """Keine Regel fuer `bus`, Mosquitto (1883, 8883) oder das Frontend von Zigbee2MQTT (8080): die Datei hat keine; eine
    eingefuegte Regel wird gefunden."""
    text = _wirksame_datei(mit_pilot_ssh=False)
    _, regeln = _eingang(text)
    zeichen = " ".join(regeln)
    assert not re.search(r"\b(1883|8883|8080)\b|\bbus\b|\bsaddr\b", zeichen)
    eingefuegt = text.replace("tcp dport 80 accept", f"tcp dport 80 accept\n    {dienst}")
    assert dienst in _eingang(eingefuegt)[1]
    with pytest.raises(AssertionError):
        _nur_vorgesehene_regeln(eingefuegt)


def test_comments_neither_open_nor_close_a_port():
    """Ein Kommentar mit `accept` oder `22` macht den Test weder rot noch gruen; eine auskommentierte Regel ist keine."""
    text = _wirksame_datei(mit_pilot_ssh=False)
    kopie = text.replace("tcp dport 80 accept", "tcp dport 80 accept  # tcp dport 22 accept")
    kopie = kopie.replace("    iif lo accept", "    # tcp dport 22 accept\n    # policy accept\n    iif lo accept")
    assert _eingang(kopie) == _eingang(text)
    _nur_vorgesehene_regeln(kopie)
    auskommentiert = text.replace("    tcp dport 80 accept", "    # tcp dport 80 accept")
    assert "tcp dport 80 accept" not in _eingang(auskommentiert)[1]
    with pytest.raises(AssertionError):
        _nur_vorgesehene_regeln(auskommentiert)


def test_port_22_is_open_only_with_pilot_ssh():
    """Ohne `--pilot-ssh` kein `dport 22` und der Platzhalter ist ersetzt; mit `--pilot-ssh` genau die eine Regel dazu."""
    ohne = _wirksame_datei(mit_pilot_ssh=False)
    assert "@PILOT_SSH@" not in ohne and "22" not in " ".join(_eingang(ohne)[1])
    _nur_vorgesehene_regeln(ohne)
    mit = _wirksame_datei(mit_pilot_ssh=True)
    assert "@PILOT_SSH@" not in mit
    assert _eingang(mit)[1] == ERLAUBT | {PILOT_REGEL}
    _nur_vorgesehene_regeln(mit, mit_pilot_ssh=True)
    with pytest.raises(AssertionError):
        _nur_vorgesehene_regeln(mit)  # Gegenprobe: die Regel fuer 22 faellt ohne Pilot auf


def test_the_placeholder_is_not_replaced_by_anything_else():
    """Bliebe der Platzhalter stehen (sed vergessen), wuerde er als Regel auffallen."""
    roh = NFTABLES.read_text()
    assert roh.count("@PILOT_SSH@") == 1
    with pytest.raises(AssertionError):
        _nur_vorgesehene_regeln(roh)
