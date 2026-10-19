"""smartheat_core.logtext.lesbar (Testoffensive T6, Sicherheitscheck zu a9756e8): Text fremder Herkunft (Server,
Konfigurationsdokument) steht im Log auf einer Zeile und UTF-8-kodierbar. Steuerzeichen, Zeilentrenner,
Bidi-Steuerzeichen, Surrogate und der Backslash werden als Escape geschrieben, alles andere bleibt."""
import pytest

from smartheat_core.logtext import lesbar


@pytest.mark.parametrize(("roh", "erwartet"), [
    ("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t"), ("\x00", "\\x00"), ("\x1b", "\\x1b"), ("\x1f", "\\x1f"),
    ("\x7f", "\\x7f"), ("\x80", "\\x80"), ("\x85", "\\x85"), ("\x9f", "\\x9f"),
    ("\u2028", "\\u2028"), ("\u2029", "\\u2029"), ("\ud800", "\\ud800"), ("\udfff", "\\udfff"), ("\\", "\\\\"),
    ("\u200e", "\\u200e"), ("\u200f", "\\u200f"), ("\u202a", "\\u202a"), ("\u202e", "\\u202e"),
    ("\u2066", "\\u2066"), ("\u2069", "\\u2069"),
], ids=["lf", "cr", "tab", "nul", "esc", "us", "del", "c1_80", "nel", "c1_9f", "ls", "ps", "surrogat_hoch",
        "surrogat_tief", "backslash", "lrm", "rlm", "bidi_202a", "bidi_202e", "bidi_2066", "bidi_2069"])
def test_jedes_steuerzeichen_wird_ein_escape(roh, erwartet):
    assert lesbar(f"a{roh}b") == f"a{erwartet}b"


def test_eine_gefaelschte_zweite_logzeile_bleibt_auf_einer_zeile():
    text = lesbar("Grund\r\nWARNING  smartheat_runtime.ticks: gefaelscht\x1b[31m")
    assert text == "Grund\\r\\nWARNING  smartheat_runtime.ticks: gefaelscht\\x1b[31m"
    assert text.splitlines() == [text]
    text.encode("utf-8")


@pytest.mark.parametrize("text", ["", "Heizungsbrücke: Server hat die Messwerte abgelehnt (dat: 99 °C, erlaubt −40–45).",
                                  "€ 😀 \xa0 \xff Ā"])
def test_druckbarer_text_bleibt_unveraendert(text):
    assert lesbar(text) == text
