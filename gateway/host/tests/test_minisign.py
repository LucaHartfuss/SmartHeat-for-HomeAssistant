import base64
import shutil
import subprocess
from pathlib import Path

import pytest
from minisign_helper import keypair, sign

from smartheat_host import minisign

DATA = b'{"version": "0.2.0"}'


@pytest.mark.parametrize("prehash", [True, False])
def test_valid_signature_returns_the_trusted_comment(prehash):
    secret, key_id, public = keypair()
    signature = sign(secret, key_id, DATA, "gw 0.2.0", prehash)
    assert minisign.verify(minisign.load_public_key(public), DATA, signature) == "gw 0.2.0"


def test_other_bytes_are_rejected():
    secret, key_id, public = keypair()
    with pytest.raises(minisign.SignatureError):
        minisign.verify(minisign.load_public_key(public), DATA + b" ", sign(secret, key_id, DATA))


def test_other_key_is_rejected():
    secret, key_id, _ = keypair()
    _, _, other_public = keypair()
    with pytest.raises(minisign.SignatureError):
        minisign.verify(minisign.load_public_key(other_public), DATA, sign(secret, key_id, DATA))


def test_same_key_id_but_other_key_is_rejected():
    # Gleiche Schluessel-ID, anderer Schluessel: die Ed25519-Pruefung selbst muss ablehnen.
    secret, key_id, _ = keypair()
    _, _, other_public = keypair()
    forged = other_public.splitlines()
    raw = bytearray(base64.b64decode(forged[1]))
    raw[2:10] = key_id
    forged[1] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(minisign.SignatureError):
        minisign.verify(minisign.load_public_key("\n".join(forged) + "\n"), DATA, sign(secret, key_id, DATA))


def test_tampered_trusted_comment_is_rejected():
    secret, key_id, public = keypair()
    text = sign(secret, key_id, DATA, "gw 0.2.0").replace("gw 0.2.0", "gw 9.9.9")
    with pytest.raises(minisign.SignatureError):
        minisign.verify(minisign.load_public_key(public), DATA, text)


@pytest.mark.parametrize("text", ["", "kaputt", "untrusted comment: x\nAAAA\n"])
def test_broken_signature_texts_are_rejected(text):
    _, _, public = keypair()
    with pytest.raises(minisign.SignatureError):
        minisign.verify(minisign.load_public_key(public), DATA, text)


def test_placeholder_public_key_refuses_everything():
    placeholder = (Path(__file__).resolve().parents[1] / "release.pub").read_text()
    with pytest.raises(minisign.SignatureError, match="Platzhalter"):
        minisign.load_public_key(placeholder)


def test_broken_public_keys_are_rejected():
    for text in ("", "untrusted comment: x\n!!!!\n", "untrusted comment: x\nAAAA\n"):
        with pytest.raises(minisign.SignatureError):
            minisign.load_public_key(text)


def test_cli_exit_codes(tmp_path, capsys):
    secret, key_id, public = keypair()
    (tmp_path / "k.pub").write_text(public)
    (tmp_path / "m.json").write_bytes(DATA)
    (tmp_path / "m.json.minisig").write_text(sign(secret, key_id, DATA, "gw 0.2.0"))
    args = [str(tmp_path / "k.pub"), str(tmp_path / "m.json"), str(tmp_path / "m.json.minisig")]
    assert minisign.main(args) == 0
    assert "gw 0.2.0" in capsys.readouterr().out
    (tmp_path / "m.json").write_bytes(DATA + b"x")
    assert minisign.main(args) == 1
    assert minisign.main(args[:2]) == 2
    assert minisign.main([str(tmp_path / "fehlt"), *args[1:]]) == 1


@pytest.mark.skipif(shutil.which("minisign") is None, reason="Programm minisign fehlt (CI installiert es)")
def test_signatures_of_the_real_minisign_tool_verify(tmp_path):
    subprocess.run(["minisign", "-G", "-W", "-p", tmp_path / "k.pub", "-s", tmp_path / "k.key"], check=True,
                   capture_output=True)
    (tmp_path / "m.json").write_bytes(DATA)
    subprocess.run(["minisign", "-S", "-s", tmp_path / "k.key", "-m", tmp_path / "m.json", "-t", "gw 0.2.0"],
                   check=True, capture_output=True)
    public = minisign.load_public_key((tmp_path / "k.pub").read_text())
    assert minisign.verify(public, DATA, (tmp_path / "m.json.minisig").read_text()) == "gw 0.2.0"
    (tmp_path / "m.json").write_bytes(DATA + b"x")
    with pytest.raises(minisign.SignatureError):
        minisign.verify(public, DATA + b"x", (tmp_path / "m.json.minisig").read_text())
