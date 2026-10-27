"""Knopf-Dienst (GP8, FR-21, GW 7.1/7.2): die Folgen-Erkennung (rein, Uhr von aussen), das Finden und Lesen des Geraets
und die Ausgaben. Die Grenzwerte der freigegebenen Folge (Regel 4) stehen als `@example` an jeder Kante: Druck 1,0/1,01 s,
Pause 2,0/2,01 s, fuenftes Druecken 10,0/10,1 s nach dem ersten, Halten 2,9/3,0/4,5/4,6 s, Halten ab 4,9/5,0 s nach dem
fuenften Loslassen, Fenster 60,0/60,01 s. Je Ablehnungsart ein eigener Test (Zaehler > 0 durch die Beispiele)."""
import contextlib
import json
import logging
import os
import signal
import struct

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from smartheat_host import knopf

T0 = 100.0  # beliebiger Start der monotonen Uhr; alle Zeiten unten sind Vielfache von 1/1000 s


def druck(folge: knopf.KnopfFolge, start: float, dauer: float) -> list[str]:
    """Ein Druck: Drücken bei `start`, Loslassen nach `dauer`; die Ereignisse beider Schritte in Reihenfolge."""
    return folge.druecken(start) + folge.loslassen(start + dauer)


def spiele(folge: knopf.KnopfFolge, druecke: list[tuple[float, float]]) -> list[str]:
    """Mehrere Druecke (Start, Dauer) nacheinander."""
    ereignisse: list[str] = []
    for start, dauer in druecke:
        ereignisse += druck(folge, start, dauer)
    return ereignisse


def fuenf(dauer: float = 0.5, abstand: float = 2.0, erster: float = T0) -> list[tuple[float, float]]:
    """Fuenf gleiche Druecke, `abstand` zwischen Loslassen und naechstem Druecken."""
    return [(erster + i * (dauer + abstand), dauer) for i in range(5)]


def bereit(folge: knopf.KnopfFolge) -> float:
    """Oeffnet das Reset-Fenster; Rueckgabe: Zeitpunkt des fuenften Loslassens."""
    druecke = fuenf(dauer=0.5, abstand=1.0)
    assert spiele(folge, druecke) == ["reset_bereit"]
    return druecke[-1][0] + druecke[-1][1]


def ms(wert: int) -> float:
    return wert / 1000


# --- die freigegebenen Parameter -------------------------------------------------------------------------------------

def test_die_parameter_sind_die_freigegebenen():
    """Regel 4: keine Aenderung ohne Rueckfrage. Ein anderer Wert macht diesen Test rot."""
    assert (knopf.CODE_DRUCK_MAX, knopf.CODE_RUHE, knopf.KURZ_MAX, knopf.FOLGE_FENSTER) == (2.0, 2.0, 1.0, 10.0)
    assert (knopf.FOLGE_DRUECKE, knopf.BEREIT_SEKUNDEN, knopf.BESTAETIGUNG_FRUEHESTENS) == (5, 60.0, 5.0)
    assert (knopf.HALTEN_MIN, knopf.HALTEN_MAX) == (3.0, 4.5)


# --- Code: genau ein Druck, danach Ruhe ------------------------------------------------------------------------------

def test_ein_kurzer_druck_ergibt_code_nach_zwei_sekunden_ruhe():
    folge = knopf.KnopfFolge()
    assert druck(folge, T0, 0.3) == []  # nicht beim Loslassen: erst die Ruhe zeigt, dass es der einzige Druck war
    los = T0 + 0.3
    assert folge.takt(los + 1.99) == []
    assert folge.takt(los + 2.0) == []  # Gleichstand: ein Druck genau jetzt waere noch Teil einer Folge (Pause <= 2,0)
    assert folge.takt(los + 2.01) == ["code"]
    assert folge.takt(los + 30.0) == []  # genau einmal


@given(dauer=st.integers(1, 3000))
@example(dauer=1000)  # KURZ_MAX: auch ein Druck von 1,0 s ist ein Code, wenn nichts folgt
@example(dauer=1500)
@example(dauer=2000)  # CODE_DRUCK_MAX: noch ein Code
@example(dauer=2001)  # darueber: nichts
def test_code_nur_fuer_druecke_bis_zwei_sekunden(dauer):
    folge = knopf.KnopfFolge()
    ereignisse = druck(folge, T0, ms(dauer)) + folge.takt(T0 + ms(dauer) + 10.0)
    assert ereignisse == (["code"] if dauer <= 2000 else [])


def test_druck_ueber_zwei_sekunden_ergibt_nichts():
    for dauer in (2.01, 2.5, 30.0):
        folge = knopf.KnopfFolge()
        ereignisse = druck(folge, T0, dauer)
        assert ereignisse + folge.takt(T0 + dauer + 100.0) == [], dauer


def test_der_code_kommt_auch_ohne_takt_beim_naechsten_druck():
    """`druecken` und `loslassen` werten die verstrichene Zeit selbst: bleibt der Takt aus, geht kein Code verloren."""
    folge = knopf.KnopfFolge()
    druck(folge, T0, 0.3)
    assert folge.druecken(T0 + 5.0) == ["code"]  # der erste Druck war allein; dieser beginnt eine neue Folge
    assert folge.loslassen(T0 + 5.2) == []
    assert folge.takt(T0 + 8.0) == ["code"]  # und ist selbst ein einzelner Druck


def test_zwei_druecke_in_der_pause_sind_kein_code():
    folge = knopf.KnopfFolge()
    ereignisse = spiele(folge, [(T0, 0.3), (T0 + 2.3, 0.3)])  # Pause genau 2,0 s: gleiche Folge
    assert ereignisse + folge.takt(T0 + 100.0) == []


@given(anzahl=st.integers(2, 4), dauer=st.integers(10, 1000), pause=st.integers(0, 2000))
@example(anzahl=2, dauer=1000, pause=2000)
@example(anzahl=4, dauer=1000, pause=2000)
@example(anzahl=3, dauer=10, pause=0)
def test_zwei_bis_vier_druecke_ergeben_nichts(anzahl, dauer, pause):
    folge = knopf.KnopfFolge()
    druecke = [(T0 + i * ms(dauer + pause), ms(dauer)) for i in range(anzahl)]
    assert spiele(folge, druecke) + folge.takt(T0 + 100.0) == []


# --- Folge: fuenf kurze Druecke -------------------------------------------------------------------------------------

def test_fuenf_kurze_bis_zehn_sekunden_oeffnen_das_fenster():
    folge = knopf.KnopfFolge()
    druecke = [(T0 + i * 2.5, 0.5) for i in range(5)]  # Pause je 2,0 s; das fuenfte Druecken genau 10,0 s nach dem ersten
    assert druecke[4][0] - druecke[0][0] == 10.0
    for start, dauer in druecke[:4]:
        assert druck(folge, start, dauer) == []
    assert folge.druecken(druecke[4][0]) == []
    assert folge.loslassen(druecke[4][0] + 0.5) == ["reset_bereit"]  # beim fuenften Loslassen, nicht frueher


def test_fuenftes_druecken_nach_10_1_sekunden_ergibt_nichts():
    for fuenftes in (10.0, 10.1, 11.0):
        folge = knopf.KnopfFolge()
        # Druck 1,0 s; Pausen 2,0 / 2,0 / 1,0 s, damit allein das 10-s-Fenster entscheidet
        druecke = [(T0, 1.0), (T0 + 3.0, 1.0), (T0 + 6.0, 1.0), (T0 + 8.0, 1.0), (T0 + fuenftes, 1.0)]
        ereignisse = spiele(folge, druecke) + folge.takt(T0 + 200.0)
        assert ereignisse == (["reset_bereit", "reset_verfallen"] if fuenftes == 10.0 else []), fuenftes


def test_pause_ueber_zwei_sekunden_beendet_die_folge():
    folge = knopf.KnopfFolge()
    druecke = [(T0 + i * 2.5, 0.5) for i in range(5)]
    assert spiele(folge, druecke) + folge.takt(T0 + 200.0) == ["reset_bereit", "reset_verfallen"]  # Gegenprobe: 2,0 s
    for luecke in (2.01, 2.5, 5.0):
        folge = knopf.KnopfFolge()
        # die Pause vor dem vierten Druck ist zu lang, alle anderen 1,5 s; das fuenfte Druecken liegt bei 10,0 s
        druecke = [(T0, 0.5), (T0 + 2.0, 0.5), (T0 + 4.0, 0.5), (T0 + 4.5 + luecke, 0.5), (T0 + 6.5 + luecke, 0.5)]
        assert druecke[4][0] - druecke[0][0] <= 10.0 or luecke > 3.0
        ereignisse = spiele(folge, druecke) + folge.takt(T0 + 200.0)
        assert ereignisse == [], luecke  # zwei Gruppen aus 3 und 2 Druecken: je ohne Ereignis


@given(index=st.integers(0, 4), dauer=st.integers(1001, 5000))
@example(index=0, dauer=1001)
@example(index=2, dauer=1001)
@example(index=4, dauer=1001)  # das fuenfte Loslassen oeffnet nur bei einem kurzen Druck
def test_druck_ueber_einer_sekunde_bricht_die_folge_ab(index, dauer):
    folge = knopf.KnopfFolge()
    druecke = [(T0 + i * 2.5, 0.5) for i in range(5)]
    start = druecke[index][0]
    druecke[index] = (start, ms(dauer))
    # die folgenden Druecke ruecken nach, damit allein die Dauer des einen Drucks entscheidet (Pause je 1,0 s)
    for i in range(index + 1, 5):
        vorher = druecke[i - 1]
        druecke[i] = (vorher[0] + vorher[1] + 1.0, 0.5)
    assert spiele(folge, druecke) + folge.takt(T0 + 500.0) == []


@given(dauern=st.lists(st.sampled_from([999, 1000, 1001, 400]), min_size=5, max_size=5),
       pausen=st.lists(st.sampled_from([0, 1999, 2000, 2001, 700]), min_size=4, max_size=4))
@example(dauern=[500] * 5, pausen=[2000] * 4)  # 4 * 2,5 s = 10,0 s: genau auf der Kante
@example(dauern=[500] * 5, pausen=[2000, 2000, 2000, 2001])
@example(dauern=[1000] * 5, pausen=[1000] * 4)  # Druecke von genau 1,0 s zaehlen als kurz
@example(dauern=[1000, 1000, 1000, 1000, 1001], pausen=[0] * 4)
def test_die_folge_oeffnet_genau_nach_den_regeln(dauern, pausen):
    """Referenz in Millisekunden: alle Druecke <= 1,0 s, alle Pausen <= 2,0 s, fuenftes Druecken <= 10,0 s nach dem ersten."""
    druecke, jetzt = [], 0
    for i, dauer in enumerate(dauern):
        druecke.append((jetzt, dauer))
        jetzt += dauer + (pausen[i] if i < 4 else 0)
    erwartet = (all(d <= 1000 for _, d in druecke) and all(pausen[i] <= 2000 for i in range(4))
                and druecke[4][0] - druecke[0][0] <= 10000)
    folge = knopf.KnopfFolge()
    ereignisse = spiele(folge, [(T0 + ms(s), ms(d)) for s, d in druecke])
    assert ("reset_bereit" in ereignisse) is erwartet
    assert "reset_bestaetigt" not in ereignisse
    if erwartet:
        assert ereignisse == ["reset_bereit"]  # und kein Code aus einer Folge


def test_folge_erzeugt_keinen_code():
    folge = knopf.KnopfFolge()
    ereignisse = spiele(folge, fuenf(dauer=0.5, abstand=1.0)) + folge.takt(T0 + 500.0)
    assert ereignisse == ["reset_bereit", "reset_verfallen"]


# --- Bestaetigung ---------------------------------------------------------------------------------------------------

@given(halten=st.integers(3000, 4500))
@example(halten=3000)
@example(halten=4500)
@example(halten=3700)
def test_halten_3_0_bis_4_5_im_fenster_bestaetigt(halten):
    folge = knopf.KnopfFolge()
    los = bereit(folge)
    assert folge.druecken(los + 5.0) == []
    assert folge.loslassen(los + 5.0 + ms(halten)) == ["reset_bestaetigt"]  # gewertet beim Loslassen
    assert folge.takt(los + 200.0) == []  # danach ist die Anforderung verbraucht (kein Verfallen, keine zweite)


@given(halten=st.integers(0, 12000))
@example(halten=2900)
@example(halten=2999)
@example(halten=4501)
@example(halten=4600)
def test_halten_2_9_und_4_6_bestaetigt_nicht(halten):
    if 3000 <= halten <= 4500:
        return
    folge = knopf.KnopfFolge()
    los = bereit(folge)
    assert druck(folge, los + 5.0, ms(halten)) == []
    # das Fenster bleibt offen: ein richtiges Halten danach bestaetigt noch
    start = los + 5.0 + ms(halten) + 1.0
    assert druck(folge, start, 3.0) == ["reset_bestaetigt"]


@given(vorlauf=st.integers(0, 4999))
@example(vorlauf=4900)  # die Schwelle aus GP8: Halten ab 4,9 s zaehlt nicht
@example(vorlauf=4999)
@example(vorlauf=0)
def test_halten_ab_4_9_sekunden_nach_dem_fuenften_bestaetigt_nicht(vorlauf):
    folge = knopf.KnopfFolge()
    los = bereit(folge)
    assert druck(folge, los + ms(vorlauf), 3.0) == []  # Beginn vor den 5,0 s: ignoriert, auch wenn es lang genug dauert
    folge = knopf.KnopfFolge()
    los = bereit(folge)
    assert druck(folge, los + 5.0, 3.0) == ["reset_bestaetigt"]  # Gegenprobe: genau 5,0 s


def test_kurzer_druck_im_fenster_wird_ignoriert():
    folge = knopf.KnopfFolge()
    los = bereit(folge)
    for versatz in (0.5, 5.0, 12.0, 30.0):  # kurze Druecke vor und nach der 5-s-Marke
        assert druck(folge, los + versatz, 0.3) == [], versatz
    ereignisse = spiele(folge, [(los + 40.0 + i * 1.0, 0.3) for i in range(5)])  # fuenf kurze: kein zweites Fenster
    assert ereignisse == []
    assert druck(folge, los + 50.0, 3.0) == ["reset_bestaetigt"]  # das Fenster hat alles uebrig gelassen


# --- Fenster ---------------------------------------------------------------------------------------------------------

def test_fenster_verfaellt_nach_sechzig_sekunden():
    folge = knopf.KnopfFolge()
    los = bereit(folge)
    assert folge.takt(los + 59.99) == []
    assert folge.takt(los + 60.0) == []
    assert folge.takt(los + 60.01) == ["reset_verfallen"]
    assert folge.takt(los + 120.0) == []  # einmal
    assert druck(folge, los + 65.0, 3.5) == []  # danach bestaetigt ein Halten nichts mehr


@given(start=st.integers(5000, 70000), halten=st.integers(3000, 4500))
@example(start=56000, halten=4000)  # Loslassen genau 60,0 s nach dem fuenften: noch gueltig
@example(start=56001, halten=4000)  # 60,001 s: verfallen
@example(start=57000, halten=4000)
def test_ein_halten_nach_dem_fensterende_bestaetigt_nicht(start, halten):
    folge = knopf.KnopfFolge()
    los = bereit(folge)
    ereignisse = druck(folge, los + ms(start), ms(halten))
    if start + halten <= 60000:
        assert ereignisse == ["reset_bestaetigt"]
    else:
        assert ereignisse == ["reset_verfallen"]  # das Fensterende wird gemeldet, bestaetigt wird nichts


def test_ein_halten_ueber_das_fensterende_hinaus_meldet_das_verfallen_und_zaehlt_nicht():
    folge = knopf.KnopfFolge()
    los = bereit(folge)
    assert folge.druecken(los + 57.0) == []
    assert folge.takt(los + 60.5) == ["reset_verfallen"]  # auch mit gehaltener Taste
    assert folge.loslassen(los + 61.0) == []  # 4,0 s Halten, aber nach dem Fensterende
    assert folge.takt(los + 300.0) == []  # und kein Code, keine neue Folge aus diesem Druck


def test_das_verfallen_wird_auch_ohne_takt_beim_naechsten_ereignis_gemeldet():
    folge = knopf.KnopfFolge()
    los = bereit(folge)
    assert folge.druecken(los + 70.0) == ["reset_verfallen"]


def test_nach_der_bestaetigung_beginnt_eine_neue_folge():
    folge = knopf.KnopfFolge()
    los = bereit(folge)
    assert druck(folge, los + 5.0, 3.0) == ["reset_bestaetigt"]
    ereignisse = druck(folge, los + 20.0, 0.3) + folge.takt(los + 30.0)
    assert ereignisse == ["code"]  # ein einzelner Druck danach ist wieder ein Code


# --- Unstimmiges vom Geraet ------------------------------------------------------------------------------------------

def test_loslassen_ohne_druecken_und_doppeltes_druecken_werden_ignoriert():
    folge = knopf.KnopfFolge()
    assert folge.loslassen(T0) == []
    assert folge.takt(T0 + 100.0) == []
    assert folge.druecken(T0 + 200.0) == []
    assert folge.druecken(T0 + 200.5) == []  # zweites Druecken ohne Loslassen: das erste gilt weiter
    assert folge.loslassen(T0 + 200.7) == []  # 0,7 s ab dem ersten Druecken
    assert folge.takt(T0 + 203.0) == ["code"]


# --- Geraet finden ---------------------------------------------------------------------------------------------------

GERAETE = """I: Bus=0019 Vendor=0001 Product=0001 Version=0100
N: Name="vc4-hdmi"
P: Phys=vc4-hdmi/input0
S: Sysfs=/devices/platform/soc/vc4-hdmi/input/input1
U: Uniq=
H: Handlers=kbd event1
B: PROP=0
B: EV=3
B: KEY=1 0

I: Bus=0003 Vendor=046d Product=c31c Version=0110
N: Name="Tastatur"
P: Phys=usb-0000:01:00.0-1/input0
S: Sysfs=/devices/platform/usb/input/input2
U: Uniq=
H: Handlers=sysrq kbd leds event2
B: PROP=0
B: EV=120013
B: KEY=1000000000007 ff9f207ac14057ff feaeffdfffefffff fffffffffffffffe
B: MSC=10
B: LED=7

I: Bus=0019 Vendor=0001 Product=0001 Version=0100
N: Name="pwr_button"
P: Phys=
S: Sysfs=/devices/platform/pwr_button/input/input0
U: Uniq=
H: Handlers=kbd event0
B: PROP=0
B: EV=3
B: KEY=10000000000000 0

I: Bus=0019 Vendor=0000 Product=0000 Version=0000
N: Name="ohne Tasten"
H: Handlers=mouse0 event3
B: EV=b
B: ABS=3
"""


def test_finde_knopf_nur_mit_key_power(tmp_path):
    """Nur ein Handler, dessen KEY-Bitmaske Bit 116 (KEY_POWER) traegt: Wort 1 (Bits 64..127), Bit 52 bei 64-Bit-Woertern."""
    datei = tmp_path / "devices"
    datei.write_text(GERAETE)
    assert knopf.finde_knopf(datei, wortbreite=64) == knopf.Path("/dev/input/event0")
    # Tastatur ohne KEY_POWER (Bits 64..127 ohne Bit 52) und Geraete ohne KEY-Zeile zaehlen nicht
    datei.write_text(GERAETE.split("I: Bus=0019 Vendor=0001 Product=0001 Version=0100\nN: Name=\"pwr_button\"")[0])
    assert knopf.finde_knopf(datei, wortbreite=64) is None
    datei.write_text("")
    assert knopf.finde_knopf(datei) is None
    assert knopf.finde_knopf(tmp_path / "fehlt") is None


@pytest.mark.parametrize("wortbreite, key, bit_116", [
    (64, "10000000000000 0", True),  # Wort 1, Bit 52
    (64, "20000000000000 0", False),  # Bit 53 = KEY_SLEEP-Naehe (117), nicht 116
    (64, "8000000000000 0", False),  # Bit 51 (115)
    (64, "10000000000000", False),  # nur ein Wort: Bits 0..63, das Bit liegt dort bei 52 = KEY_... nicht 116
    (32, "100000 0 0 0", True),  # Wort 3 (Bits 96..127), Bit 20
    (32, "200000 0 0 0", False),
    (32, "100000 0 0", False),  # Wort 2: Bit 20 dort = 84
])
def test_bit_116_in_der_key_bitmaske(tmp_path, wortbreite, key, bit_116):
    datei = tmp_path / "devices"
    datei.write_text(f"N: Name=\"x\"\nH: Handlers=kbd event7\nB: KEY={key}\n")
    assert (knopf.finde_knopf(datei, wortbreite=wortbreite) == knopf.Path("/dev/input/event7")) is bit_116


def test_ohne_event_handler_gibt_es_keinen_knopf(tmp_path):
    datei = tmp_path / "devices"
    datei.write_text("N: Name=\"x\"\nH: Handlers=kbd\nB: KEY=10000000000000 0\n")
    assert knopf.finde_knopf(datei, wortbreite=64) is None


def test_von_mehreren_geraeten_mit_key_power_gilt_das_erste(tmp_path):
    datei = tmp_path / "devices"
    datei.write_text("H: Handlers=kbd event4\nB: KEY=10000000000000 0\n\nH: Handlers=kbd event2\nB: KEY=10000000000000 0\n")
    assert knopf.finde_knopf(datei, wortbreite=64) == knopf.Path("/dev/input/event4")


# --- Ereignisse lesen ------------------------------------------------------------------------------------------------

EVENT = struct.Struct("llHHi")


def roh(sekunden: int, mikro: int, typ: int, code: int, wert: int) -> bytes:
    return EVENT.pack(sekunden, mikro, typ, code, wert)


@pytest.fixture
def rohr():
    """(Lese-Ende nicht blockierend, Schreib-Ende) einer Pipe, wie ein evdev-Deskriptor."""
    lesen, schreiben = os.pipe()
    os.set_blocking(lesen, False)
    yield lesen, schreiben
    for fd in (lesen, schreiben):
        with contextlib.suppress(OSError):
            os.close(fd)


def test_ereignisse_aus_rohen_bytes(rohr):
    lesen, schreiben = rohr
    os.write(schreiben, b"".join([
        roh(5, 250000, 1, 116, 1),  # Druecken
        roh(5, 250001, 0, 0, 0),  # EV_SYN: kein Tastenereignis
        roh(5, 300000, 1, 116, 2),  # Wiederholung: verworfen
        roh(5, 400000, 1, 30, 1),  # EV_KEY, aber eine andere Taste (KEY_A)
        roh(5, 400001, 4, 116, 1),  # EV_MSC mit Code 116: kein Tastenereignis
        roh(6, 750000, 1, 116, 0),  # Loslassen
        roh(7, 0, 1, 116, -1),  # unbekannter Wert
    ]))
    assert list(knopf.lies_ereignisse(lesen)) == [(5_250_000, 1), (6_750_000, 0)]
    assert list(knopf.lies_ereignisse(lesen)) == []  # nichts mehr da: Ende der Lieferung, kein Blockieren


def test_ereignisse_ueber_mehrere_lesungen_und_die_grosse_lieferung(rohr):
    lesen, schreiben = rohr
    viele = b"".join(roh(1, i, 1, 116, 1 if i % 2 == 0 else 0) for i in range(200))  # mehr als ein Lesepuffer
    os.write(schreiben, viele)
    ereignisse = list(knopf.lies_ereignisse(lesen))
    assert len(ereignisse) == 200
    assert ereignisse[0] == (1_000_000, 1) and ereignisse[-1] == (1_000_199, 0)


def test_das_ende_des_geraets_ist_ein_laut_gemeldeter_fehler(rohr):
    lesen, schreiben = rohr
    os.write(schreiben, roh(1, 0, 1, 116, 1))
    os.close(schreiben)
    gelesen = []
    with pytest.raises(EOFError):
        for ereignis in knopf.lies_ereignisse(lesen):
            gelesen.append(ereignis)
    assert gelesen == [(1_000_000, 1)]  # was davor kam, wird noch geliefert


def test_das_geraet_wird_exklusiv_und_mit_monotoner_uhr_geoeffnet(tmp_path, monkeypatch):
    """EVIOCGRAB: kein anderer Leser (logind) sieht den Knopf; EVIOCSCLOCKID: Zeitstempel der Ereignisse aus
    CLOCK_MONOTONIC (kein Sprung durch NTP, gleiche Uhr wie `time.monotonic` im Takt). Die Zahlen sind _IOW('E', nr, int)."""
    assert knopf.EVIOCGRAB == 0x40044590 and knopf.EVIOCSCLOCKID == 0x400445A0
    datei = tmp_path / "event0"
    datei.write_bytes(b"")
    aufrufe = []
    monkeypatch.setattr(knopf.fcntl, "ioctl", lambda fd, nummer, arg: aufrufe.append((fd, nummer, arg)))
    fd = knopf.oeffne(datei)
    try:
        assert [(n, a) for _, n, a in aufrufe] == [
            (knopf.EVIOCSCLOCKID, struct.pack("i", knopf.time.CLOCK_MONOTONIC)), (knopf.EVIOCGRAB, 1)]
        assert all(f == fd for f, _, _ in aufrufe)
        assert not os.get_blocking(fd)
    finally:
        os.close(fd)


def test_scheitert_das_belegen_wird_der_deskriptor_geschlossen(tmp_path, monkeypatch):
    datei = tmp_path / "event0"
    datei.write_bytes(b"")
    gesehen = []

    def ioctl_belegt(fd, nummer, arg):
        gesehen.append(fd)
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(knopf.fcntl, "ioctl", ioctl_belegt)
    with pytest.raises(OSError):
        knopf.oeffne(datei)
    with pytest.raises(OSError):
        os.fstat(gesehen[0])  # geschlossen


# --- Ausgaben --------------------------------------------------------------------------------------------------------

def _ausgabe(tmp_path, jetzt=1000.0):
    uhr = [jetzt]
    ausgabe = knopf.Ausgabe(tmp_path / "root", led_datei=tmp_path / "run" / "led_knopf.json", wanduhr=lambda: uhr[0])
    return ausgabe, uhr


def _led(tmp_path):
    return json.loads((tmp_path / "run" / "led_knopf.json").read_text())


def test_seq_wird_fortgesetzt(tmp_path):
    ausgabe, uhr = _ausgabe(tmp_path, 1000.0)
    datei = tmp_path / "root" / "host" / "knopf.json"
    ausgabe.behandeln("code", reset=lambda: pytest.fail("kein Reset"))
    assert json.loads(datei.read_text()) == {"seq": 1, "ts": "1970-01-01T00:16:40+00:00"}
    uhr[0] = 1060.0
    ausgabe.behandeln("code", reset=lambda: pytest.fail("kein Reset"))
    assert json.loads(datei.read_text()) == {"seq": 2, "ts": "1970-01-01T00:17:40+00:00"}
    # ein neuer Dienst (Neustart) setzt aus der Datei fort
    neu = knopf.Ausgabe(tmp_path / "root", led_datei=tmp_path / "run" / "led_knopf.json", wanduhr=lambda: 1100.0)
    neu.behandeln("code", reset=lambda: pytest.fail("kein Reset"))
    assert json.loads(datei.read_text())["seq"] == 3
    assert (datei.stat().st_mode & 0o777) == 0o644  # der Container liest die Datei
    assert [p.name for p in datei.parent.iterdir()] == ["knopf.json"]  # keine Temp-Datei


@pytest.mark.parametrize("inhalt, erwartet", [
    ('{"seq": 41, "ts": "x"}', 42), ('{"seq": 0}', 1), ("kaputt{", 1), ("[]", 1), ('{"seq": "5"}', 1),
    ('{"seq": true}', 1), ('{"seq": 2.5}', 1), ('{"seq": -3}', 1), ('{"ts": "x"}', 1), ("", 1),
], ids=["fortsetzen", "null", "kaputt", "liste", "text", "bool", "kommazahl", "negativ", "ohne-seq", "leer"])
def test_seq_beginnt_bei_unbrauchbarer_datei_neu(tmp_path, inhalt, erwartet, caplog):
    ausgabe, _ = _ausgabe(tmp_path)
    datei = tmp_path / "root" / "host" / "knopf.json"
    datei.parent.mkdir(parents=True)
    datei.write_text(inhalt)
    with caplog.at_level(logging.INFO):
        ausgabe.behandeln("code", reset=lambda: pytest.fail("kein Reset"))
    assert json.loads(datei.read_text())["seq"] == erwartet


def test_ein_code_aendert_die_led_nicht(tmp_path):
    ausgabe, _ = _ausgabe(tmp_path)
    ausgabe.behandeln("code", reset=lambda: pytest.fail("kein Reset"))
    assert not (tmp_path / "run" / "led_knopf.json").exists()  # code_sichtbar kommt aus agent_state.json (Container)


def test_reset_bereit_zeigt_sechzig_sekunden_das_muster_und_verfallen_loescht_es(tmp_path):
    ausgabe, _ = _ausgabe(tmp_path, 1000.0)
    ausgabe.behandeln("reset_bereit", reset=lambda: pytest.fail("kein Reset"))
    assert _led(tmp_path) == {"muster": "reset_bereit", "bis": 1060.0}
    ausgabe.behandeln("reset_verfallen", reset=lambda: pytest.fail("kein Reset"))
    assert not (tmp_path / "run" / "led_knopf.json").exists()
    ausgabe.behandeln("reset_verfallen", reset=lambda: pytest.fail("kein Reset"))  # ohne Datei: kein Fehler


def test_reset_bestaetigt_zeigt_reset_laeuft_und_ruft_den_reset(tmp_path):
    ausgabe, _ = _ausgabe(tmp_path, 1000.0)
    ausgabe.behandeln("reset_bereit", reset=lambda: None)
    gesehen = []

    def reset():
        gesehen.append(_led(tmp_path))  # waehrend des Resets steht das Muster schon

    ausgabe.behandeln("reset_bestaetigt", reset=reset)
    assert gesehen == [{"muster": "reset_laeuft", "bis": 1600.0}]
    assert not (tmp_path / "run" / "led_knopf.json").exists()  # kehrt der Reset zurueck, ist der Hinweis weg


def test_ein_gescheiterter_reset_zeigt_stoerung_fuenf_sekunden_und_beendet_den_dienst_nicht(tmp_path, caplog):
    ausgabe, _ = _ausgabe(tmp_path, 1000.0)

    def reset():
        raise RuntimeError("Reset scheitert")

    with caplog.at_level(logging.ERROR):
        ausgabe.behandeln("reset_bestaetigt", reset=reset)
    assert _led(tmp_path) == {"muster": "stoerung", "bis": 1005.0}
    assert "Reset" in caplog.text and any(r.levelno == logging.ERROR for r in caplog.records)


def test_ein_unbekanntes_ereignis_ist_ein_programmierfehler(tmp_path):
    ausgabe, _ = _ausgabe(tmp_path)
    with pytest.raises(ValueError):
        ausgabe.behandeln("etwas", reset=lambda: None)


def test_ein_fehler_beim_schreiben_des_codes_zeigt_stoerung_und_laeuft_weiter(tmp_path, caplog):
    ausgabe, _ = _ausgabe(tmp_path, 1000.0)
    (tmp_path / "root").write_text("ist eine Datei")  # root/host/ laesst sich nicht anlegen
    with caplog.at_level(logging.ERROR):
        ausgabe.behandeln("code", reset=lambda: pytest.fail("kein Reset"))
    assert _led(tmp_path) == {"muster": "stoerung", "bis": 1005.0}
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# --- Schleife und Einstieg -------------------------------------------------------------------------------------------

def test_die_schleife_liest_ereignisse_und_tackt_auch_ohne_neue(rohr):
    lesen, schreiben = rohr
    os.write(schreiben, roh(100, 0, 1, 116, 1) + roh(100, 300000, 1, 116, 0))
    uhren = iter([100.5, 101.0, 102.31, 103.0])
    gesehen = []
    durchlaeufe = []

    def ende():
        durchlaeufe.append(1)
        return len(durchlaeufe) > 3

    knopf.laufe(lesen, knopf.KnopfFolge(), gesehen.append, uhr=lambda: next(uhren), ende=ende, takt=0.01)
    assert gesehen == ["code"]  # beim dritten Takt: 102,31 > 100,3 + 2,0


def test_die_schleife_endet_laut_wenn_das_geraet_verschwindet(rohr):
    lesen, schreiben = rohr
    os.close(schreiben)
    with pytest.raises(EOFError):
        knopf.laufe(lesen, knopf.KnopfFolge(), lambda e: None, uhr=lambda: 0.0, takt=0.01)


def _main_umgebung(monkeypatch, tmp_path, rohr):
    lesen, schreiben = rohr
    monkeypatch.setenv("SHG_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(knopf, "finde_knopf", lambda: tmp_path / "event0")
    monkeypatch.setattr(knopf, "oeffne", lambda pfad: lesen)
    monkeypatch.setattr(knopf.led, "KNOPF_DATEI", tmp_path / "run" / "led_knopf.json")
    return lesen, schreiben


def test_main_ohne_knopf_endet_mit_meldung(monkeypatch):
    monkeypatch.setattr(knopf, "finde_knopf", lambda: None)
    monkeypatch.setattr(knopf, "oeffne", lambda pfad: pytest.fail("ohne Geraet nichts oeffnen"))
    with pytest.raises(SystemExit) as ende:
        knopf.main(reset=lambda: pytest.fail("kein Reset"))
    assert "Kein Knopf" in str(ende.value.code)


def test_main_schreibt_den_code_und_schliesst_das_geraet(monkeypatch, tmp_path, rohr):
    lesen, schreiben = _main_umgebung(monkeypatch, tmp_path, rohr)
    # ein Druck, viel spaeter ein weiterer: das zweite Druecken meldet den Code des ersten (der Takt kaeme erst nach dem EOF)
    os.write(schreiben, roh(1, 0, 1, 116, 1) + roh(1, 300000, 1, 116, 0) + roh(100, 0, 1, 116, 1))
    os.close(schreiben)
    alt = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        with pytest.raises(EOFError):
            knopf.main(reset=lambda: pytest.fail("kein Reset"))
        behandler = signal.getsignal(signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, alt)
    assert json.loads((tmp_path / "root" / "host" / "knopf.json").read_text())["seq"] == 1
    with pytest.raises(OSError):
        os.fstat(lesen)  # geschlossen
    assert callable(behandler) and behandler is not signal.SIG_DFL
    with pytest.raises(SystemExit) as ende:
        behandler(signal.SIGTERM, None)  # SIGTERM von systemd: sauberes Ende, der Deskriptor wird freigegeben
    assert ende.value.code == 0


def test_main_fuehrt_die_ganze_reset_folge_aus_und_ruft_den_eingespeisten_reset(monkeypatch, tmp_path, rohr):
    """Grosse Zeitstempel und die Kanten selbst (Halten genau 3,0 s, genau 5,0 s nach dem fuenften Loslassen): die
    Umrechnung der Mikrosekunden muss exakt sein, sonst faellt die Bestaetigung durch."""
    lesen, schreiben = _main_umgebung(monkeypatch, tmp_path, rohr)
    basis = 10_000
    ereignisse = []
    for i in range(5):  # fuenf kurze Druecke (0,4 s), Pause je 0,6 s
        ereignisse += [roh(basis + i, 0, 1, 116, 1), roh(basis + i, 400000, 1, 116, 0)]
    ereignisse += [roh(basis + 9, 400000, 1, 116, 1), roh(basis + 12, 400000, 1, 116, 0)]  # 5,0 s danach, 3,0 s halten
    os.write(schreiben, b"".join(ereignisse))
    os.close(schreiben)
    aufrufe = []
    with pytest.raises(EOFError):
        knopf.main(reset=lambda: aufrufe.append(1))
    assert aufrufe == [1]
    assert not (tmp_path / "root" / "host" / "knopf.json").exists()  # kein Code aus einer Folge


def test_die_schleife_wartet_hoechstens_den_takt(monkeypatch, rohr):
    """Der Code kommt 2,0 s nach dem Loslassen plus hoechstens einen Takt."""
    lesen, _ = rohr
    wartezeiten = []
    monkeypatch.setattr(knopf.select, "select", lambda r, w, x, timeout: wartezeiten.append(timeout) or ([], [], []))
    durchlaeufe = []
    knopf.laufe(lesen, knopf.KnopfFolge(), lambda e: None, uhr=lambda: 0.0,
                ende=lambda: durchlaeufe.append(1) or len(durchlaeufe) > 2)
    assert wartezeiten == [knopf.TAKT_SEKUNDEN] * 2
    assert knopf.TAKT_SEKUNDEN <= 0.25


def test_finde_knopf_nimmt_standardmaessig_die_wortbreite_des_systems(tmp_path):
    bits = struct.calcsize("l") * 8
    woerter = [f"{1 << (knopf.KEY_POWER % bits):x}"] + ["0"] * (knopf.KEY_POWER // bits)
    datei = tmp_path / "devices"
    datei.write_text(f"H: Handlers=kbd event6\nB: KEY={' '.join(woerter)}\n")
    assert knopf.finde_knopf(datei) == knopf.Path("/dev/input/event6")


def test_der_einstieg_der_unit_hat_noch_keinen_reset_und_scheitert_laut():
    """Bis Task 9 verdrahtet ist, bestaetigt der Dienst eine Folge mit Stoerung statt still nichts zu tun."""
    with pytest.raises(RuntimeError, match="nicht verdrahtet"):
        knopf._reset_fehlt()
