"""Spec 5c 1.1: Supervisor-Discovery mit Geraete-ID, Zustand und (nur nicht uebernommen) Uebernahme-Code."""
import json
import logging

from conftest import FakeClock
from fakes import datei_stand

from heizungsbruecke import discovery

CODE = "ABCDEFGHIJKL"


class Supervisor:
    def __init__(self):
        self.posts, self.deletes, self.fail, self.fail_delete, self.fixed_uuid = [], [], False, False, None
        self.stale = set()  # UUIDs, deren Entfernen immer scheitert
        self.post_calls = self.delete_calls = 0

    def discovery_anmelden(self, config):
        self.post_calls += 1
        if self.fail:
            raise OSError(f"weg {config.get('claim_code', '')}")
        self.posts.append(config)
        return self.fixed_uuid or f"uuid-{len(self.posts)}"

    def discovery_entfernen(self, uuid):
        self.delete_calls += 1
        if self.fail_delete or uuid in self.stale:
            raise OSError("weg")
        self.deletes.append(uuid)


def _discovery(tmp_path, supervisor):
    clock = FakeClock()
    return discovery.Discovery(supervisor, tmp_path / "discovery.json", clock=clock), clock


def test_first_registration_carries_the_code(tmp_path):
    supervisor = Supervisor()
    d, _ = _discovery(tmp_path, supervisor)
    d.aktualisieren("shg-a", "nicht_uebernommen", CODE)
    assert supervisor.posts == [{"geraet_id": "shg-a", "zustand": "nicht_uebernommen", "claim_code": CODE}]
    assert json.loads((tmp_path / "discovery.json").read_text())["uuid"] == "uuid-1"


def test_die_meldung_hat_genau_die_felder_der_discovery(tmp_path):
    supervisor = Supervisor()
    meldung, _ = _discovery(tmp_path, supervisor)
    meldung.aktualisieren("shg-a", "nicht_uebernommen", CODE)
    assert set(supervisor.posts[-1]) == {"geraet_id", "zustand", "claim_code"}  # Literal des Vertrags
    meldung.aktualisieren("shg-a", "uebernommen", None)
    assert set(supervisor.posts[-1]) == {"geraet_id", "zustand"}


def test_discovery_drops_the_code_after_the_claim(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    supervisor = Supervisor()
    d, _ = _discovery(tmp_path, supervisor)
    d.aktualisieren("shg-a", "nicht_uebernommen", CODE)
    d.aktualisieren("shg-a", "uebernommen", CODE)  # der Code ist noch bekannt, darf aber nicht mehr hinaus
    assert supervisor.posts[-1] == {"geraet_id": "shg-a", "zustand": "uebernommen"}
    assert "claim_code" not in supervisor.posts[-1]
    assert supervisor.deletes == ["uuid-1"]  # die Meldung mit dem Code ist weg
    assert CODE not in (tmp_path / "discovery.json").read_text()
    assert CODE not in caplog.text


def test_a_state_other_than_unclaimed_never_carries_a_code(tmp_path):
    supervisor = Supervisor()
    d, _ = _discovery(tmp_path, supervisor)
    d.aktualisieren("shg-a", "gesperrt", CODE)
    d.aktualisieren("shg-a", None, CODE)
    assert all("claim_code" not in post for post in supervisor.posts)
    assert supervisor.posts[-1]["zustand"] == "startet"


def test_unclaimed_without_a_code_announces_without_and_follows_with_it(tmp_path):
    supervisor = Supervisor()
    d, _ = _discovery(tmp_path, supervisor)
    d.aktualisieren("shg-a", "nicht_uebernommen", None)
    d.aktualisieren("shg-a", "nicht_uebernommen", CODE)
    assert supervisor.posts == [{"geraet_id": "shg-a", "zustand": "nicht_uebernommen"},
                                {"geraet_id": "shg-a", "zustand": "nicht_uebernommen", "claim_code": CODE}]
    assert supervisor.deletes == ["uuid-1"]


def test_unchanged_state_is_not_resent_and_a_restart_replaces_the_old_entry(tmp_path):
    supervisor = Supervisor()
    d, _ = _discovery(tmp_path, supervisor)
    d.aktualisieren("shg-a", "uebernommen", None)
    d.aktualisieren("shg-a", "uebernommen", None)
    assert len(supervisor.posts) == 1
    neu, _ = _discovery(tmp_path, supervisor)  # Prozessneustart
    neu.aktualisieren("shg-a", "uebernommen", None)
    assert supervisor.deletes == ["uuid-1"] and len(supervisor.posts) == 2
    assert json.loads((tmp_path / "discovery.json").read_text())["uuid"] == "uuid-2"


def test_a_restart_keeps_an_announcement_the_supervisor_deduplicated(tmp_path):
    supervisor = Supervisor()
    supervisor.fixed_uuid = "gleich"  # der Supervisor liefert fuer dieselbe Meldung dieselbe UUID
    d, _ = _discovery(tmp_path, supervisor)
    d.aktualisieren("shg-a", "uebernommen", None)
    neu, _ = _discovery(tmp_path, supervisor)
    neu.aktualisieren("shg-a", "uebernommen", None)
    assert supervisor.deletes == []


def test_supervisor_errors_are_retried_after_the_pause(tmp_path):
    supervisor = Supervisor()
    supervisor.fail = True
    d, clock = _discovery(tmp_path, supervisor)
    d.aktualisieren("shg-a", "uebernommen", None)
    supervisor.fail = False
    d.aktualisieren("shg-a", "uebernommen", None)  # zu frueh: kein neuer Versuch
    assert supervisor.posts == []
    clock.advance(discovery.RETRY_SECONDS)
    d.aktualisieren("shg-a", "uebernommen", None)
    assert len(supervisor.posts) == 1


def test_a_down_supervisor_is_not_hammered_and_the_log_is_not_flooded(tmp_path, caplog):
    supervisor = Supervisor()
    supervisor.fail = True
    d, clock = _discovery(tmp_path, supervisor)
    for _ in range(3 * discovery.RETRY_SECONDS):  # ein Aufruf je Sekunde, drei Minuten lang
        d.aktualisieren("shg-a", "nicht_uebernommen", CODE)
        clock.advance(1)
    assert supervisor.post_calls == 3
    assert len(caplog.records) == 3
    assert "OSError" in caplog.text and CODE not in caplog.text and "weg" not in caplog.text


def test_a_changed_announcement_is_sent_at_once_even_during_the_pause(tmp_path):
    supervisor = Supervisor()
    supervisor.fail = True
    d, _ = _discovery(tmp_path, supervisor)
    d.aktualisieren("shg-a", "nicht_uebernommen", CODE)
    supervisor.fail = False
    d.aktualisieren("shg-a", "uebernommen", None)  # der Code ist weg: sofort melden
    assert supervisor.posts == [{"geraet_id": "shg-a", "zustand": "uebernommen"}]


def test_an_old_entry_that_cannot_be_removed_is_retried_and_survives_a_restart(tmp_path):
    supervisor = Supervisor()
    d, _ = _discovery(tmp_path, supervisor)
    d.aktualisieren("shg-a", "nicht_uebernommen", CODE)
    supervisor.fail_delete = True
    d.aktualisieren("shg-a", "uebernommen", None)
    d.aktualisieren("shg-a", "uebernommen", None)  # Pause: kein erneuter Versuch
    assert supervisor.delete_calls == 1 and supervisor.deletes == []
    neu, _ = _discovery(tmp_path, supervisor)  # Neustart: die vorgemerkte Meldung bleibt vorgemerkt
    supervisor.fail_delete = False
    neu.aktualisieren("shg-a", "uebernommen", None)
    assert supervisor.deletes == ["uuid-1", "uuid-2"]  # die vorgemerkte und die des frueheren Prozesses
    assert json.loads((tmp_path / "discovery.json").read_text())["entfernen"] == []


def _queue(tmp_path, *uuids):
    (tmp_path / "discovery.json").write_text(json.dumps({"uuid": "aktuell", "entfernen": list(uuids)}))


def test_a_stale_uuid_does_not_block_the_ones_behind_it(tmp_path):
    supervisor = Supervisor()
    supervisor.stale = {"alt-1"}
    _queue(tmp_path, "alt-1", "alt-2")
    d, _ = _discovery(tmp_path, supervisor)
    d.aktualisieren("shg-a", "uebernommen", None)
    assert supervisor.deletes == ["alt-2", "aktuell"]
    stored = json.loads((tmp_path / "discovery.json").read_text())
    assert stored["entfernen"] == ["alt-1"]


def test_an_unremovable_uuid_is_dropped_after_the_limit_with_one_log_line(tmp_path, caplog):
    supervisor = Supervisor()
    supervisor.stale = {"alt-1"}
    _queue(tmp_path, "alt-1")
    d, clock = _discovery(tmp_path, supervisor)
    for _ in range(discovery.MAX_ENTFERNEN_VERSUCHE + 5):
        d.aktualisieren("shg-a", "uebernommen", None)
        clock.advance(discovery.RETRY_SECONDS)
    alt_calls = supervisor.delete_calls
    assert alt_calls == discovery.MAX_ENTFERNEN_VERSUCHE + 1  # + die eigene Vorgaengermeldung "aktuell"
    assert sum("aufgegeben" in r.getMessage() for r in caplog.records) == 1
    stored = json.loads((tmp_path / "discovery.json").read_text())
    assert stored["entfernen"] == [] and stored["versuche"] == {}


def test_the_attempt_count_survives_a_restart(tmp_path):
    supervisor = Supervisor()
    supervisor.stale = {"alt-1"}
    _queue(tmp_path, "alt-1")
    d, clock = _discovery(tmp_path, supervisor)
    d.aktualisieren("shg-a", "uebernommen", None)
    neu, _ = _discovery(tmp_path, supervisor)
    assert neu._versuche == {"alt-1": 1}


def test_the_state_file_is_only_rewritten_when_the_queue_changes(tmp_path):
    """Geschrieben heisst: discovery.json hat einen neuen Stand auf dem Datentraeger (fakes.datei_stand)."""
    supervisor = Supervisor()
    supervisor.stale = {"alt-1"}
    _queue(tmp_path, "alt-1")
    d, clock = _discovery(tmp_path, supervisor)
    d.aktualisieren("shg-a", "uebernommen", None)
    pfad = tmp_path / "discovery.json"
    stand = datei_stand(pfad)
    d._aufraeumen()  # Pause: nichts
    assert datei_stand(pfad) == stand
    clock.advance(discovery.RETRY_SECONDS)
    d.aktualisieren("shg-a", "uebernommen", None)  # Versuch zaehlt hoch: aendert den Stand
    assert datei_stand(pfad) != stand and json.loads(pfad.read_text())["versuche"] == {"alt-1": 2}
    stand = datei_stand(pfad)
    supervisor.fail_delete = False
    d._veraltet.clear()
    clock.advance(discovery.RETRY_SECONDS)  # nach der Pause laeuft das Aufraeumen wirklich
    d._aufraeumen()  # leere Warteschlange: kein Schreiben
    assert datei_stand(pfad) == stand


def test_a_corrupt_state_file_counts_as_empty(tmp_path):
    (tmp_path / "discovery.json").write_text("{kaputt")
    supervisor = Supervisor()
    d, _ = _discovery(tmp_path, supervisor)
    d.aktualisieren("shg-a", "uebernommen", None)
    assert len(supervisor.posts) == 1 and supervisor.deletes == []


def test_a_broken_state_directory_does_not_raise(tmp_path, caplog):
    supervisor = Supervisor()
    d = discovery.Discovery(supervisor, tmp_path / "gibtsnicht" / "x" / "discovery.json")
    (tmp_path / "gibtsnicht").write_text("datei statt Ordner")
    d.aktualisieren("shg-a", "uebernommen", None)  # wirft nicht
    assert "Discovery fehlgeschlagen" in caplog.text


def test_the_code_never_reaches_the_log(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    supervisor = Supervisor()
    supervisor.fail = True
    d, clock = _discovery(tmp_path, supervisor)
    d.aktualisieren("shg-a", "nicht_uebernommen", CODE)
    supervisor.fail = False
    clock.advance(discovery.RETRY_SECONDS)
    d.aktualisieren("shg-a", "nicht_uebernommen", CODE)
    supervisor.fail_delete = True
    d.aktualisieren("shg-a", "uebernommen", None)
    assert caplog.records and CODE not in caplog.text
