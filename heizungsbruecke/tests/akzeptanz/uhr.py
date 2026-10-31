"""Uhr der Welt: eine monotone Uhr, die nur vorwaerts geht, und die Wanduhr (Berlin), die mitlaeuft und per
`wand_versatz` springen kann (Rueckstellung der Systemzeit, Zeitsynchronisation)."""
import datetime
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
START = datetime.datetime(2027, 1, 15, 9, 0, tzinfo=BERLIN)  # Tagestick 12:00 wird nicht erreicht


class Uhr:
    jetzt = 1000.0
    wand_versatz = 0.0  # Sekunden, um die die Wanduhr von der monotonen Uhr abweicht (negativ: zurueckgesprungen)

    def __call__(self) -> float:
        return self.jetzt

    def wand(self) -> datetime.datetime:
        return START + datetime.timedelta(seconds=self.jetzt - 1000.0 + self.wand_versatz)
