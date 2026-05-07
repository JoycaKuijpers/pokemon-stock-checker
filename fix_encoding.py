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

def name_from_url(url: str) -> str:
    path = urlparse(url).path
    slug = path.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").title()


data = json.loads(STORES_FILE.read_text(encoding="utf-8"))
fixed = 0

for store in data.get("stores", []):
    name = store["name"]
    if len(name) > 150:  # kapotte naam door encoding — normale naam is nooit zo lang
        store["name"] = name_from_url(store["url"])
        fixed += 1

STORES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"Klaar. {fixed} namen hersteld uit URL.")
