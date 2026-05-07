#!/usr/bin/env python3
"""
Eenmalig script om encoding-rommel uit stores.json te halen.
Vervangt ook alle varianten van Pokémon door Pokemon.
"""
import json
import re
from pathlib import Path

STORES_FILE = next(
    (p for p in [Path("stores.json"), Path("checker/stores.json")] if p.exists()),
    Path("stores.json"),
)


def fix_string(s: str) -> str:
    # Herstel dubbel-geëncoded UTF-8 (Ã© → é → e, etc.)
    try:
        s = s.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    # Alles naar Pokemon zonder accent
    s = re.sub(r"Pok[eéèêëÃ][^\s]{0,3}mon", "Pokemon", s)
    s = re.sub(r"pok[eéèêëÃ][^\s]{0,3}mon", "pokemon", s)
    return s


data = json.loads(STORES_FILE.read_text(encoding="utf-8"))

for store in data.get("stores", []):
    store["name"] = fix_string(store["name"])

STORES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8")
print(f"Klaar. {len(data['stores'])} stores gecleaned.")
