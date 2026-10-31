from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes

import ca
import roots_pruefen
from smartheat_host import kette

PASSPHRASE = "passphrase-fuer-den-test"
HOST = Path(__file__).resolve().parents[2] / "host"
PLATZHALTER = "# SMARTHEAT-PLATZHALTER - noch keine echte Root\n"


def _abdruck(pem: str) -> str:
    return x509.load_pem_x509_certificate(pem.encode()).fingerprint(hashes.SHA256()).hex()


@pytest.fixture(scope="module")
def echte_roots(tmp_path_factory):
    """Zwei Produktions-Roots (ohne "(Test)" im Namen) als PEM-Text."""
    texte = []
    for nummer in (1, 2):
        ziel = tmp_path_factory.mktemp(f"echt{nummer}")
        ca.root_anlegen(ziel, PASSPHRASE)
        texte.append((ziel / "root.crt").read_text())
    return texte


@pytest.fixture(scope="module")
def test_roots(tmp_path_factory):
    texte = []
    for nummer in (1, 2):
        ziel = tmp_path_factory.mktemp(f"test{nummer}")
        ca.test_kette_anlegen(ziel)
        texte.append((ziel / "root.crt").read_text())
    return texte


def _pruefe(tmp_path, modus, liste, roots, capsys):
    liste_datei, roots_datei = tmp_path / "liste.sha256", tmp_path / "roots.pem"
    liste_datei.write_text(liste)
    roots_datei.write_text(roots)
    code = roots_pruefen.main([modus, "--liste", str(liste_datei), str(roots_datei)])
    return code, capsys.readouterr().err


def _liste(*roots: str, name="Setwell Release Root") -> str:
    return "# Kommentar\n\n" + "".join(f"{_abdruck(r)}  {name}\n" for r in roots)


def test_production_root_in_list_is_accepted(tmp_path, echte_roots, capsys):
    code, err = _pruefe(tmp_path, "produktion", _liste(echte_roots[0]), echte_roots[0], capsys)
    assert code == 0 and err == ""


def test_production_foreign_root_is_rejected(tmp_path, echte_roots, capsys):
    code, err = _pruefe(tmp_path, "produktion", _liste(echte_roots[0]), echte_roots[1], capsys)
    assert code == 1
    assert err.startswith("FEHLER:") and _abdruck(echte_roots[1]) in err and _abdruck(echte_roots[0]) not in err


def test_production_test_root_is_rejected_even_when_listed(tmp_path, test_roots, capsys):
    code, err = _pruefe(tmp_path, "produktion", _liste(test_roots[0]), test_roots[0], capsys)
    assert code == 1 and "(Test)" in err


def test_production_bundle_with_one_foreign_root_is_rejected(tmp_path, echte_roots, capsys):
    code, err = _pruefe(tmp_path, "produktion", _liste(echte_roots[0]), echte_roots[0] + echte_roots[1], capsys)
    assert code == 1
    assert _abdruck(echte_roots[1]) in err and _abdruck(echte_roots[0]) not in err


def test_production_bundle_with_all_roots_listed_is_accepted(tmp_path, echte_roots, capsys):
    code, _ = _pruefe(tmp_path, "produktion", _liste(*echte_roots), echte_roots[0] + echte_roots[1], capsys)
    assert code == 0


def test_production_placeholder_list_is_rejected(tmp_path, echte_roots, capsys):
    code, err = _pruefe(tmp_path, "produktion", PLATZHALTER, echte_roots[0], capsys)
    assert code == 1 and "AP-04-Root" in err and "Runbook 13.2" in err


def test_production_placeholder_wins_over_a_matching_line(tmp_path, echte_roots, capsys):
    code, _ = _pruefe(tmp_path, "produktion", PLATZHALTER + _liste(echte_roots[0]), echte_roots[0], capsys)
    assert code == 1


def test_production_empty_list_is_rejected(tmp_path, echte_roots, capsys):
    code, err = _pruefe(tmp_path, "produktion", "# nur Kommentar\n\n", echte_roots[0], capsys)
    assert code == 1 and "leer" in err


@pytest.mark.parametrize("zeile", ["nicht-hex  Name", "abcd  Name", "ab" * 32 + "\n", "ab" * 33 + "  Name", "zz" * 32 + "  Name"])
@pytest.mark.parametrize("modus", ["produktion", "labor"])
def test_broken_line_is_rejected_in_both_modes(tmp_path, echte_roots, test_roots, capsys, modus, zeile):
    roots = echte_roots[0] if modus == "produktion" else test_roots[0]
    code, err = _pruefe(tmp_path, modus, _liste(roots) + zeile + "\n", roots, capsys)
    assert code == 1 and "Zeile" in err


def test_hex_in_capitals_is_accepted(tmp_path, echte_roots, capsys):
    liste = f"{_abdruck(echte_roots[0]).upper()}  Setwell Release Root\n"
    code, _ = _pruefe(tmp_path, "produktion", liste, echte_roots[0], capsys)
    assert code == 0


def test_labor_production_root_is_rejected(tmp_path, echte_roots, capsys):
    code, err = _pruefe(tmp_path, "labor", _liste(echte_roots[0]), echte_roots[0], capsys)
    assert code == 1 and "(Test)" in err


def test_labor_test_root_in_the_list_is_rejected(tmp_path, test_roots, capsys):
    code, err = _pruefe(tmp_path, "labor", _liste(test_roots[0]), test_roots[0], capsys)
    assert code == 1 and _abdruck(test_roots[0]) in err


def test_labor_test_root_with_a_foreign_list_is_accepted(tmp_path, echte_roots, test_roots, capsys):
    code, err = _pruefe(tmp_path, "labor", _liste(echte_roots[0]), test_roots[0] + test_roots[1], capsys)
    assert code == 0 and err == ""


def test_labor_test_root_with_a_placeholder_list_is_accepted(tmp_path, test_roots, capsys):
    code, _ = _pruefe(tmp_path, "labor", PLATZHALTER, test_roots[0], capsys)
    assert code == 0


def test_labor_placeholder_line_does_not_clear_the_real_entries(tmp_path, test_roots, capsys):
    code, err = _pruefe(tmp_path, "labor", PLATZHALTER + _liste(test_roots[0]), test_roots[0], capsys)
    assert code == 1 and _abdruck(test_roots[0]) in err


def test_labor_mixed_bundle_is_rejected(tmp_path, echte_roots, test_roots, capsys):
    code, _ = _pruefe(tmp_path, "labor", PLATZHALTER, test_roots[0] + echte_roots[0], capsys)
    assert code == 1


@pytest.mark.parametrize("modus", ["produktion", "labor"])
def test_pem_with_a_broken_block_is_a_load_error_for_the_whole_bundle(tmp_path, echte_roots, test_roots, capsys, modus):
    roots = echte_roots[0] if modus == "produktion" else test_roots[0]
    kaputt = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
    code, err = _pruefe(tmp_path, modus, _liste(roots), roots + kaputt, capsys)
    assert code == 1 and err.startswith("FEHLER:") and "Root" in err


def test_pem_with_text_before_the_certificate_is_read_like_on_the_device(tmp_path, echte_roots, capsys):
    pem = "Notiz\n" + echte_roots[0]
    assert len(kette.load_roots(pem)) == 1  # derselbe Lader wie am Geraet: Text davor ist dort ebenso harmlos
    code, _ = _pruefe(tmp_path, "produktion", _liste(echte_roots[0]), pem, capsys)
    assert code == 0


def test_unreadable_files_are_an_error(tmp_path, capsys):
    code = roots_pruefen.main(["labor", "--liste", str(tmp_path / "fehlt"), str(tmp_path / "fehlt.pem")])
    assert code == 1 and "FEHLER:" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["produktion"], ["egal", "--liste", "a", "b"], ["labor", "--liste", "a"],
                                  ["labor", "b"]])
def test_wrong_usage_exits_with_2(argv):
    with pytest.raises(SystemExit) as ende:
        roots_pruefen.main(argv)
    assert ende.value.code == 2


def _fingerabdruecke_pem(text: str) -> set[str]:
    return {r.fingerprint(hashes.SHA256()).hex() for r in kette.load_roots(text)}


def _fingerabdruecke_liste(text: str) -> set[str]:
    return {zeile.split()[0].lower() for zeile in text.splitlines() if zeile.strip() and not zeile.lstrip().startswith("#")}


def test_repo_pem_and_list_are_both_placeholders_or_carry_the_same_roots():
    pem = (HOST / "release-roots.pem").read_text()
    liste = (HOST / "release-roots.sha256").read_text()
    if kette.PLACEHOLDER_MARKER in pem or kette.PLACEHOLDER_MARKER in liste:
        assert kette.PLACEHOLDER_MARKER in pem and kette.PLACEHOLDER_MARKER in liste
    else:
        assert _fingerabdruecke_pem(pem) == _fingerabdruecke_liste(liste) != set()
