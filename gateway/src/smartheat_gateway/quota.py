"""Kontingent-Waechter (Spec SHG G2 4.2): Zaehler je Treiber in /data/quota/<treiber>.json, gemeinsam fuer Agent und
Laufzeit (zwei Prozesse) ueber flock. Gleitendes Fenster ueber die WANDUHR (die monotone Uhr ist je Prozess
verschieden); Eintraege aus der Zukunft (Uhrsprung beim Boot) verfallen mit dem Fenster."""
import fcntl
import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from smartheat_gateway.files import write_json


@dataclass(frozen=True)
class QuotaSpec:
    limit: int          # Grenze des Herstellers (Anzeige/Diagnose)
    hard_limit: int     # eigene harte Grenze: ab hier "erschoepft"
    window_seconds: float


class QuotaExhausted(RuntimeError):
    pass


class QuotaGuard:
    def __init__(self, path: Path, spec: QuotaSpec, now: Callable[[], float] = time.time) -> None:
        self._path = path
        self._lock_path = path.with_suffix(".lock")
        self._spec = spec
        self._now = now

    def take(self) -> None:
        with self._locked() as state:
            if self._is_exhausted(state):
                raise QuotaExhausted(f"Kontingent erschöpft ({len(state['aufrufe'])}/{self._spec.hard_limit})")
            state["aufrufe"].append(self._now())

    def used(self) -> int:
        with self._locked() as state:
            return len(state["aufrufe"])

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
        """Zustand unter exklusivem flock. Gespeichert wird nach Erfolg und nach QuotaExhausted (der bereinigte
        Zustand); der Lock wird immer freigegeben."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            state = self._load()
            try:
                yield state
            except QuotaExhausted:
                self._save(state)
                raise
            self._save(state)
        finally:
            os.close(fd)  # gibt auch den flock frei

    def _load(self) -> dict:
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        now, window = self._now(), self._spec.window_seconds
        calls = [ts for ts in raw.get("aufrufe", []) if isinstance(ts, int | float) and now - window < ts <= now + 1]
        blocked = raw.get("gesperrt_bis")
        return {"aufrufe": calls, "gesperrt_bis": blocked if isinstance(blocked, int | float) else None}

    def _save(self, state: dict) -> None:
        # Atomar mit fsync (files.py): nach einem Stromausfall nie eine halbe Datei, die als leer gilt und das
        # Kontingent zuruecksetzt (Plan G2b-1, Restpunkt 8). Welche Uhr das Fenster zaehlt, entscheidet G4.
        write_json(self._path, state)
