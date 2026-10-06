"""SignalSource ueber die HA-REST-API (Spec SHG 3.2): Referenzen sind Entity-IDs bzw. `entity::attribut`
(ha_api.get_state). Uebersetzt die requests-Fehler in die Fehler des Ports; ungueltige Werte bleiben ValueError/
KeyError/TypeError."""
import requests

from smartheat_runtime.ports import SignalNotFound, SourceUnavailable


class HaSignalSource:
    def __init__(self, ha_api) -> None:
        self._ha_api = ha_api

    def get_state(self, ref: str) -> float:
        return _translated(self._ha_api.get_state, ref)

    def get_raw_state(self, ref: str) -> str:
        return _translated(self._ha_api.get_raw_state, ref)

    def device_key(self, ref: str) -> str:
        return ref.partition("::")[0]


def _translated(read, ref: str):
    try:
        return read(ref)
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code == 404:
            raise SignalNotFound(ref) from error
        raise SourceUnavailable(str(error)) from error
    except requests.RequestException as error:
        raise SourceUnavailable(str(error)) from error
