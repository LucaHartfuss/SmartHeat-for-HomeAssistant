"""Kontingent-Waechter (Spec SHG G2 4.2): Zaehler je Treiber in /data/quota/<treiber>.json, gemeinsam fuer die Befehle
des Geraetekerns und die Laufzeit (zwei Waechter auf derselben Datei in einem Prozess, geschuetzt durch ein Lock je
Pfad). Gleitendes Fenster ueber die WANDUHR; Eintraege aus der Zukunft (Uhrsprung zurueck) zaehlen auf `now` begrenzt
bis zum Ende des Fensters weiter; die Sperre nach HTTP 429 (`gesperrt_bis`) gilt hoechstens ein Fenster ab jetzt."""
import json
import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from smartheat_core import dateien


@dataclass(frozen=True)
class QuotaSpec:
    hard_limit: int     # eigene harte Grenze: ab hier "erschoepft"
    window_seconds: float


class QuotaExhausted(RuntimeError):
    pass


_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_LOCK = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    """Ein Lock je Datei, gemeinsam fuer alle Waechter des Prozesses auf diesem Pfad."""
    with _LOCKS_LOCK:
        return _LOCKS.setdefault(path.absolute(), threading.Lock())


class QuotaGuard:
    def __init__(self, path: Path, spec: QuotaSpec, now: Callable[[], float] = time.time) -> None:
        self._path = path
        self._lock = _lock_for(path)
        self._spec = spec
        self._now = now

    def take(self) -> None:
        with self._locked() as state:
            if self._is_exhausted(state):
                raise QuotaExhausted(f"Kontingent erschöpft ({len(state['aufrufe'])}/{self._spec.hard_limit})")
            state["aufrufe"].append(self._now())

    def exhausted(self) -> bool:
        with self._locked() as state:
            return self._is_exhausted(state)

    def block_until(self, until: float) -> None:
        """Rate-Limit-Antwort der API: erschoepft bis `until`."""
        with self._locked() as state:
            state["gesperrt_bis"] = max(until, state.get("gesperrt_bis") or 0.0)

    def _is_exhausted(self, state: dict) -> bool:
        return len(state["aufrufe"]) >= self._spec.hard_limit or (state.get("gesperrt_bis") or 0.0) > self._now()

    @contextmanager
    def _locked(self) -> Iterator[dict]:
        """Zustand unter dem Lock der Datei. Gespeichert wird nach Erfolg und nach QuotaExhausted (der bereinigte
        Zustand); der Lock wird immer freigegeben."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            state = self._load()
            try:
                yield state
            except QuotaExhausted:
                self._save(state)
                raise
            self._save(state)

    def _load(self) -> dict:
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        now, window = self._now(), self._spec.window_seconds
        calls = [min(ts, now) for ts in raw.get("aufrufe", []) if isinstance(ts, int | float) and now - window < ts]
        blocked = raw.get("gesperrt_bis")
        # Rate-Limit-Sperre hoechstens ein Fenster ab jetzt (Uhrsprung zurueck, kaputter Wert); keine Zahl: keine Sperre
        blocked = None if not isinstance(blocked, int | float) or math.isnan(blocked) else min(blocked, now + window)
        return {"aufrufe": calls, "gesperrt_bis": blocked}

    def _save(self, state: dict) -> None:
        # Atomar mit fsync (smartheat_core.dateien): nach einem Stromausfall nie eine halbe Datei, die als leer gilt und das
        # Kontingent zuruecksetzt (Plan G2b-1, Restpunkt 8). Welche Uhr das Fenster zaehlt, entscheidet G4.
        dateien.schreiben_json(self._path, state, modus=dateien.LESBAR)
