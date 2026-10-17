"""Die Laufzeit (smartheat_runtime) auf dem Link des Geraets (Spec 5b 5.1/5.2, Weichenstellung 2): Tick und Telemetrie
gehen ueber dieselbe MQTT-Verbindung wie Dokumente und Befehle, die Antwort down/setpoints kommt ueber den Link zurueck.
Der Snapshot geht unveraendert hoch (raum_soll_wirksam entfiel mit Stufe 3 S8a-1, A5-43: im Modus manuell war es nur
room_target). stop() (Abo inaktiv, Ruhezustand der Laufzeit) schaltet nur die
Laufzeit stumm; der Link bleibt, Dokumente und Befehle laufen weiter. Die paho-Thread-Regel gilt: Ereignisse gehen nur
per post in den Regel-Worker."""
from collections.abc import Callable

from smartheat_runtime.runtime import EV_MQTT_CONNECTED, EV_SETPOINTS
from smartheat_runtime.worker import Event, RegulationWorker


class RuntimeChannel:
    def __init__(self, publish: Callable[[str, dict], int | None], connected: Callable[[], bool],
                 failures: Callable[[], int]) -> None:
        self._publish, self._connected, self._failures = publish, connected, failures
        self._worker: RegulationWorker | None = None
        self._stopped = False

    def binden(self, worker: RegulationWorker) -> "RuntimeChannel":
        """Vom mqtt_factory-Aufruf der Laufzeit; besteht die Verbindung schon, gilt sie sofort als hergestellt."""
        self._worker = worker
        if self._connected():
            worker.post_coalesced(EV_MQTT_CONNECTED)
        return self

    # --- MqttChannel (Laufzeit) ---

    @property
    def connect_failures(self) -> int:
        return self._failures()

    def is_connected(self) -> bool:
        return not self._stopped and self._connected()

    def publish_snapshot(self, payload: dict) -> None:
        if not self._stopped:
            self._publish("up/snapshot", payload)

    def publish_telemetry(self, payload: dict) -> None:
        if not self._stopped:
            self._publish("telemetry", payload)

    def loop_start(self) -> None:
        """Ohne Wirkung: den Link startet das Geraet."""

    def stop(self) -> None:
        self._stopped = True

    # --- vom Geraet (Geraete-Thread) ---

    def verbunden(self) -> None:
        if self._worker is not None and not self._stopped:
            self._worker.post_coalesced(EV_MQTT_CONNECTED)

    def setpoints(self, payload: dict) -> None:
        if self._worker is not None and not self._stopped:
            self._worker.post(Event(EV_SETPOINTS, {"payload": payload}))
