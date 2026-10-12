"""SignalSource ueber die HA-REST-API (Spec SHG 3.2): Referenzen sind Entity-IDs bzw. `entity::attribut`
(ha_api.get_state). Uebersetzt die requests-Fehler in die Fehler des Ports; ungueltige Werte bleiben ValueError/
KeyError/TypeError."""
from collections.abc import Callable

import requests

from smartheat_runtime.ports import SignalNotFound, SourceUnavailable
from smartheat_runtime.soll_speicher import TargetStore

#: Referenz des Raum-Solls in der Manifest-Rolle room_target: der Wert kommt aus dem Soll-Speicher, nicht aus HA.
SOLL_REF = "smartheat:raum_soll"


class HaSignalSource:
    def __init__(self, ha_api) -> None:
        self._ha_api = ha_api

    def get_state(self, ref: str) -> float:
        return _translated(self._ha_api.get_state, ref)

    def get_raw_state(self, ref: str) -> str:
        return _translated(self._ha_api.get_raw_state, ref)

    def device_key(self, ref: str) -> str:
        return ref.partition("::")[0]


class GeraetSignale:
    """SignalSource des Geraete-Pfads (Spec 5c 1.3): der Raum-Soll (SOLL_REF) kommt aus dem Soll-Speicher, alles
    andere aus HA. store_getter liefert None, solange der Host nicht geladen hat."""

    def __init__(self, ha_signals: HaSignalSource, store_getter: Callable[[], TargetStore | None]) -> None:
        self._ha, self._store = ha_signals, store_getter

    def get_state(self, ref: str) -> float:
        if ref == SOLL_REF:
            store = self._store()
            if store is None:
                raise SourceUnavailable("Soll-Speicher noch nicht geladen")
            return store.value
        return self._ha.get_state(ref)

    def get_raw_state(self, ref: str) -> str:
        return str(self.get_state(ref)) if ref == SOLL_REF else self._ha.get_raw_state(ref)

    def device_key(self, ref: str) -> str:
        return ref if ref == SOLL_REF else self._ha.device_key(ref)


def _translated(read, ref: str):
    try:
        return read(ref)
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code == 404:
            raise SignalNotFound(ref) from error
        raise SourceUnavailable(str(error)) from error
    except requests.RequestException as error:
        raise SourceUnavailable(str(error)) from error
