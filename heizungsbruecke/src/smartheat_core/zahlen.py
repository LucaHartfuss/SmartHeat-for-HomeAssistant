"""Zahlenpruefung fuer Werte, die aus JSON stammen koennen (Serverantwort, Dokumente, Zustandsdateien, HA-Zustaende),
rein (Standardbibliothek). JSON liefert beliebig grosse Ganzzahlen; `math.isfinite` wirft bei einer Ganzzahl ausserhalb
des float-Bereichs (z. B. 10**400) einen OverflowError, statt sie als nicht endlich abzulehnen. Eine solche Zahl nimmt
hier denselben Ungueltig-Pfad wie NaN und Unendlich."""
import math
from typing import TypeGuard

# Gleitkomma-Rest beim Vergleich mit einer Toleranz (21.05 - 21.0 = 0.0500000000000007, 0.91 - 0.9): eine Abweichung von
# genau der Toleranz zaehlt nicht als Abweichung (enforce, derived).
GLEITKOMMA_REST = 1e-9


def ist_endliche_zahl(value) -> TypeGuard[float]:
    """True fuer ein int oder float (kein bool), das endlich ist. Wirft nie: eine Ganzzahl ausserhalb des
    float-Bereichs ist nicht endlich."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False
