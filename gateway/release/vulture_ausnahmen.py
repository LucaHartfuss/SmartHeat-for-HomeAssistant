# Ausnahmen fuer vulture (scripts/check.sh, Schritt lint, K4), Whitelist-Format von vulture.
# vulture zaehlt jeden Namen hier als benutzt. Jede Zeile nennt den Grund; keine Zeile ohne Grund.
_.ignore_aliases  # build_bundle.py _FlatDumper: PyYAML ruft die Methode des Dumpers ueber ihren Namen
_.size  # build_bundle.py write_named_archive: tarfile liest TarInfo.size beim Schreiben des Eintrags (addfile)
