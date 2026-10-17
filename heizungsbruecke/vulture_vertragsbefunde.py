# Offene Befunde fuer vulture, KEINE Ausnahmen (Stufe 3 S8b, A5-87; Rest von A5-85): Vertragskonstanten, die im
# Produktcode niemand liest. Leser sind nur tools/contract_check.py (Dev-Root) und die Pin-Tests des Add-ons. Sie werden
# nicht geloescht (Vertrag) und stehen hier nur, damit der Lint-Schritt gruen ist, bis entschieden ist, ob der Code sie
# verwendet oder der Vertrag sie streicht. Neue Eintraege nur mit Entscheidung; jede Zeile nennt den Leser.

# --- smartheat_device/wire.py: woertliche Kopie aus dem Server (device_protocol.py); welche Namen das Geraet nicht
#     liest, haelt tests/test_device_wire.py fest (NUR_SERVER, NUR_GEPINNT_GERAET) ---
_.MAX_CLOCK_SKEW_SECONDS  # NUR_SERVER; contract_check (Geraetevertrag V1)
_.MAX_BODY_BYTES  # NUR_SERVER; contract_check (V1)
_.UNAUTHENTICATED_ERROR  # NUR_SERVER; contract_check (V1)
_.ENERGIE_EINHEITEN  # NUR_GEPINNT_GERAET (bindungsparameter.EINHEITEN, test_core_vertrag_pin); contract_check (V1)
_.expires_rule  # Feld von wire.Command; nur der Server wertet die Regel aus; contract_check (V1)

# --- HA-Host: Vertraege mit der Integration ---
_.DIENST  # heizungsbruecke/discovery.py; contract_check (Discovery, config.yaml discovery)
_.FIELDS  # heizungsbruecke/discovery.py; contract_check (Discovery-Felder)
_.HINWEIS_FIELDS  # heizungsbruecke/geraet_status.py; contract_check (Hinweis-Felder), test_geraet_status

# --- Kern und Laufzeit ---
_.GRUENDE  # smartheat_core/config_check.py; contract_check (Teilmenge von DOKUMENT_ERRORS), test_core_config_check
_.HINT_CATEGORIES  # smartheat_runtime/notifier.py; Pin wire.HA_HINWEISE (test_core_vertrag_pin), test_notifier;
#                    Loeschkandidat: Duplikat von config_check.HA_HINWEISE, nur Pin-Test
_.STATUS_VALUES  # smartheat_runtime/status.py; contract_check (Integration, Portal-Statustexte)
_.BOOST_VALUES  # smartheat_runtime/status.py; contract_check (Integration)
_.ABO_VALUES  # smartheat_runtime/status.py; contract_check (Integration)
_.HINT_FIELDS  # smartheat_runtime/status.py; contract_check (Integration)
_.DELIVERY_ROLES  # smartheat_runtime/telemetry.py; contract_check (Server TELEMETRY_DELIVERY_FIELDS), test_telemetry
_.REGULATION_FIELDS  # smartheat_runtime/telemetry.py; contract_check (Server TELEMETRY_REGULATION_FIELDS)
