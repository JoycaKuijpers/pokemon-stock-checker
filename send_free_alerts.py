#!/usr/bin/env python3
"""
send_free_alerts.py

Draait elke 30 minuten via GitHub Actions.
Checkt pending_free.json op meldingen die 6+ uur oud zijn
en post ze naar het publieke Whop forum (gratis zichtbaar voor iedereen).

GitHub Secrets nodig:
  WHOP_API_KEY     — Company API key van Whop
  WHOP_CHANNEL_ID  — Experience ID van het publieke forum (exp_WDHmktRAikmxPN)
"""

import json
import os
import sys
import requests
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PENDING_FILE = next(
    (p for p in [SCRIPT_DIR / "pending_free.json", SCRIPT_DIR.parent / "pending_free.json"] if p.exists()),
    SCRIPT_DIR / "pending_free.json",
)

WHOP_API_KEY    = os.environ.get("WHOP_API_KEY", "")
WHOP_CHANNEL_ID = os.environ.get("WHOP_CHANNEL_ID", "")
WHOP_API_URL    = "https://api.whop.com/api/v5/messages"

FREE_DELAY_HOURS = 6


def load_pending() -> list[dict]:
    if PENDING_FILE.exists():
        return json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    return []


def save_pending(pending: list[dict]) -> None:
    PENDING_FILE.write_text(json.dumps(pending, indent=2, ensure_ascii=False), encoding="utf-8")


def post_to_whop(name: str, url: str) -> bool:
    if not WHOP_API_KEY or not WHOP_CHANNEL_ID:
        print("  [SKIP] Whop niet geconfigureerd.", flush=True)
        return False

    content = (
        f"🟢 **OP VOORRAAD!**\n\n"
        f"📦 **{name}**\n"
        f"🛒 {url}\n\n"
        f"⚡ Wees er snel bij!\n\n"
        f"💡 *Wil je meldingen binnen 5 minuten? Upgrade naar Card Radar.*"
    )

    headers = {
        "Authorization": f"Bearer {WHOP_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "channel_id": WHOP_CHANNEL_ID,
        "content": content,
    }

    try:
        resp = requests.post(WHOP_API_URL, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        return True
    except requests.exceptions.HTTPError as e:
        print(f"  [FOUT] Whop HTTP fout: {e} — {resp.text}", file=sys.stderr)
        return False
    except requests.exceptions.RequestException as e:
        print(f"  [FOUT] Whop verbindingsfout: {e}", file=sys.stderr)
        return False


def main() -> int:
    print("Free alerts checker gestart.", flush=True)

    pending = load_pending()
    if not pending:
        print("Geen berichten in wachtrij.", flush=True)
        return 0

    print(f"{len(pending)} bericht(en) in wachtrij.", flush=True)

    now = datetime.now(timezone.utc)
    still_pending = []
    sent_count = 0

    for item in pending:
        queued_at = datetime.fromisoformat(item["queued_at"])
        age = now - queued_at
        hours_old = age.total_seconds() / 3600

        if hours_old >= FREE_DELAY_HOURS:
            sent = post_to_whop(item["name"], item["url"])
            if sent:
                print(f"  [✓] Whop forum post verstuurd: {item['name']}", flush=True)
                sent_count += 1
            else:
                still_pending.append(item)
        else:
            remaining = FREE_DELAY_HOURS - hours_old
            print(f"  [⏳] Nog {remaining:.1f} uur wachten: {item['name']}", flush=True)
            still_pending.append(item)

    save_pending(still_pending)
    print(f"\n{sent_count} verstuurd, {len(still_pending)} nog in wachtrij.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
