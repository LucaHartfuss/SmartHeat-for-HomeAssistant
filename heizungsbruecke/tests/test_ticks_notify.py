"""Zuordnung der delivery-Meldungen zu Meldeschluesseln (Spec TP6 3.4, Praezisierung 2)."""
from unittest.mock import MagicMock

import pytest

from heizungsbruecke import delivery
from heizungsbruecke.notifier import Notifier
from heizungsbruecke.ticks import _notice, seed_notices


@pytest.mark.parametrize("kind,detail,expected", [
    (delivery.NOTIFY_NOTBETRIEB_ON, (), ("notbetrieb", "aktiv")),
    (delivery.NOTIFY_NOTBETRIEB_OFF, (), ("notbetrieb", "ok")),
    (delivery.NOTIFY_DATENFEHLER_LOCAL, ("dat", "room_actual"), ("datenfehler", "lokal:dat,room_actual")),
    (delivery.NOTIFY_DATENFEHLER_SERVER, ("unplausibler Wert für dat: 99 (erlaubt -40–45)",),
     ("datenfehler", "server:unplausibler Wert für dat")),
    (delivery.NOTIFY_DATENFEHLER_SERVER, (), ("datenfehler", "server:")),
    (delivery.NOTIFY_DATENFEHLER_WRITE, ("curve", "number.x", "503"), ("datenfehler", "anlage")),
    (delivery.NOTIFY_DATENFEHLER_RESOLVED, (), ("datenfehler", "ok")),
    (delivery.NOTIFY_WRITE_RESOLVED, (), ("datenfehler", "ok")),
])
def test_notice_maps_every_delivery_kind(kind, detail, expected):
    assert _notice(kind, detail) == expected


def test_notice_rejects_unknown_kind():
    with pytest.raises(ValueError):
        _notice("gibt_es_nicht", ())


def test_seed_notices_takes_over_persisted_notbetrieb_and_fault(make_store):
    notifier = Notifier(make_store(), MagicMock(), [])
    state = delivery.DeliveryState(
        notbetrieb=True, datenfehler=delivery.DataFault(delivery.SOURCE_SERVER, ("unplausibel: 99",)),
    )

    seed_notices(notifier, state)

    assert notifier.state("notbetrieb") == "aktiv"
    assert notifier.state("datenfehler") == "server:unplausibel"


def test_seed_notices_leaves_a_clean_state_alone(make_store):
    notifier = Notifier(make_store(), MagicMock(), [])

    seed_notices(notifier, delivery.DeliveryState())

    assert notifier.state("notbetrieb") == "ok"
    assert notifier.state("datenfehler") == "ok"
