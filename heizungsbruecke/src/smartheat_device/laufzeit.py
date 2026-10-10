"""Die Laufzeit (smartheat_runtime) auf dem Link des Geraets (Spec 5b 5.1/5.2, Weichenstellung 2): Tick und Telemetrie
gehen ueber dieselbe MQTT-Verbindung wie Dokumente und Befehle, die Antwort down/setpoints kommt ueber den Link zurueck.
Der Snapshot traegt zusaetzlich raum_soll_wirksam (Spec 2/3.2): im Modus manuell (Schema 1) ist das das Raum-Soll des
Ticks, also die Bedienung bzw. ein lokaler Wunsch. stop() (Abo inaktiv, Ruhezustand der Laufzeit) schaltet nur die
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
            self._publish("up/snapshot", {**payload, "raum_soll_wirksam": payload.get("room_target")})

    def publish_telemetry(self, payload: dict) -> None:
        if not self._stopped:
            self._publish("telemetry", payload)

    def subscribe_setpoints(self, on_message) -> None:
        """Ohne Wirkung: die Antworten kommen ueber setpoints() vom Geraet."""

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
