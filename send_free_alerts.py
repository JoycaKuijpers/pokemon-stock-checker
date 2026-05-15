#!/usr/bin/env python3
"""
send_free_alerts.py

Draait elke 30 minuten via GitHub Actions.
Checkt pending_free.json op meldingen die 6+ uur oud zijn
en stuurt ze naar het gratis Telegram-kanaal.

GitHub Secrets nodig:
  TELEGRAM_BOT_TOKEN       — zelfde bot token als check_stock.py
  TELEGRAM_CHANNEL_FREE    — chat ID van het gratis publieke kanaal
"""

import json
import os
import sys
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PENDING_FILE = next(
    (p for p in [SCRIPT_DIR / "pending_free.json", SCRIPT_DIR.parent / "pending_free.json"] if p.exists()),
    SCRIPT_DIR / "pending_free.json",
)

TELEGRAM_BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_FREE = os.environ.get("TELEGRAM_CHANNEL_FREE", "")

FREE_DELAY_HOURS = 6


def load_pending() -> list[dict]:
    if PENDING_FILE.exists():
        return json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    return []


def save_pending(pending: list[dict]) -> None:
    PENDING_FILE.write_text(json.dumps(pending, indent=2, ensure_ascii=False), encoding="utf-8")


def send_telegram(message: str, chat_id: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        print("  [SKIP] Telegram niet geconfigureerd.", flush=True)
        return False
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            api_url,
            json={"chat_id": chat_id, "text": message,
                  "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"  [FOUT] Telegram mislukt: {e}", file=sys.stderr)
        return False


def build_notification(name: str, url: str) -> str:
    return (
        f"🟢 <b>OP VOORRAAD!</b>\n\n"
        f"📦 <b>{name}</b>\n"
        f"🛒 <a href=\"{url}\">{url}</a>\n\n"
        f"⚡ Wees er snel bij!\n\n"
        f"<i>💡 Upgrade naar Card Radar betaald voor meldingen binnen 5 minuten.</i>"
    )


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
            msg = build_notification(item["name"], item["url"])
            sent = send_telegram(msg, TELEGRAM_CHANNEL_FREE)
            if sent:
                print(f"  [✓] Gratis melding verstuurd: {item['name']}", flush=True)
                sent_count += 1
            else:
                # Bij fout toch in wachtrij houden voor volgende run
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
