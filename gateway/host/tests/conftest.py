"""Tests der Host-Dienste.

Profile von hypothesis wie in heizungsbruecke/tests/conftest.py (Root-Runbook Abschnitt 3): `schnell` (Standard, 50
Beispiele, fester Seed) und `voll` (500 Beispiele, zufaelliger Seed), gewaehlt per `HYPOTHESIS_PROFILE`
(`scripts/check.sh --full` setzt `voll`). Einzelne Tests setzen keine eigenen Einstellungen."""
import os

from hypothesis import HealthCheck, settings

settings.register_profile("schnell", max_examples=50, derandomize=True, database=None, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow], print_blob=False)
settings.register_profile("voll", parent=settings.get_profile("schnell"), max_examples=500, derandomize=False,
                          print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "schnell"))
