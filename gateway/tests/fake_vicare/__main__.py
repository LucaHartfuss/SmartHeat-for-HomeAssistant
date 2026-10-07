"""Startet den Fake-ViCare als Container-Dienst (E2E): python -m fake_vicare [port]."""
import sys
import threading

from fake_vicare.server import FakeVicare

fake = FakeVicare()
print(fake.start("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 8099), flush=True)
threading.Event().wait()
