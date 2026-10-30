"""Lokale Befehle der Integration ueber das stdin des Add-ons: loescht der Nutzer den
HA-Eintrag, schreibt die Integration `entfernen <geraet_id>` an das Add-on; das Geraet meldet sich dann wie bei
sign_off ab. Beginnt die Einrichtung per Geraetecode, schreibt sie `nachweis <geraet_id>`; das Add-on antwortet mit
dem Ereignis smartheat_nachweis (geraet_nachweis). Erlaubt sind genau diese beiden Formen mit der eigenen Geraete-ID;
jede andere Zeile (fremde ID, unbekannt,
zu lang, kein UTF-8) endet in genau einer WARNING mit dem Grund, nie mit dem Zeileninhalt (der Absender ist nicht
vertrauenswuerdig und der Inhalt koennte Zugangsdaten tragen). Reine Standardbibliothek; der Zeilenleser laeuft in
einem Daemon-Thread, der Rueckruf stellt die Arbeit in das Postfach des Geraetekerns (__main__.stdin_anhaengen)."""
import logging
import threading
from collections.abc import Callable
from typing import BinaryIO

logger = logging.getLogger(__name__)

#: Wortlaut des Befehls und Format der Zeile; die Integration (Konstanten in ihrer const.py) und der Contract-Check
#: vergleichen sie.
BEFEHL_ENTFERNEN = "entfernen"
BEFEHL_NACHWEIS = "nachweis"
BEFEHLE = (BEFEHL_ENTFERNEN, BEFEHL_NACHWEIS)
ZEILE_FORMAT = "{befehl} {geraet_id}\n"
#: Laengste Zeile in Bytes ohne Zeilenende; laengere werden verworfen (gueltige Zeilen sind ASCII und kuerzer).
ZEILE_MAX = 200

GRUND_KEIN_UTF8 = "kein_utf8"
GRUND_UNBEKANNT = "unbekannt"
GRUND_ANDERE_ID = "andere_geraete_id"
GRUND_ZU_LANG = "zu_lang"


def zeile_pruefen(inhalt: bytes, geraet_id: str) -> tuple[str | None, str | None]:
    """(Befehl, None), wenn inhalt (eine Zeile ohne Zeilenende) genau ein Befehl mit der eigenen Geraete-ID ist; sonst
    (None, Grund) mit kein_utf8, unbekannt oder andere_geraete_id."""
    try:
        text = inhalt.decode("utf-8")
    except UnicodeDecodeError:
        return None, GRUND_KEIN_UTF8
    for befehl in BEFEHLE:
        if text + "\n" == ZEILE_FORMAT.format(befehl=befehl, geraet_id=geraet_id):
            return befehl, None
    teile = text.split(" ")
    if len(teile) == 2 and teile[0] in BEFEHLE and teile[1]:
        return None, GRUND_ANDERE_ID
    return None, GRUND_UNBEKANNT


def _rest_verwerfen(quelle: BinaryIO) -> None:
    """Rest einer zu langen Zeile bis zum Zeilenende oder EOF lesen und wegwerfen."""
    while True:
        stueck = quelle.readline(ZEILE_MAX + 1)
        if not stueck or stueck.endswith(b"\n"):
            return


def lesen(quelle: BinaryIO, geraet_id: str, entfernen: Callable[[], None], nachweis: Callable[[], None]) -> None:
    """Zeilen aus quelle lesen bis EOF (still) oder bis ein Lesefehler (eine WARNING mit der Fehlerart); jede Zeile
    wird geprueft, ein Treffer ruft entfernen() bzw. nachweis()."""
    try:
        while True:
            zeile = quelle.readline(ZEILE_MAX + 1)
            if not zeile:
                return
            if zeile == b"\n":
                # Der Supervisor haengt beim Schreiben selbst ein Zeilenende an (supervisor/docker/app.py write_stdin:
                # data + b"\n"): auf der Leitung steht `entfernen <id>\n\n`. Die exakt leere Zeile ist kein Befehl
                # und bleibt still (Leerzeichen oder \r\n sind es nicht).
                continue
            if zeile.endswith(b"\n"):
                inhalt = zeile[:-1]
            elif len(zeile) > ZEILE_MAX:
                _rest_verwerfen(quelle)
                logger.warning("stdin: Zeile verworfen (%s)", GRUND_ZU_LANG)
                continue
            else:
                inhalt = zeile  # letzte Zeile ohne Zeilenende vor EOF
            befehl, grund = zeile_pruefen(inhalt, geraet_id)
            if befehl == BEFEHL_ENTFERNEN:
                entfernen()
            elif befehl == BEFEHL_NACHWEIS:
                nachweis()
            else:
                logger.warning("stdin: Zeile verworfen (%s)", grund)
    except (OSError, ValueError) as error:
        logger.warning("stdin: Lesen gescheitert (%s), der lokale Befehl entfaellt", type(error).__name__)


def starten(quelle: BinaryIO, geraet_id: str, entfernen: Callable[[], None],
            nachweis: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=lesen, args=(quelle, geraet_id, entfernen, nachweis), name="stdin", daemon=True)
    thread.start()
    return thread
