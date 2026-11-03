"""Die Laufzeit einer Anlage hat eine Instanz je Kreis: eigener Worker-Thread, Zustandsspeicher und Statusmelder, ein
Link, eine Meldesenke, ein Statusereignis der Anlage. Ein Kreis mit abgeschlossener Abo-Frist ruht, die anderen regeln
weiter; ein wieder aktives Abo nimmt nur diesen Kreis wieder auf, ohne Neustart und ohne die Verbindung zu kappen."""
import threading
from datetime import datetime

import pytest
from geraet_helpers import FakeGeraet, Sent, konfiguration_aus_optionen
from runtime_helpers import OPTIONS

from heizungsbruecke import config
from heizungsbruecke.host import HaHost
from smartheat_device.laufzeit import RuntimeChannel
from smartheat_runtime import app, entitlement, kreise
from smartheat_runtime.app import EV_RECHECK, StartFailure
from smartheat_runtime.runtime import Runtime
from smartheat_runtime.worker import Event, WorkerBeendet


class Senke:
    """Status- und Meldesenke in einem."""

    def __init__(self):
        self.ereignisse, self.schluessel = [], []

    def publish(self, event):
        self.ereignisse.append(event)

    def push(self, key, message):
        self.schluessel.append(key)

    def show(self, key, message):
        self.schluessel.append(key)

    def withdraw(self, key):
        self.schluessel.append(key)


class Boom(BaseException):
    pass


def _dokument(nummer, abo="aktiv"):
    dokument = konfiguration_aus_optionen({**OPTIONS, "abo": abo})
    dokument["kreise"][0]["kreis"] = nummer
    return dokument


class Anlage:
    """Test-Host mit einer Sicht je Kreis (HaHost auf einer Geraete-Attrappe), ein Link, eine Meldesenke."""

    def __init__(self, env, nummern=(1, 2)):
        self.env, self.nummern = env, list(nummern)
        self.status_sink = self.senke = Senke()
        self.abo = {n: entitlement.ACTIVE for n in nummern}
        self.sent = Sent()
        self.kanaele: list[tuple[int, RuntimeChannel]] = []
        self.hosts = {}
        self.defekt: set[int] = set()  # Kreise, deren Konfiguration beim Laden scheitert

    def kreise(self):
        return self.nummern

    def kreis_host(self, kreis):
        geraet = FakeGeraet(_dokument(kreis), abo=lambda: self.abo[kreis], bedienung={"raum_soll": 21.0})
        host = HaHost(geraet, self.env.ha, clock=self.env.clock)
        host.notify_sink = self.senke
        if kreis in self.defekt:
            def kaputt():
                raise StartFailure("Konfiguration: kaputt")
            host.load = kaputt
        self.hosts[kreis] = host
        return host

    def kanal(self, worker, kreis):
        kanal = RuntimeChannel(self.sent.publish, lambda: self.sent.connected, lambda: self.sent.failures)
        self.kanaele.append((kreis, kanal))
        return kanal.binden(worker)

    def ruhen_lassen(self, kreis, monkeypatch):
        """Die Abo-Frist des Kreises ist abgelaufen (er startet als abo_beendet)."""
        self.abo[kreis] = entitlement.INACTIVE
        entitlement.mark_inactive(config.entitlement_path(kreis), datetime.now().astimezone())
        monkeypatch.setattr("smartheat_runtime.entitlement.grace_expired", lambda since, now: True)


def _laufzeit(anlage, env, neustarts=None):
    """`neustarts` sammelt die Neustarts des ganzen Prozesses."""
    neustart = (lambda: neustarts.append("prozess")) if neustarts is not None else (lambda: None)
    return kreise.start(anlage, env.clock, mqtt_factory=anlage.kanal, neustart=neustart)


def _lauf_im_thread(laufzeit):
    fehler = []

    def lauf():
        try:
            laufzeit.run()
        except BaseException as error:  # noqa: BLE001
            fehler.append(error)

    thread = threading.Thread(target=lauf, daemon=True)
    thread.start()
    return thread, fehler


def _warten(bedingung, sekunden=5.0):
    ende = threading.Event()
    for _ in range(int(sekunden * 100)):
        if bedingung():
            return True
        ende.wait(0.01)
    return False


def test_zwei_kreise_laufen_mit_eigenem_zustand_und_einem_link(env):
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)

    eins, zwei = laufzeit.kreise()[1], laufzeit.kreise()[2]
    assert isinstance(eins, Runtime) and isinstance(zwei, Runtime)
    assert eins.worker is not zwei.worker and eins.store is not zwei.store
    assert eins.config.kreis == 1 and zwei.config.kreis == 2
    assert sorted(kreis for kreis, _ in anlage.kanaele) == [1, 2]
    assert config.backup_path(1).parent != config.backup_path(2).parent


def test_die_anlage_meldet_ein_ereignis_des_schlechtesten_kreises(env, monkeypatch):
    anlage = Anlage(env)
    anlage.ruhen_lassen(1, monkeypatch)
    laufzeit = _laufzeit(anlage, env)

    assert isinstance(laufzeit.kreise()[1], app.IdleBridge) and laufzeit.kreise()[1].reason == "abo_beendet"
    assert isinstance(laufzeit.kreise()[2], Runtime)
    assert anlage.senke.ereignisse[-1]["status"] == "abo_beendet"


def test_die_meldungen_mehrerer_kreise_tragen_den_kreis_im_schluessel(env):
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)
    laufzeit.kreise()[2].notifier.notify("probe", "gestoert", "Text", critical=True)
    laufzeit.kreise()[1].notifier.notify("probe", "gestoert", "Text", critical=True)

    schluessel = {k for k in anlage.senke.schluessel}
    assert {"kreis-1:probe", "kreis-2:probe"} <= schluessel
    assert all(k.startswith("kreis-") for k in schluessel)


def test_ein_einzelner_kreis_meldet_ohne_praefix(env):
    anlage = Anlage(env, nummern=(1,))
    laufzeit = _laufzeit(anlage, env)
    laufzeit.kreise()[1].notifier.notify("probe", "gestoert", "Text", critical=True)
    assert "probe" in anlage.senke.schluessel


def test_kreis_1_beendet_kreis_2_regelt_weiter(env, monkeypatch):
    anlage = Anlage(env)
    anlage.ruhen_lassen(1, monkeypatch)
    laufzeit = _laufzeit(anlage, env)
    _, fehler = _lauf_im_thread(laufzeit)
    zwei = laufzeit.kreise()[2]
    gesehen = []
    zwei.worker.register("probe", lambda event: gesehen.append(event.kind))

    zwei.worker.post(Event("probe"))

    assert _warten(lambda: gesehen == ["probe"])
    assert not fehler
    assert laufzeit.kreise()[1].reason == "abo_beendet"  # Kreis 1 ruht weiter


def test_wiederaufnahme_nimmt_nur_den_betroffenen_kreis_neu_auf(env, monkeypatch):
    anlage = Anlage(env)
    anlage.ruhen_lassen(1, monkeypatch)
    laufzeit = _laufzeit(anlage, env)
    _, fehler = _lauf_im_thread(laufzeit)
    zwei = laufzeit.kreise()[2]
    anlage.abo[1] = entitlement.ACTIVE
    kanaele_vorher = len(anlage.kanaele)

    assert laufzeit.wiederaufnehmen(1) is True

    neu = laufzeit.kreise()[1]
    assert isinstance(neu, Runtime) and laufzeit.kreise()[2] is zwei
    assert anlage.sent.connected is True
    assert [kreis for kreis, _ in anlage.kanaele[kanaele_vorher:]] == [1]  # nur Kreis 1 bekommt einen neuen Kanal
    gesehen = []
    neu.worker.register("probe", lambda event: gesehen.append(event.kind))
    neu.worker.post(Event("probe"))
    assert _warten(lambda: gesehen == ["probe"])  # der neue Worker laeuft
    assert not fehler


def test_ein_laufender_kreis_wird_nicht_wiederaufgenommen(env):
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)
    zwei = laufzeit.kreise()[2]

    assert laufzeit.wiederaufnehmen(2) is False
    assert laufzeit.kreise()[2] is zwei


def test_handler_der_aufsicht_gelten_auch_fuer_den_wiederaufgenommenen_kreis(env, monkeypatch):
    anlage = Anlage(env)
    anlage.ruhen_lassen(1, monkeypatch)
    laufzeit = _laufzeit(anlage, env)
    gesehen = []
    laufzeit.worker.register("reload", lambda event: gesehen.append(event.kind))
    anlage.abo[1] = entitlement.ACTIVE
    laufzeit.wiederaufnehmen(1)

    laufzeit.worker.post(Event("reload"))
    laufzeit.kreise()[1].worker.run_pending()

    assert gesehen.count("reload") == 1


def test_ein_neustart_im_worker_eines_kreises_erreicht_den_aufseher(env):
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)

    def neustart(event):
        raise Boom()

    laufzeit.kreise()[2].worker.register("neustart", neustart)
    laufzeit.kreise()[2].worker.post(Event("neustart"))

    with pytest.raises(Boom):
        laufzeit.run()


def test_beenden_laesst_den_worker_enden(env):
    anlage = Anlage(env, nummern=(1,))
    worker = _laufzeit(anlage, env).kreise()[1].worker
    worker.beenden()
    with pytest.raises(WorkerBeendet):
        worker.run()


def test_abo_wiederaufnahme_braucht_keinen_neustart_des_hosts(env, monkeypatch):
    anlage = Anlage(env)
    anlage.ruhen_lassen(1, monkeypatch)
    laufzeit = _laufzeit(anlage, env)
    alt, neu = _dokument(1, "inaktiv"), _dokument(1, "aktiv")
    for dokument in (alt, neu):
        dokument["kreise"].append({**_dokument(2)["kreise"][0]})
    anlage.abo[1] = entitlement.ACTIVE

    assert laufzeit.konfiguration_geaendert(alt, neu) is False
    assert isinstance(laufzeit.kreise()[1], Runtime)


def test_eine_laufzeitrelevante_aenderung_verlangt_den_neustart_des_hosts(env):
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)
    alt = _dokument(1)
    alt["kreise"].append(_dokument(2)["kreise"][0])
    neu = {**alt, "tagestick": "13:00"}
    assert laufzeit.konfiguration_geaendert(alt, neu) is True
    assert laufzeit.konfiguration_geaendert(alt, alt) is False
    assert laufzeit.konfiguration_geaendert(alt, {**alt, "kreise": alt["kreise"][:1]}) is True


def test_die_meldesenke_eines_kreises_setzt_den_kreis_vor_jeden_schluessel():
    senke = Senke()
    meldesenke = kreise._KreisMeldesenke(senke, 3)
    meldesenke.push("a", "Text")
    meldesenke.show("b", "Text")
    meldesenke.withdraw("c")
    assert senke.schluessel == ["kreis-3:a", "kreis-3:b", "kreis-3:c"]


def test_ein_kreis_ruht_im_ruhezustand_oder_waehrend_der_abo_frist(env):
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)
    zwei = laufzeit.kreise()[2]
    assert kreise.ruhend(zwei) is False

    zwei.store.update(abo_inactive_since=datetime.now().astimezone())
    assert kreise.ruhend(zwei) is True  # Frist laeuft

    zwei.store.update(abo_inactive_since=None)
    zwei.idle = True
    assert kreise.ruhend(zwei) is True  # Frist abgeschlossen



def test_die_wiederaufnahme_beendet_die_verbindungen_des_alten_kreises(env):
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)
    alt = laufzeit.kreise()[2]
    alter_kanal = next(kanal for kreis, kanal in anlage.kanaele if kreis == 2)
    assert alter_kanal.is_connected() is True
    alt.store.update(abo_inactive_since=datetime.now().astimezone())

    assert laufzeit.wiederaufnehmen(2) is True

    assert alter_kanal.is_connected() is False
    assert laufzeit.kreise()[2] is not alt


def test_vor_dem_lauf_startet_die_wiederaufnahme_keinen_thread(env):
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)
    laufzeit.kreise()[2].store.update(abo_inactive_since=datetime.now().astimezone())
    vorher = set(threading.enumerate())

    assert laufzeit.wiederaufnehmen(2) is True

    assert set(threading.enumerate()) == vorher


def test_ein_worker_der_nicht_endet_haelt_den_kreis_an_und_meldet_den_ausfall(env, monkeypatch):
    monkeypatch.setattr(kreise, "ABWICKLUNG_SECONDS", 0.05)
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)
    _, fehler = _lauf_im_thread(laufzeit)
    zwei = laufzeit.kreise()[2]
    zwei.store.update(abo_inactive_since=datetime.now().astimezone())
    beenden = zwei.worker.beenden
    monkeypatch.setattr(zwei.worker, "beenden", lambda: None)

    assert laufzeit.wiederaufnehmen(2) is False

    assert laufzeit.kreise()[2] is zwei
    assert _warten(lambda: bool(fehler))
    assert isinstance(fehler[0], RuntimeError)
    beenden()


def _in_start_anhalten(laufzeit, kreis, monkeypatch):
    """Der Neustart von `kreis` (app.start) bleibt haengen, bis `frei` gesetzt wird; `drin` meldet, dass er laeuft."""
    echt = laufzeit._starte_kreis
    drin, frei = threading.Event(), threading.Event()

    def starte(nummer):
        if nummer == kreis:
            drin.set()
            assert frei.wait(10)
        return echt(nummer)

    monkeypatch.setattr(laufzeit, "_starte_kreis", starte)
    return drin, frei


def test_scheitert_der_neustart_des_kreises_geht_der_ausfall_an_den_aufseher(env, monkeypatch):
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)
    alt = laufzeit.kreise()[2]
    alt.store.update(abo_inactive_since=datetime.now().astimezone())

    def kaputt(nummer):
        raise Boom()

    monkeypatch.setattr(laufzeit, "_starte_kreis", kaputt)

    assert laufzeit.wiederaufnehmen(2) is False

    with pytest.raises(Boom):  # der Prozess folgt dem Neustartweg des Hosts, der Kreis bleibt nicht ohne Regelung
        laufzeit.run()


def test_scheitert_der_neustart_im_laufenden_betrieb_erreicht_der_ausfall_den_lauf(env, monkeypatch):
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)
    _, fehler = _lauf_im_thread(laufzeit)
    laufzeit.kreise()[2].store.update(abo_inactive_since=datetime.now().astimezone())
    monkeypatch.setattr(laufzeit, "_starte_kreis", lambda nummer: (_ for _ in ()).throw(Boom()))

    assert laufzeit.wiederaufnehmen(2) is False

    assert _warten(lambda: bool(fehler))
    assert isinstance(fehler[0], Boom)


def test_gelingt_der_neustart_gibt_es_keinen_ausfall(env):
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)
    laufzeit.kreise()[2].store.update(abo_inactive_since=datetime.now().astimezone())

    assert laufzeit.wiederaufnehmen(2) is True

    assert laufzeit._ausfall.empty()


def test_waehrend_ein_kreis_neu_startet_bleiben_die_anderen_erreichbar(env, monkeypatch):
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)
    laufzeit.kreise()[1].store.update(abo_inactive_since=datetime.now().astimezone())
    drin, frei = _in_start_anhalten(laufzeit, 1, monkeypatch)
    aufnahme = threading.Thread(target=lambda: laufzeit.wiederaufnehmen(1), daemon=True)
    aufnahme.start()
    assert drin.wait(5)
    gesehen, fertig = [], threading.Event()

    def andere():
        laufzeit.worker.register("probe", lambda event: gesehen.append(event.kind))
        laufzeit.worker.post(Event("probe"))
        laufzeit.kreise()
        fertig.set()

    thread = threading.Thread(target=andere, daemon=True)
    thread.start()

    erreichbar = fertig.wait(3)
    frei.set()
    aufnahme.join(10)
    thread.join(10)
    assert erreichbar, "register/post/kreise warten auf den Neustart eines anderen Kreises"
    assert not aufnahme.is_alive()


def test_ein_handler_der_waehrend_des_neustarts_kommt_erreicht_auch_den_neuen_kreis(env, monkeypatch):
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)
    laufzeit.kreise()[1].store.update(abo_inactive_since=datetime.now().astimezone())
    drin, frei = _in_start_anhalten(laufzeit, 1, monkeypatch)
    aufnahme = threading.Thread(target=lambda: laufzeit.wiederaufnehmen(1), daemon=True)
    aufnahme.start()
    assert drin.wait(5)
    gesehen = []

    laufzeit.worker.register("spaet", lambda event: gesehen.append(event.kind))
    frei.set()
    aufnahme.join(10)

    laufzeit.worker.post(Event("spaet"))
    laufzeit.kreise()[1].worker.run_pending()
    assert gesehen.count("spaet") == 1


def test_eine_zweite_wiederaufnahme_desselben_kreises_waehrend_der_ersten_tut_nichts(env, monkeypatch):
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)
    laufzeit.kreise()[1].store.update(abo_inactive_since=datetime.now().astimezone())
    drin, frei = _in_start_anhalten(laufzeit, 1, monkeypatch)
    ergebnis = []
    aufnahme = threading.Thread(target=lambda: ergebnis.append(laufzeit.wiederaufnehmen(1)), daemon=True)
    aufnahme.start()
    assert drin.wait(5)

    zweite = laufzeit.wiederaufnehmen(1)

    frei.set()
    aufnahme.join(10)
    assert zweite is False and ergebnis == [True]


def _ersetzt(laufzeit, kreis, alt):
    return _warten(lambda: laufzeit.kreise()[kreis] is not alt)


def test_die_neupruefung_eines_kreises_im_konfigurationsfehler_startet_nur_diesen_kreis(env):
    anlage = Anlage(env)
    anlage.defekt.add(1)
    neustarts = []
    laufzeit = _laufzeit(anlage, env, neustarts)
    _, fehler = _lauf_im_thread(laufzeit)
    eins, zwei = laufzeit.kreise()[1], laufzeit.kreise()[2]
    assert isinstance(eins, app.IdleBridge) and eins.reason == app.STATUS_KONFIGURATIONSFEHLER
    anlage.defekt.clear()

    eins.worker.post(Event(EV_RECHECK))

    assert _ersetzt(laufzeit, 1, eins)
    assert isinstance(laufzeit.kreise()[1], Runtime)  # der Kreis regelt wieder
    assert laufzeit.kreise()[2] is zwei  # der andere Kreis lief durch
    assert neustarts == [] and not fehler  # kein Neustart des Prozesses


def test_die_neupruefung_eines_weiter_fehlerhaften_kreises_bleibt_im_fehler_ohne_prozessneustart(env):
    anlage = Anlage(env)
    anlage.defekt.add(1)
    neustarts = []
    laufzeit = _laufzeit(anlage, env, neustarts)
    _, fehler = _lauf_im_thread(laufzeit)
    eins, zwei = laufzeit.kreise()[1], laufzeit.kreise()[2]

    eins.worker.post(Event(EV_RECHECK))

    assert _ersetzt(laufzeit, 1, eins)
    neu = laufzeit.kreise()[1]
    assert isinstance(neu, app.IdleBridge) and neu.reason == app.STATUS_KONFIGURATIONSFEHLER
    assert laufzeit.kreise()[2] is zwei and neustarts == [] and not fehler
    # Die naechste Neupruefung laeuft wieder ueber den Kreis (der neue Ruhezustand plant sie selbst).
    anlage.defekt.clear()
    neu.worker.post(Event(EV_RECHECK))
    assert _warten(lambda: isinstance(laufzeit.kreise()[1], Runtime))


def test_eine_einkreisanlage_startet_bei_der_neupruefung_weiter_den_prozess_neu(env):
    anlage = Anlage(env, nummern=(1,))
    anlage.defekt.add(1)
    neustarts = []
    laufzeit = _laufzeit(anlage, env, neustarts)
    eins = laufzeit.kreise()[1]

    eins.worker.post(Event(EV_RECHECK))
    eins.worker.run_pending()

    assert neustarts == ["prozess"] and laufzeit.kreise()[1] is eins


def test_eine_laufzeitrelevante_aenderung_eines_kreises_startet_nur_diesen_kreis_neu(env):
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)
    eins, zwei = laufzeit.kreise()[1], laufzeit.kreise()[2]
    alt = _dokument(1)
    alt["kreise"].append(_dokument(2)["kreise"][0])
    neu = {**alt, "kreise": [alt["kreise"][0], {**alt["kreise"][1], "probe": 1}]}

    assert laufzeit.konfiguration_geaendert(alt, neu) is False  # der Prozess bleibt

    assert laufzeit.kreise()[1] is eins  # Kreis 1 unveraendert
    assert laufzeit.kreise()[2] is not zwei and isinstance(laufzeit.kreise()[2], Runtime)


def test_eine_anlagenweite_aenderung_verlangt_den_neustart_ohne_einen_kreis_anzufassen(env):
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)
    vorher = dict(laufzeit.kreise())
    alt = _dokument(1)
    alt["kreise"].append(_dokument(2)["kreise"][0])

    assert laufzeit.konfiguration_geaendert(alt, {**alt, "tagestick": "13:00"}) is True
    assert laufzeit.konfiguration_geaendert(alt, alt) is False
    assert dict(laufzeit.kreise()) == vorher


def test_eine_einkreisanlage_startet_bei_laufzeitrelevanter_aenderung_den_prozess_neu(env):
    anlage = Anlage(env, nummern=(1,))
    laufzeit = _laufzeit(anlage, env)
    eins = laufzeit.kreise()[1]
    alt = _dokument(1)

    assert laufzeit.konfiguration_geaendert(alt, {**alt, "kreise": [{**alt["kreise"][0], "probe": 1}]}) is True
    assert laufzeit.kreise()[1] is eins


def test_eine_einkreisanlage_nimmt_ein_wieder_aktives_abo_ohne_prozessneustart_auf(env, monkeypatch):
    anlage = Anlage(env, nummern=(1,))
    anlage.ruhen_lassen(1, monkeypatch)
    laufzeit = _laufzeit(anlage, env)
    anlage.abo[1] = entitlement.ACTIVE

    assert laufzeit.konfiguration_geaendert(_dokument(1, "inaktiv"), _dokument(1, "aktiv")) is False
    assert isinstance(laufzeit.kreise()[1], Runtime)


def test_der_rueckruf_gestartet_meldet_jeden_start_und_jede_wiederaufnahme_mit_dem_neuen_ergebnis(env, monkeypatch):
    anlage = Anlage(env)
    anlage.ruhen_lassen(1, monkeypatch)
    gemeldet = []
    laufzeit = kreise.start(anlage, env.clock, mqtt_factory=anlage.kanal, neustart=lambda: None,
                            gestartet=lambda kreis, ergebnis: gemeldet.append((kreis, ergebnis)))
    _lauf_im_thread(laufzeit)
    assert [kreis for kreis, _ in gemeldet] == [1, 2]
    assert all(ergebnis is laufzeit.kreise()[kreis] for kreis, ergebnis in gemeldet)

    anlage.abo[1] = entitlement.ACTIVE
    assert laufzeit.wiederaufnehmen(1) is True

    assert [kreis for kreis, _ in gemeldet] == [1, 2, 1]
    assert gemeldet[-1][1] is laufzeit.kreise()[1] and isinstance(gemeldet[-1][1], Runtime)
    assert laufzeit.wiederaufnehmen(2) is False  # ein laufender Kreis meldet keinen neuen Start
    assert [kreis for kreis, _ in gemeldet] == [1, 2, 1]


def test_der_neustart_eines_laufenden_kreises_nimmt_ihn_in_jedem_zustand_in_einem_daemon_thread_wieder_auf(env, monkeypatch):
    anlage = Anlage(env)
    laufzeit = _laufzeit(anlage, env)
    eins, zwei = laufzeit.kreise()[1], laufzeit.kreise()[2]
    gestartet = []
    echt = threading.Thread

    def thread(*args, **kwargs):
        gestartet.append(kwargs)
        return echt(*args, **kwargs)

    monkeypatch.setattr(kreise.threading, "Thread", thread)
    laufzeit._kreis_neustart(2)()  # zwei laeuft und wartet auf kein Abo: nur die erzwungene Wiederaufnahme ersetzt ihn

    assert _ersetzt(laufzeit, 2, zwei)
    assert laufzeit.kreise()[1] is eins
    assert gestartet[0]["daemon"] is True and gestartet[0]["kwargs"] == {"erzwingen": True}


def test_eine_einkreisanlage_nimmt_ein_wieder_aktives_abo_samt_laufzeitrelevanter_aenderung_ohne_prozessneustart_auf(env):
    anlage = Anlage(env, nummern=(1,))
    laufzeit = _laufzeit(anlage, env)
    alt = _dokument(1, "inaktiv")
    neu = {**_dokument(1, "aktiv"), "kreise": [{**_dokument(1, "aktiv")["kreise"][0], "probe": 1}]}

    assert laufzeit.prozessneustart_noetig(alt, neu) is False  # die Wiederaufnahme startet den Kreis mit dem neuen Stand
    assert laufzeit.prozessneustart_noetig(_dokument(1), {**_dokument(1), "kreise": [{**_dokument(1)["kreise"][0], "probe": 1}]}) is True
