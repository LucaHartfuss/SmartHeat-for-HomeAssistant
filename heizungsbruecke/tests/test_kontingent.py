"""Kontingent der Quelle: ein erschoepftes Abfragekontingent wird dem Kunden einmal gemeldet (nur Push), die Rueckkehr
entwarnt; Quellen ohne Kontingent (HA) und die Aussentemperatur-Ueberwachung bleiben davon unberuehrt."""
from types import SimpleNamespace

from fakes import FakeNotifySink

from smartheat_runtime import aussentemperatur, kontingent
from smartheat_runtime.notifier import STATE_OK, Notifier
from smartheat_runtime.ports import QuelleErschoepft
from smartheat_runtime.roles import ChannelManifest

TEXT = "Das Abfragekontingent bei Viessmann ist erschöpft."


class _Quelle:
    def __init__(self) -> None:
        self.meldung: str | None = None

    def kontingent_meldung(self) -> str | None:
        return self.meldung

    def get_state(self, ref):
        raise QuelleErschoepft(TEXT)


def _rt(make_store, signals):
    store = make_store()
    kunde = FakeNotifySink()
    return SimpleNamespace(manifest=ChannelManifest(refs={"outdoor_temp": "treiber:outdoor_temp"}), signals=signals,
                           store=store, notifier=Notifier(store, kunde), kunde=kunde)


def test_erschoepftes_kontingent_meldet_einmal_und_entwarnt(make_store):
    quelle = _Quelle()
    rt = _rt(make_store, quelle)

    for _ in range(3):
        kontingent.check(rt)
    assert rt.kunde.pushes == []  # Kontingent in Ordnung

    quelle.meldung = TEXT
    for _ in range(3):
        kontingent.check(rt)
    assert [key for key, _text in rt.kunde.pushes] == ["kontingent"]  # einmal je Wechsel
    assert TEXT in rt.kunde.pushes[0][1]
    assert rt.kunde.shown == {}  # nicht kritisch: nur ein Push
    assert rt.notifier.state("kontingent") == kontingent.STATE_ERSCHOEPFT

    quelle.meldung = None
    kontingent.check(rt)
    assert [key for key, _text in rt.kunde.pushes] == ["kontingent"] * 2
    assert "wieder" in rt.kunde.pushes[-1][1]
    assert rt.notifier.state("kontingent") == STATE_OK


def test_quelle_ohne_kontingent_hat_nichts_zu_melden(make_store):
    rt = _rt(make_store, SimpleNamespace())  # wie die Quelle des HA-Hosts: kein kontingent_meldung
    kontingent.check(rt)
    assert rt.kunde.pushes == [] and rt.notifier.state("kontingent") == STATE_OK


def test_aussentemperatur_zaehlt_ein_erschoepftes_kontingent_nicht_als_ausfall(make_store):
    """Die Meldung zum Kontingent erklaert den fehlenden Wert; keine zweite Meldung zur Aussentemperatur, kein Fehler."""
    rt = _rt(make_store, _Quelle())
    rt.texts = SimpleNamespace(source_unavailable_log="Quelle nicht erreichbar")
    for _ in range(10):
        aussentemperatur.check(rt)
    assert rt.kunde.pushes == [] and rt.store.state.outdoor_misses == 0
