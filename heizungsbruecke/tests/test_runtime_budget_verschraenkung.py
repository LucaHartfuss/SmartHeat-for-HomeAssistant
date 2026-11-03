"""Zwei Kreise zaehlen an denselben Anlagenschluesseln, ohne die Transaktion zu halten (kein Tageslimit am Binding):
ein Schreiben eines Kreisschluessels reicht die beim Lesen gesehenen Anlagenschluessel durch und darf die Zaehlung eines
anderen Kreises nicht ueberschreiben. Die Verschraenkung ist erzwungen: der erste Kreis liest die zusammengesetzte
Sicht und wartet, bis der zweite gezaehlt hat."""
import threading

import pytest

from smartheat_core import write_budget
from smartheat_runtime import backup_store
from smartheat_runtime.budget_store import AnlagenBudget, BudgetStore
from smartheat_runtime.state import StateStore

DAY = "2026-10-03"
GRENZE = 10


@pytest.fixture(autouse=True)
def _tag(monkeypatch):
    monkeypatch.setattr(write_budget, "today", lambda: DAY)


class WartenderStore(BudgetStore):
    """Wartet nach dem Lesen der Sicht im Thread `wartender`, bis der andere Kreis fertig ist."""

    def __init__(self, kreis, anlage):
        super().__init__(kreis, anlage)
        self.gelesen = threading.Event()
        self.weiter = threading.Event()
        self.wartender: int | None = None

    @property
    def state(self):
        stand = super().state
        if threading.get_ident() == self.wartender:
            self.gelesen.set()
            assert self.weiter.wait(5)
        return stand


def _stores(tmp_path):
    anlage = AnlagenBudget(tmp_path / "anlage_budget.json")
    stores = []
    for kreis in (1, 2):
        ordner = tmp_path / f"kreis-{kreis}"
        stores.append((WartenderStore if kreis == 1 else BudgetStore)(
            StateStore(ordner / "backup.json", ordner / "failsafe_state.json"), anlage))
    return stores[0], stores[1], anlage


def _verschraenkt(erster, zweiter, schreiben_erster, schreiben_zweiter) -> None:
    """`schreiben_erster` liest im Thread, `schreiben_zweiter` laeuft zwischen Lesen und Schreiben des ersten."""
    fehler: list[BaseException] = []

    def lauf():
        erster.wartender = threading.get_ident()
        try:
            schreiben_erster()
        except BaseException as error:  # noqa: BLE001 - im Hauptthread erneut ausloesen
            fehler.append(error)

    thread = threading.Thread(target=lauf)
    thread.start()
    assert erster.gelesen.wait(5)
    try:
        schreiben_zweiter()
    finally:
        erster.weiter.set()
        thread.join(5)
    assert not thread.is_alive()
    if fehler:
        raise fehler[0]


def _anlagen_eintrag(tmp_path, key):
    return backup_store.load_backup(tmp_path / "anlage_budget.json")["write_budget"].get(key)


def test_ein_kreisschluessel_ohne_transaktion_ueberschreibt_die_zaehlung_des_anderen_kreises_nicht(tmp_path):
    eins, zwei, anlage = _stores(tmp_path)
    write_budget.put(eins, write_budget.TOTAL, {"day": DAY, "count": 3, "last": 1.0})

    _verschraenkt(
        eins, zwei,
        lambda: write_budget.put(eins, write_budget.RESTORE, {"day": DAY, "count": 1}),
        lambda: write_budget.put(zwei, write_budget.TOTAL, {"day": DAY, "count": 4, "last": 2.0}),
    )

    assert _anlagen_eintrag(tmp_path, write_budget.TOTAL)["count"] == 4
    assert eins.state.write_budget[write_budget.RESTORE] == {"day": DAY, "count": 1}
    assert zwei.state.write_budget[write_budget.TOTAL]["count"] == 4


def test_ein_anlagenschluessel_den_der_erste_kreis_selbst_aendert_geht_durch(tmp_path):
    eins, zwei, _ = _stores(tmp_path)
    write_budget.put(eins, write_budget.TOTAL, {"day": DAY, "count": 3, "last": 1.0})

    _verschraenkt(
        eins, zwei,
        lambda: write_budget.put(eins, write_budget.BOOST_START, {"day": DAY, "count": 1}),
        lambda: write_budget.put(zwei, write_budget.EMERGENCY_START, {"day": DAY, "count": 2}),
    )

    gespeichert = backup_store.load_backup(tmp_path / "anlage_budget.json")["write_budget"]
    assert gespeichert["boost_start"] == {"day": DAY, "count": 1}
    assert gespeichert["emergency_start"] == {"day": DAY, "count": 2}
    assert gespeichert["writes:total"]["count"] == 3


def test_zwei_kreise_die_gleichzeitig_zaehlen_verlieren_keinen_schritt(tmp_path):
    eins, zwei, _ = _stores(tmp_path)
    write_budget.put(eins, write_budget.TOTAL, {"day": DAY, "count": 3, "last": 1.0})

    _verschraenkt(
        eins, zwei,
        lambda: write_budget.put(eins, write_budget.TOTAL, {"day": DAY, "count": 4, "last": 5.0}),
        lambda: write_budget.put(zwei, write_budget.TOTAL, {"day": DAY, "count": 4, "last": 2.0}),
    )

    eintrag = _anlagen_eintrag(tmp_path, write_budget.TOTAL)
    assert eintrag["count"] == 5
    assert eintrag["last"] == 5.0


def test_ein_neuer_tag_in_beiden_kreisen_zaehlt_beide_schritte(tmp_path):
    eins, zwei, _ = _stores(tmp_path)
    write_budget.put(eins, write_budget.TOTAL, {"day": "2026-10-02", "count": 8, "last": 1.0})

    _verschraenkt(
        eins, zwei,
        lambda: write_budget.put(eins, write_budget.TOTAL, {"day": DAY, "count": 1, "last": 5.0}),
        lambda: write_budget.put(zwei, write_budget.TOTAL, {"day": DAY, "count": 1, "last": 2.0}),
    )

    assert _anlagen_eintrag(tmp_path, write_budget.TOTAL)["count"] == 2


def test_die_lebensdauer_zweier_kreise_verliert_keinen_schritt(tmp_path):
    eins, zwei, anlage = _stores(tmp_path)
    eins.update(lifetime_writes=10)

    _verschraenkt(
        eins, zwei,
        lambda: eins.update(lifetime_writes=eins.state.lifetime_writes + 1),
        lambda: zwei.update(lifetime_writes=zwei.state.lifetime_writes + 1),
    )

    assert anlage.lifetime_writes == 12


def test_ein_loeschen_im_anderen_kreis_bleibt_und_ein_eigenes_loeschen_wirkt(tmp_path):
    eins, zwei, _ = _stores(tmp_path)
    write_budget.put(eins, write_budget.ZONE_PREPARE, {"day": DAY, "count": 1})
    write_budget.put(eins, write_budget.BOOST_START, {"day": DAY, "count": 1})

    _verschraenkt(
        eins, zwei,
        lambda: write_budget.put(eins, write_budget.BOOST_START, None),
        lambda: write_budget.put(zwei, write_budget.ZONE_PREPARE, None),
    )

    assert set(backup_store.load_backup(tmp_path / "anlage_budget.json").get("write_budget", {})) == set()


@pytest.mark.parametrize(("gezaehlt", "erreicht"), [(GRENZE - 1, False), (GRENZE, True)])
def test_das_tageslimit_sieht_die_zaehlung_des_anderen_kreises_am_randwert(tmp_path, gezaehlt, erreicht):
    eins, zwei, _ = _stores(tmp_path)
    write_budget.put(eins, write_budget.TOTAL, {"day": DAY, "count": GRENZE - 2, "last": 1.0})

    _verschraenkt(
        eins, zwei,
        lambda: write_budget.put(eins, write_budget.RESTORE, {"day": DAY, "count": 1}),
        lambda: write_budget.put(zwei, write_budget.TOTAL, {"day": DAY, "count": gezaehlt, "last": 2.0}),
    )

    zaehler = write_budget.count_today(write_budget.get(eins, write_budget.TOTAL), DAY)
    assert zaehler == gezaehlt
    assert (zaehler >= GRENZE) is erreicht


# Das Zusammenfuehren selbst, ohne Threads: Randwerte der Rechnung.
def _eintrag(count, last=None, day=DAY):
    eintrag = {"day": day, "count": count}
    if last is not None:
        eintrag["last"] = last
    return eintrag


@pytest.mark.parametrize(("aktuell", "neu", "gelesen", "erwartet"), [
    (_eintrag(4), _eintrag(2), _eintrag(1), 5),    # Zuwachs 1 auf den aktuellen Stand
    (_eintrag(2), _eintrag(1), _eintrag(5), 0),    # nie unter 0
    (_eintrag(0), _eintrag(2), _eintrag(5), 0),
    (_eintrag(3), _eintrag(1), None, 4),           # kein gelesener Eintrag: alles ist Zuwachs
    (_eintrag(3), _eintrag(1), _eintrag(7, day="2026-10-02"), 4),  # gelesener Eintrag eines anderen Tages zaehlt nicht
])
def test_the_count_takes_over_only_the_increase(aktuell, neu, gelesen, erwartet):
    from smartheat_runtime.budget_store import _eintrag_eingefuegt
    assert _eintrag_eingefuegt(aktuell, neu, gelesen)["count"] == erwartet


def test_the_latest_start_time_wins_and_may_be_missing():
    from smartheat_runtime.budget_store import _eintrag_eingefuegt
    assert _eintrag_eingefuegt(_eintrag(1, last=9.0), _eintrag(2, last=5.0), _eintrag(1))["last"] == 9.0
    assert _eintrag_eingefuegt(_eintrag(1, last=3.0), _eintrag(2, last=5.0), _eintrag(1))["last"] == 5.0
    ohne = _eintrag_eingefuegt(_eintrag(1), _eintrag(2), _eintrag(1))
    assert "last" not in ohne and ohne["count"] == 2


@pytest.mark.parametrize("zaehler", ["3", None, 2.5, True])
def test_an_entry_without_an_integer_count_is_taken_as_it_is(zaehler):
    from smartheat_runtime.budget_store import _eintrag_eingefuegt
    neu = {"day": DAY, "count": zaehler}
    assert _eintrag_eingefuegt(_eintrag(4), neu, _eintrag(1)) == neu
    assert _eintrag_eingefuegt({"day": DAY, "count": zaehler}, _eintrag(2), _eintrag(1)) == _eintrag(2)


@pytest.mark.parametrize(("aktuell", "neu", "gelesen", "erwartet"), [
    (7, 5, 3, 9),      # Zuwachs 2
    (5, 0, 3, 0),      # Zuruecksetzen gilt absolut
    (5, 1, None, 1),   # ohne gelesenen Stand absolut
    (2, 1, 5, 0),      # nie unter 0
])
def test_the_lifetime_takes_over_the_increase_but_a_reset_is_absolute(aktuell, neu, gelesen, erwartet):
    from smartheat_runtime.budget_store import _lebensdauer
    assert _lebensdauer(aktuell, neu, gelesen) == erwartet
