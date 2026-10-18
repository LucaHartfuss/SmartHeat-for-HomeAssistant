"""Spec 5c 1.1: Supervisor-Discovery mit Geraete-ID, Zustand und (nur nicht uebernommen) Uebernahme-Code."""
import json
import logging

import pytest
import requests
from conftest import FakeClock
from fakes import datei_stand

from heizungsbruecke import discovery
from heizungsbruecke.supervisor import SupervisorApi

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


def test_two_alternately_failing_announcements_are_both_throttled(tmp_path):
    """5c-D-9 Teil 2: die Drossel merkt sich jede gescheiterte Meldung, nicht nur die letzte. Wechselt die Anzeige jede
    Sekunde zwischen zwei Meldungen, wird jede hoechstens einmal je 60 s versucht (59 s nichts, nach 60 s erneut)."""
    supervisor = Supervisor()
    supervisor.fail = True
    d, clock = _discovery(tmp_path, supervisor)
    for _ in range(60):  # Sekunde 0 bis 59
        d.aktualisieren("shg-a", "nicht_uebernommen", CODE)
        d.aktualisieren("shg-a", "gesperrt", None)
        clock.advance(1)
    assert supervisor.post_calls == 2
    d.aktualisieren("shg-a", "nicht_uebernommen", CODE)  # Sekunde 60
    assert supervisor.post_calls == 3
    d.aktualisieren("shg-a", "gesperrt", None)
    assert supervisor.post_calls == 4


def test_a_success_releases_only_its_own_announcement(tmp_path):
    """Gelingt eine Meldung, bleibt eine andere gescheiterte in ihrer Pause (sie scheiterte vielleicht an sich selbst)."""
    supervisor = Supervisor()
    supervisor.fail = True
    d, clock = _discovery(tmp_path, supervisor)
    d.aktualisieren("shg-a", "gesperrt", None)
    clock.advance(10)
    d.aktualisieren("shg-a", "nicht_uebernommen", CODE)
    supervisor.fail = False
    clock.advance(50)  # Sekunde 60 nach "gesperrt", 50 nach der Meldung mit Code
    d.aktualisieren("shg-a", "gesperrt", None)
    assert supervisor.posts == [{"geraet_id": "shg-a", "zustand": "gesperrt"}]
    d.aktualisieren("shg-a", "nicht_uebernommen", CODE)  # noch in ihrer Pause
    assert supervisor.post_calls == 3
    clock.advance(10)
    d.aktualisieren("shg-a", "nicht_uebernommen", CODE)
    assert supervisor.posts[-1] == {"geraet_id": "shg-a", "zustand": "nicht_uebernommen", "claim_code": CODE}


def test_the_throttle_remembers_sixteen_failed_announcements(tmp_path):
    """Die Drossel ist begrenzt: 16 gescheiterte Meldungen bleiben gedrosselt; eine 17. verdraengt die aelteste, die
    danach sofort wieder versucht wird."""
    supervisor = Supervisor()
    supervisor.fail = True
    d, _ = _discovery(tmp_path, supervisor)
    for n in range(16):
        d.aktualisieren(f"shg-{n}", "uebernommen", None)
    for n in range(16):
        d.aktualisieren(f"shg-{n}", "uebernommen", None)
    assert supervisor.post_calls == 16
    d.aktualisieren("shg-16", "uebernommen", None)  # die 17.: shg-0 faellt aus der Drossel
    assert supervisor.post_calls == 17
    d.aktualisieren("shg-1", "uebernommen", None)
    assert supervisor.post_calls == 17
    d.aktualisieren("shg-0", "uebernommen", None)
    assert supervisor.post_calls == 18


class SupervisorHttp:
    """Grenze Supervisor-HTTP (requests) wie der echte Supervisor (Probe 2026-10-12, docs/module/ha-host.md): eine
    Meldung je Add-on und Dienst, ein neuer POST ersetzt ihre config unter derselben UUID; GET /discovery ist dem
    Add-on verwehrt. meldungen: UUID -> config."""

    def __init__(self):
        self.meldungen: dict[str, dict] = {}
        self._uuids = iter(f"u-{n}" for n in range(1, 100))

    def post(self, url, *, json, headers, timeout):
        assert url == "http://supervisor/discovery" and json["service"] == "smartheat"
        uuid = next(iter(self.meldungen), None) or next(self._uuids)
        self.meldungen[uuid] = json["config"]
        return _Antwort({"result": "ok", "data": {"uuid": uuid}})

    def delete(self, url, *, headers, timeout):
        uuid = url.rsplit("/", 1)[1]
        return _Antwort({}, 200 if self.meldungen.pop(uuid, None) is not None else 404)


class _Antwort:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class _Absturz(BaseException):
    """Der Prozess endet zwischen POST und Speichern (kein Exception: aktualisieren faengt sie nicht)."""


def test_a_crash_between_post_and_saving_the_uuid_leaves_no_entry_with_the_code(tmp_path, monkeypatch):
    """5c-D-9 Teil 1: stuerzt der Prozess nach dem POST ab, bevor die UUID gespeichert ist, meldet der naechste Start
    sofort; der Supervisor ersetzt die Meldung unter derselben UUID, nach der Uebernahme steht kein Code mehr im
    Supervisor und keine zweite Meldung."""
    http = SupervisorHttp()
    monkeypatch.setattr(requests, "post", http.post)
    monkeypatch.setattr(requests, "delete", http.delete)
    api = SupervisorApi(token="t")
    pfad = tmp_path / "discovery.json"
    echt = discovery.dateien.schreiben_json

    def absturz(*args, **kwargs):
        raise _Absturz

    monkeypatch.setattr(discovery.dateien, "schreiben_json", absturz)
    with pytest.raises(_Absturz):
        discovery.Discovery(api, pfad, clock=FakeClock()).aktualisieren("shg-a", "nicht_uebernommen", CODE)
    assert http.meldungen == {"u-1": {"geraet_id": "shg-a", "zustand": "nicht_uebernommen", "claim_code": CODE}}
    assert not pfad.exists()
    monkeypatch.setattr(discovery.dateien, "schreiben_json", echt)
    neu = discovery.Discovery(api, pfad, clock=FakeClock())  # Neustart ohne gespeicherte UUID
    neu.aktualisieren("shg-a", "uebernommen", None)
    assert http.meldungen == {"u-1": {"geraet_id": "shg-a", "zustand": "uebernommen"}}
    assert json.loads(pfad.read_text())["uuid"] == "u-1"


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


def test_an_unremovable_uuid_is_never_given_up_and_retried_with_a_growing_pause(tmp_path, caplog):
    """5c-D-9 Teil 3: keine Aufgabe nach 10 Fehlversuchen; der Abstand waechst je Fehlversuch (60, 120, 240, 480, 960,
    1920 s) bis hoechstens 3600 s. Je Versuch eine Logzeile, nie "aufgegeben"."""
    supervisor = Supervisor()
    supervisor.stale = {"alt-1"}
    _queue(tmp_path, "alt-1")
    d, clock = _discovery(tmp_path, supervisor)
    start = clock()
    versuche = []
    for _ in range(30_000):  # 30 000 s im Sekundentakt der Anzeige
        vorher = supervisor.delete_calls
        d.aktualisieren("shg-a", "uebernommen", None)
        if supervisor.delete_calls > vorher:
            versuche.append(clock() - start)
        clock.advance(1)
    # der erste Aufruf entfernt zusaetzlich die eigene Vorgaengermeldung "aktuell"
    assert versuche == [0, 60, 180, 420, 900, 1860, 3780, 7380, 10980, 14580, 18180, 21780, 25380, 28980]
    assert sum("Alte Discovery nicht entfernt" in r.getMessage() for r in caplog.records) == 14
    assert not any("aufgegeben" in r.getMessage() for r in caplog.records)
    stored = json.loads((tmp_path / "discovery.json").read_text())
    assert stored["entfernen"] == ["alt-1"] and stored["versuche"] == {"alt-1": 14}
    supervisor.stale = set()  # der Supervisor nimmt sie wieder an: beim naechsten faelligen Versuch weg
    clock.advance(28980 + 3600 - (clock() - start))
    d.aktualisieren("shg-a", "uebernommen", None)
    assert supervisor.deletes[-1] == "alt-1"
    stored = json.loads((tmp_path / "discovery.json").read_text())
    assert stored["entfernen"] == [] and stored["versuche"] == {}


def test_the_pause_before_the_next_removal_is_due_exactly_on_time(tmp_path):
    """Grenze der wachsenden Pause: nach dem ersten Fehlversuch 59 s nichts, nach 60 s der zweite; danach 119 s nichts,
    nach 120 s der dritte."""
    supervisor = Supervisor()
    supervisor.stale = {"alt-1"}
    _queue(tmp_path, "alt-1")
    d, clock = _discovery(tmp_path, supervisor)
    d.aktualisieren("shg-a", "uebernommen", None)
    erste = supervisor.delete_calls  # alt-1 und die Vorgaengermeldung "aktuell"
    clock.advance(59)
    d.aktualisieren("shg-a", "uebernommen", None)
    assert supervisor.delete_calls == erste
    clock.advance(1)
    d.aktualisieren("shg-a", "uebernommen", None)
    assert supervisor.delete_calls == erste + 1
    clock.advance(119)
    d.aktualisieren("shg-a", "uebernommen", None)
    assert supervisor.delete_calls == erste + 1
    clock.advance(1)
    d.aktualisieren("shg-a", "uebernommen", None)
    assert supervisor.delete_calls == erste + 2


def test_a_new_old_entry_is_removed_at_once_while_another_waits(tmp_path):
    """Die Pause gilt je alte Meldung: eine neu vorgemerkte wartet nicht auf die Pause einer anderen."""
    supervisor = Supervisor()
    supervisor.stale = {"alt-1"}
    _queue(tmp_path, "alt-1")
    d, clock = _discovery(tmp_path, supervisor)
    d.aktualisieren("shg-a", "uebernommen", None)  # alt-1 scheitert, "aktuell" ist weg
    d.aktualisieren("shg-a", "nicht_uebernommen", None)  # neue UUID: uuid-1 wird vorgemerkt
    assert supervisor.deletes == ["aktuell", "uuid-1"]


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


def test_a_supervisor_timeout_is_retried_after_the_pause(tmp_path, monkeypatch, caplog):
    """Echte SupervisorApi, Zeitlimit am requests-Rand: aktualisieren wirft nicht, im Log nur die Fehlerart; 59 s
    spaeter kein neuer Versuch, nach 60 s geht die Meldung hinaus."""
    http = SupervisorHttp()
    versuche = []

    def haengt(url, **kw):
        versuche.append(url)
        raise requests.Timeout("Bearer geheim")

    monkeypatch.setattr(requests, "post", haengt)
    clock = FakeClock()
    d = discovery.Discovery(SupervisorApi(token="geheim"), tmp_path / "discovery.json", clock=clock)
    d.aktualisieren("shg-a", "uebernommen", None)
    assert len(versuche) == 1 and "Discovery nicht gemeldet (Timeout)" in caplog.text and "geheim" not in caplog.text
    clock.advance(59)
    d.aktualisieren("shg-a", "uebernommen", None)
    assert len(versuche) == 1
    monkeypatch.setattr(requests, "post", http.post)
    clock.advance(1)
    d.aktualisieren("shg-a", "uebernommen", None)
    assert http.meldungen == {"u-1": {"geraet_id": "shg-a", "zustand": "uebernommen"}}
