#!/usr/bin/env python3
"""
Pokémon Stock Checker
Checks Dutch/Belgian webshops for product availability and sends Telegram alerts.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).parent
STORES_FILE = SCRIPT_DIR / "stores.json"
STATE_FILE = SCRIPT_DIR / "state.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Dutch/Belgian out-of-stock signals (case-insensitive)
OUT_OF_STOCK_PATTERNS = [
    r"uitverkocht",
    r"niet (op |meer )?voorraad",
    r"niet beschikbaar",
    r"tijdelijk niet leverbaar",
    r"binnenkort beschikbaar",
    r"out of stock",
    r"sold out",
    r"niet op voorraad",
    r"op dit moment niet",
]

# In-stock signals
IN_STOCK_PATTERNS = [
    r"in winkelwagen",
    r"toevoegen aan winkelwagen",
    r"koop nu",
    r"bestel nu",
    r"add to cart",
    r"op voorraad",
    r"in voorraad",
    r"direct leverbaar",
    r"vandaag besteld",
]


def load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def fetch_page(url: str, timeout: int = 15) -> Optional[str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"  [FOUT] Kon {url} niet ophalen: {e}", file=sys.stderr)
        return None


def check_jsonld_availability(soup: BeautifulSoup) -> Optional[bool]:
    """Check JSON-LD structured data for availability."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                avail = item.get("offers", {})
                if isinstance(avail, list):
                    avail = avail[0] if avail else {}
                availability = avail.get("availability", "")
                if "InStock" in availability or "OnlineOnly" in availability:
                    return True
                if any(s in availability for s in ["OutOfStock", "Discontinued", "PreOrder"]):
                    return False
        except (json.JSONDecodeError, AttributeError):
            continue
    return None


def check_meta_availability(soup: BeautifulSoup) -> Optional[bool]:
    """Check Open Graph / meta tags."""
    for tag in soup.find_all("meta"):
        prop = tag.get("property", "") or tag.get("name", "")
        content = tag.get("content", "").lower()
        if "availability" in prop.lower():
            if "instock" in content or "in stock" in content:
                return True
            if "outofstock" in content or "out of stock" in content:
                return False
    return None


def check_selector(soup: BeautifulSoup, selector: str) -> Optional[bool]:
    """Check if a custom CSS selector element is present (and not disabled)."""
    try:
        elements = soup.select(selector)
        if not elements:
            return False
        el = elements[0]
        if el.get("disabled") or "disabled" in el.get("class", []):
            return False
        return True
    except Exception:
        return None


def text_heuristic(soup: BeautifulSoup) -> Optional[bool]:
    """Scan visible text for Dutch stock indicators."""
    text = soup.get_text(" ", strip=True).lower()

    for pattern in OUT_OF_STOCK_PATTERNS:
        if re.search(pattern, text):
            return False

    for pattern in IN_STOCK_PATTERNS:
        if re.search(pattern, text):
            return True

    return None


def is_in_stock(html: str, selector: Optional[str]) -> Optional[bool]:
    """Return True=in stock, False=out of stock, None=unknown."""
    soup = BeautifulSoup(html, "lxml")

    # 1. Structured data (most reliable)
    result = check_jsonld_availability(soup)
    if result is not None:
        return result

    # 2. Meta tags
    result = check_meta_availability(soup)
    if result is not None:
        return result

    # 3. Custom selector from store config
    if selector:
        result = check_selector(soup, selector)
        if result is not None:
            return result

    # 4. Text heuristic
    return text_heuristic(soup)


def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [SKIP] Telegram niet geconfigureerd.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"  [FOUT] Telegram melding mislukt: {e}", file=sys.stderr)
        return False


def build_notification(store: dict) -> str:
    return (
        f"🟢 <b>OP VOORRAAD!</b>\n\n"
        f"🎮 <b>{store['name']}</b>\n"
        f"🛒 <a href=\"{store['url']}\">{store['url']}</a>\n\n"
        f"⚡ Wees er snel bij!"
    )


def main() -> int:
    stores_data = load_json(STORES_FILE)
    state = load_json(STATE_FILE)
    stores = stores_data.get("stores", [])

    active_stores = [s for s in stores if s.get("active", True)]
    print(f"Checking {len(active_stores)} winkel(s)...")

    state_changed = False

    for store in active_stores:
        store_id = store["id"]
        name = store["name"]
        url = store["url"]
        selector = store.get("selector")

        print(f"\n→ {name}")
        html = fetch_page(url)
        if html is None:
            continue

        status = is_in_stock(html, selector)

        if status is None:
            print("  [?] Status onbekend")
            continue

        prev_status = state.get(store_id, {}).get("in_stock")
        state.setdefault(store_id, {})["in_stock"] = status
        state[store_id]["last_checked"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        if status:
            print("  [✓] OP VOORRAAD")
            if prev_status is not True:
                state_changed = True
                msg = build_notification(store)
                sent = send_telegram(msg)
                if sent:
                    print("  [✓] Telegram melding verstuurd")
        else:
            print("  [✗] Niet op voorraad")
            if prev_status is not False:
                state_changed = True

        time.sleep(2)

    if state_changed:
        save_json(STATE_FILE, state)
        print("\nState opgeslagen.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
