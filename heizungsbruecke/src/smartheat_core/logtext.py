"""Text fremder Herkunft fuer eine Logzeile: Servertext (Grund einer Ablehnung) oder Text aus dem
Konfigurationsdokument darf keine eigene Logzeile vortaeuschen und die UTF-8-Ausgabe des Logs nicht brechen.
Steuerzeichen (U+0000-U+001F, U+007F-U+009F), die Zeilentrenner U+2028/U+2029, die Bidi-Steuerzeichen U+061C,
U+200E/U+200F, U+202A-U+202E und U+2066-U+2069 (ordnen die Darstellung einer Zeile um), einzelne Surrogate und der
Backslash werden als Escape geschrieben (wie repr, ohne Anfuehrungszeichen); alles andere bleibt. Nur fuer das Log: Push
und Speicher bekommen den Text unveraendert."""
import re

_ESCAPE = re.compile("[\\\\\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ud800-\udfff]")
_BENANNT = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape(treffer: re.Match[str]) -> str:
    zeichen = treffer.group()
    if zeichen in _BENANNT:
        return _BENANNT[zeichen]
    code = ord(zeichen)
    return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"


def lesbar(text: str) -> str:
    """`text` auf einer Logzeile: Steuerzeichen, Zeilentrenner, Bidi-Steuerzeichen, Surrogate und Backslash als Escape."""
    return _ESCAPE.sub(_escape, text)
