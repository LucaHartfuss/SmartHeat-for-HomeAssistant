"""Zeigt je Aufzeichnung, welche der fuer vicare_cloud relevanten Features vorhanden sind (Hilfe beim Schreiben von
expected.json; schreibt selbst nichts). Aufruf: python3 survey.py"""
import json
from pathlib import Path

NAMES = ("heating.circuits.0.heating.curve", "heating.circuits.0.operating.programs.normal",
         "heating.circuits.0.operating.programs.active", "heating.sensors.temperature.outside",
         "heating.circuits.0.sensors.temperature.supply", "heating.circuits.0.sensors.temperature.room",
         "heating.sensors.temperature.return", "heating.gas.consumption.heating", "heating.gas.consumption.dhw",
         "heating.power.consumption.total")
for path in sorted((Path(__file__).parent / "recordings").glob("*.json")):
    features = {f.get("feature") for f in json.loads(path.read_text()).get("data", [])}
    kind = "brenner" if any(f.startswith("heating.burners.") for f in features) else ""
    kind += " verdichter" if any(f.startswith("heating.compressors.") for f in features) else ""
    print(f"{path.name:55} {kind:20}", " ".join("1" if n in features else "." for n in NAMES))
