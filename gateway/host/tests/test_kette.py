import base64
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID
from kette_helper import BASIS, FORMAT, release, root, roots_pem, signiere

from smartheat_host import kette

DATA = b'{"version": "0.2.0"}'
VON = BASIS + timedelta(days=1)
BIS = BASIS + timedelta(days=91)
JETZT = BASIS + timedelta(days=30)


def _kette(**kwargs):
    """Root plus Release-Zertifikat; Rueckgabe (Root-Zertifikat, Release-Schluessel, Release-Zertifikat)."""
    root_key, root_cert = root()
    key, cert = release(root_key, root_cert, von=kwargs.pop("von", VON), bis=kwargs.pop("bis", BIS), **kwargs)
    return root_cert, key, cert


def _pruefe(root_cert, key, cert, **kwargs):
    return kette.verify([root_cert], DATA, signiere(key, cert, DATA), kwargs.pop("jetzt", JETZT), **kwargs)


def test_gueltige_signatur_liefert_das_release_zertifikat():
    root_cert, key, cert = _kette()
    assert _pruefe(root_cert, key, cert) == cert


def test_andere_bytes_werden_abgelehnt():
    root_cert, key, cert = _kette()
    with pytest.raises(kette.SignatureError, match="Signatur ungueltig"):
        kette.verify([root_cert], DATA + b" ", signiere(key, cert, DATA), JETZT)


def test_zertifikat_einer_fremden_root_wird_abgelehnt():
    # Bootstrap-Anker (GW 21): das mitgelieferte Zertifikat fuehrt nicht zum Schluesselbund des Geraets.
    _, key, cert = _kette()
    _, fremd = root()
    with pytest.raises(kette.SignatureError, match="keiner vertrauten Root"):
        kette.verify([fremd], DATA, signiere(key, cert, DATA), JETZT)


def test_zweite_root_mit_gleichem_cn_zaehlt_nur_mit_passendem_schluessel():
    root_cert, key, cert = _kette()
    _, gleicher_name = root()  # gleicher CN, anderer Schluessel
    with pytest.raises(kette.SignatureError, match="keiner vertrauten Root"):
        kette.verify([gleicher_name], DATA, signiere(key, cert, DATA), JETZT)
    assert kette.verify([gleicher_name, root_cert], DATA, signiere(key, cert, DATA), JETZT) == cert


def test_zweite_root_im_schluesselbund_gilt():
    _, andere = root(cn="Zweite Root")
    root_cert, key, cert = _kette()
    for bund in ([andere, root_cert], [root_cert, andere]):
        assert kette.verify(bund, DATA, signiere(key, cert, DATA), JETZT) == cert


def test_abgelaufenes_release_zertifikat_wird_abgelehnt():
    root_cert, key, cert = _kette()
    with pytest.raises(kette.SignatureError, match="Zertifikat abgelaufen"):
        _pruefe(root_cert, key, cert, jetzt=BIS + timedelta(seconds=1))
    assert _pruefe(root_cert, key, cert, jetzt=BIS) == cert  # Ablaufzeitpunkt selbst gilt noch


def test_noch_nicht_gueltiges_wird_abgelehnt():
    root_cert, key, cert = _kette()
    with pytest.raises(kette.SignatureError, match="noch nicht gueltig"):
        _pruefe(root_cert, key, cert, jetzt=VON - timedelta(seconds=1))
    assert _pruefe(root_cert, key, cert, jetzt=VON) == cert


def test_abgelaufene_root_wird_abgelehnt():
    root_key, root_cert = root(tage=60)  # BASIS bis BASIS + 60 Tage
    key, cert = release(root_key, root_cert, von=VON, bis=BIS)  # Zertifikat laenger gueltig als die Root
    with pytest.raises(kette.SignatureError, match="Root abgelaufen"):
        _pruefe(root_cert, key, cert, jetzt=BASIS + timedelta(days=60, seconds=1))
    assert _pruefe(root_cert, key, cert, jetzt=BASIS + timedelta(days=60)) == cert


def test_root_noch_nicht_gueltig_wird_abgelehnt():
    root_key, root_cert = root(von=JETZT + timedelta(days=1))
    key, cert = release(root_key, root_cert, von=VON, bis=BIS)
    with pytest.raises(kette.SignatureError, match="Root abgelaufen oder noch nicht gueltig"):
        _pruefe(root_cert, key, cert)


def test_abgelaufene_root_neben_gleicher_gueltiger_root_gilt():
    # Derselbe Schluessel in zwei Roots (Verlaengerung): es genuegt eine, die zu `jetzt` gilt.
    root_key, alt = root(tage=10)
    _, neu = root(key=root_key)
    key, cert = release(root_key, neu, von=VON, bis=BIS)
    for bund in ([alt, neu], [neu, alt]):
        assert kette.verify(bund, DATA, signiere(key, cert, DATA), JETZT) == cert


def test_zertifikat_vor_der_untergrenze_wird_abgelehnt():
    # FR-25: ein Zertifikat, das vor der gespeicherten Untergrenze beginnt, ist ein Rueckfall auf Altes.
    root_cert, key, cert = _kette()
    with pytest.raises(kette.SignatureError, match="vor der Untergrenze"):
        _pruefe(root_cert, key, cert, untergrenze=VON + timedelta(seconds=1))
    assert _pruefe(root_cert, key, cert, untergrenze=VON) == cert
    assert _pruefe(root_cert, key, cert, untergrenze=None) == cert


def test_release_zertifikat_mit_ca_true_wird_abgelehnt():
    root_cert, key, cert = _kette(ca=True)
    with pytest.raises(kette.SignatureError, match="CA-Zertifikat"):
        _pruefe(root_cert, key, cert)


def test_release_zertifikat_ohne_basic_constraints_wird_abgelehnt():
    root_key, root_cert = root()
    key = ec.generate_private_key(ec.SECP256R1())
    builder = (x509.CertificateBuilder().subject_name(root_cert.subject).issuer_name(root_cert.subject)
               .public_key(key.public_key()).serial_number(1).not_valid_before(VON).not_valid_after(BIS)
               .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), True)
               .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), False))
    cert = builder.sign(root_key, hashes.SHA256())
    with pytest.raises(kette.SignatureError, match="BasicConstraints fehlt"):
        _pruefe(root_cert, key, cert)


@pytest.mark.parametrize("eku", [(ExtendedKeyUsageOID.CLIENT_AUTH,), (ExtendedKeyUsageOID.SERVER_AUTH,
                                                                       ExtendedKeyUsageOID.EMAIL_PROTECTION), None])
def test_ohne_code_signing_wird_abgelehnt(eku):
    root_cert, key, cert = _kette(eku=eku)
    with pytest.raises(kette.SignatureError, match="Code-Signing"):
        _pruefe(root_cert, key, cert)


def test_mit_code_signing_unter_weiteren_zwecken_wird_angenommen():
    root_cert, key, cert = _kette(eku=(ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.CODE_SIGNING))
    assert _pruefe(root_cert, key, cert) == cert


def test_ohne_digital_signature_wird_abgelehnt():
    root_cert, key, cert = _kette(digital_signature=False)
    with pytest.raises(kette.SignatureError, match="KeyUsage"):
        _pruefe(root_cert, key, cert)


def test_nicht_kritische_key_usage_wird_abgelehnt():
    root_cert, key, cert = _kette(ku_kritisch=False)
    with pytest.raises(kette.SignatureError, match="KeyUsage"):
        _pruefe(root_cert, key, cert)


def test_unbekannte_kritische_erweiterung_wird_abgelehnt():
    root_cert, key, cert = _kette(extra_kritisch=True)
    with pytest.raises(kette.SignatureError, match="kritische Erweiterung"):
        _pruefe(root_cert, key, cert)


def test_andere_kurve_wird_abgelehnt():
    root_cert, key, cert = _kette(kurve=ec.SECP384R1())
    with pytest.raises(kette.SignatureError, match="nicht P-256"):
        _pruefe(root_cert, key, cert)


def test_anderer_hash_der_zertifikatssignatur_wird_abgelehnt():
    root_cert, key, cert = _kette(signatur_hash=hashes.SHA384())
    with pytest.raises(kette.SignatureError, match="ecdsa-with-SHA256"):
        _pruefe(root_cert, key, cert)


def test_signatur_mit_anderem_hash_wird_abgelehnt():
    root_cert, key, cert = _kette()
    text = json.loads(signiere(key, cert, DATA))
    text["signatur"] = base64.b64encode(key.sign(DATA, ec.ECDSA(hashes.SHA384()))).decode()
    with pytest.raises(kette.SignatureError, match="Signatur ungueltig"):
        kette.verify([root_cert], DATA, json.dumps(text), JETZT)


def test_signatur_eines_anderen_schluessels_wird_abgelehnt():
    root_cert, _, cert = _kette()
    fremd = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(kette.SignatureError, match="Signatur ungueltig"):
        kette.verify([root_cert], DATA, signiere(fremd, cert, DATA), JETZT)


def test_root_ohne_ca_oder_ohne_key_cert_sign_wird_nicht_geladen():
    with pytest.raises(kette.SignatureError, match="Root: kein CA-Zertifikat"):
        kette.load_roots(roots_pem(root(ca=False)[1]))
    with pytest.raises(kette.SignatureError, match="key_cert_sign"):
        kette.load_roots(roots_pem(root(key_cert_sign=False)[1]))
    with pytest.raises(kette.SignatureError, match="Root: Schluessel nicht P-256"):
        kette.load_roots(roots_pem(root(kurve=ec.SECP384R1())[1]))
    # Eine schlechte Root im Bund verdirbt den ganzen Bund (lautes Scheitern statt stilles Auslassen).
    with pytest.raises(kette.SignatureError):
        kette.load_roots(roots_pem(root()[1], root(ca=False)[1]))


def test_gueltige_roots_werden_geladen():
    a, b = root(cn="A")[1], root(cn="B")[1]
    assert kette.load_roots(roots_pem(a, b)) == [a, b]


@pytest.mark.parametrize("text", ["", "kein pem", "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"])
def test_roots_ohne_zertifikat_werden_nicht_geladen(text):
    with pytest.raises(kette.SignatureError, match="Root: keine Zertifikate"):
        kette.load_roots(text)


def test_platzhalter_lehnt_alles_ab():
    gueltig = roots_pem(root()[1])
    for text in (kette.PLACEHOLDER_MARKER, f"# {kette.PLACEHOLDER_MARKER}\n{gueltig}", gueltig + kette.PLACEHOLDER_MARKER):
        with pytest.raises(kette.SignatureError, match="Platzhalter"):
            kette.load_roots(text)
        with pytest.raises(kette.SignatureError, match="Platzhalter"):
            kette.roots_identity(text)
    assert kette.PLACEHOLDER_MARKER == "SMARTHEAT-PLATZHALTER"


def test_ohne_roots_lehnt_verify_alles_ab():
    _, key, cert = _kette()
    with pytest.raises(kette.SignatureError, match="keiner vertrauten Root"):
        kette.verify([], DATA, signiere(key, cert, DATA), JETZT)


def test_zu_langer_signaturtext_wird_abgelehnt():
    root_cert, _, _ = _kette()
    assert kette.MAX_SIGNATUR_BYTES == 16384
    with pytest.raises(kette.SignatureError, match="zu lang"):
        kette.verify([root_cert], DATA, " " * (kette.MAX_SIGNATUR_BYTES + 1), JETZT)
    # Genau an der Grenze wird weitergeprueft (hier scheitert erst das JSON).
    with pytest.raises(kette.SignatureError, match="kein JSON"):
        kette.verify([root_cert], DATA, "x" * kette.MAX_SIGNATUR_BYTES, JETZT)
    # Die Grenze zaehlt Bytes, nicht Zeichen.
    with pytest.raises(kette.SignatureError, match="zu lang"):
        kette.verify([root_cert], DATA, "ä" * (kette.MAX_SIGNATUR_BYTES // 2 + 1), JETZT)


def test_kaputte_signaturtexte_werden_abgelehnt():
    root_cert, key, cert = _kette()
    gut = json.loads(signiere(key, cert, DATA))
    pem = gut["zertifikat"]

    def ablehnen(text, match):
        with pytest.raises(kette.SignatureError, match=match):
            kette.verify([root_cert], DATA, text, JETZT)

    ablehnen("", "kein JSON")
    ablehnen("kein json", "kein JSON")
    ablehnen("[" * 10000, "kein JSON")  # tief verschachtelt: RecursionError darf nicht durchschlagen
    ablehnen("[]", "Felder")
    ablehnen('"text"', "Felder")
    ablehnen(json.dumps({**gut, "format": "smartheat-kette-2"}), "falsches Format")
    ablehnen(json.dumps({**gut, "format": None}), "falsches Format")
    ablehnen(json.dumps({k: v for k, v in gut.items() if k != "format"}), "Felder")
    ablehnen(json.dumps({k: v for k, v in gut.items() if k != "zertifikat"}), "Felder")
    ablehnen(json.dumps({k: v for k, v in gut.items() if k != "signatur"}), "Felder")
    ablehnen(json.dumps({**gut, "zusatz": "x"}), "Felder")
    ablehnen(json.dumps({**gut, "zertifikat": 5}), "Felder")
    ablehnen(json.dumps({**gut, "signatur": None}), "Felder")
    ablehnen(json.dumps({**gut, "signatur": "***kein base64***"}), "kein Base64")
    ablehnen(json.dumps({**gut, "zertifikat": "kein pem"}), "Zertifikat: kein PEM")
    ablehnen(json.dumps({**gut, "zertifikat": ""}), "Zertifikat: kein PEM")
    ablehnen(json.dumps({**gut, "zertifikat": pem + pem}), "Zertifikat: kein PEM")  # genau ein Zertifikat
    ablehnen(json.dumps({**gut, "zertifikat": pem.replace("MI", "MJ", 1)}), "Zertifikat: kein PEM")
    ablehnen(json.dumps({**gut, "signatur": base64.b64encode(b"kein DER").decode()}), "Signatur ungueltig")
    ablehnen(json.dumps({**gut, "signatur": ""}), "Signatur ungueltig")


def test_naive_zeit_ist_ein_programmierfehler():
    root_cert, key, cert = _kette()
    with pytest.raises(ValueError, match="Zeitzone"):
        kette.verify([root_cert], DATA, signiere(key, cert, DATA), JETZT.replace(tzinfo=None))
    with pytest.raises(ValueError, match="Zeitzone"):
        _pruefe(root_cert, key, cert, untergrenze=VON.replace(tzinfo=None))


def test_roots_identity_aendert_sich_mit_dem_bund():
    a, b = root(cn="A")[1], root(cn="B")[1]
    nur_a, beide, umgekehrt = roots_pem(a), roots_pem(a, b), roots_pem(b, a)
    kennungen = {kette.roots_identity(nur_a), kette.roots_identity(beide), kette.roots_identity(roots_pem(b))}
    assert len(kennungen) == 3
    assert kette.roots_identity(nur_a) == kette.roots_identity(nur_a)
    assert kette.roots_identity(beide) == kette.roots_identity(umgekehrt)  # Reihenfolge ist keine Aenderung
    kennung = kette.roots_identity(beide)
    assert len(kennung) == 64 and kennung == kennung.lower() and int(kennung, 16) >= 0
    with pytest.raises(kette.SignatureError):
        kette.roots_identity("kein pem")


def test_cli_exit_codes(tmp_path, capsys):
    jetzt = datetime.now(UTC)
    root_key, root_cert = root()
    key, cert = release(root_key, root_cert, von=jetzt - timedelta(days=1), bis=jetzt + timedelta(days=30))
    roots, datei, sig = tmp_path / "roots.pem", tmp_path / "manifest.json", tmp_path / "manifest.json.sig"
    roots.write_text(roots_pem(root_cert))
    datei.write_bytes(DATA)
    sig.write_text(signiere(key, cert, DATA))
    assert kette.main([str(roots), str(datei), str(sig)]) == 0
    assert capsys.readouterr().out.startswith("OK: ")
    # abgelehnt: Exit 1, Grund auf stderr
    datei.write_bytes(DATA + b" ")
    assert kette.main([str(roots), str(datei), str(sig)]) == 1
    assert "UNGUELTIG: Signatur ungueltig" in capsys.readouterr().err
    datei.write_bytes(DATA)
    roots.write_text(kette.PLACEHOLDER_MARKER)
    assert kette.main([str(roots), str(datei), str(sig)]) == 1
    assert "Platzhalter" in capsys.readouterr().err
    roots.write_text(roots_pem(root_cert))
    abgelaufen = tmp_path / "alt.sig"
    abgelaufen.write_text(signiere(*release(root_key, root_cert, von=jetzt - timedelta(days=9),
                                            bis=jetzt - timedelta(days=8)), DATA))
    assert kette.main([str(roots), str(datei), str(abgelaufen)]) == 1
    # fehlende Datei: Exit 1; falsche Argumentzahl: Exit 2
    assert kette.main([str(roots), str(tmp_path / "fehlt"), str(sig)]) == 1
    assert "UNGUELTIG" in capsys.readouterr().err
    assert kette.main([str(roots), str(datei)]) == 2
    assert kette.main([]) == 2
    assert kette.main([str(roots), str(datei), str(sig), "zuviel"]) == 2
    assert "Aufruf" in capsys.readouterr().err


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl fehlt")
def test_signatur_von_openssl_wird_angenommen(tmp_path):
    # Das Format des Release-Werkzeugs: PEM-Schluessel, `openssl dgst -sha256 -sign` (DER-ECDSA), Base64.
    root_key, root_cert = root()
    key, cert = release(root_key, root_cert, von=VON, bis=BIS)
    schluessel, datei, sig = tmp_path / "release.key", tmp_path / "manifest.json", tmp_path / "manifest.sig"
    schluessel.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
    datei.write_bytes(DATA)
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(schluessel), "-out", str(sig), str(datei)], check=True)
    text = json.dumps({"format": FORMAT, "zertifikat": cert.public_bytes(serialization.Encoding.PEM).decode(),
                       "signatur": base64.b64encode(sig.read_bytes()).decode()})
    assert kette.verify([root_cert], DATA, text, JETZT) == cert
