# Ausnahmen fuer vulture (scripts/check.sh, Schritt lint; Stufe 3 S8b, A5-87, K4), Whitelist-Format von vulture.
# vulture zaehlt jeden Namen hier als benutzt. Jede Zeile nennt den Grund; keine Zeile ohne Grund.
_.ignore_aliases  # build_bundle.py _FlatDumper: PyYAML ruft die Methode des Dumpers ueber ihren Namen
