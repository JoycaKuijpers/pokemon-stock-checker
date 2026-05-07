#!/usr/bin/env python3
"""
Bouwt kapotte namen opnieuw op vanuit de URL slug.
bv. /products/pokemon-elite-trainer-box-silver-tempest
  → "Pokemon Elite Trainer Box Silver Tempest"
"""
import json
import re
from pathlib import Path
from urllib.parse import urlparse

STORES_FILE = next(
    (p for p in [Path("stores.json"), Path("checker/stores.json")] if p.exists()),
    Path("stores.json"),
)

BROKEN = re.compile(r"[ÃÂÀÁÂÃÄÅÆ]{2,}")  # twee of meer rommelchars achter elkaar


def name_from_url(url: str) -> str:
    path = urlparse(url).path          # /products/pokemon-elite-trainer-box-silver-tempest
    slug = path.rstrip("/").split("/")[-1]  # pokemon-elite-trainer-box-silver-tempest
    return slug.replace("-", " ").title()   # Pokemon Elite Trainer Box Silver Tempest


data = json.loads(STORES_FILE.read_text(encoding="utf-8"))
fixed = 0

for store in data.get("stores", []):
    name = store["name"]
    if BROKEN.search(name):
        store["name"] = name_from_url(store["url"])
        fixed += 1

STORES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"Klaar. {fixed} namen hersteld uit URL.")
