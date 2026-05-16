#!/usr/bin/env python3
"""
Pokémon Stock Checker — Card Radar versie

Wijzigingen t.o.v. origineel:
1. TCG-filter — alleen Pokémon TCG-producten worden gemeld
2. Twee Telegram-ontvangers:
   - TELEGRAM_CHAT_ID     → jouw privé chat (direct)
   - TELEGRAM_CHANNEL_ID  → Card Radar kanaal (5 minuten later)

Extra GitHub Secret nodig:
  TELEGRAM_CHANNEL_ID  → chat ID van je Card Radar kanaal (begint met -100...)
"""

import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
import requests

from bs4 import BeautifulSoup
import cloudscraper

SCRIPT_DIR = Path(__file__).parent
STORES_FILE = next(
    (p for p in [SCRIPT_DIR / "stores.json", SCRIPT_DIR.parent / "stores.json"] if p.exists()),
    SCRIPT_DIR / "stores.json",
)
STATE_FILE = STORES_FILE.parent / "state.json"

TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")

CHANNEL_DELAY_SECONDS = 300
NOTIFY_COOLDOWN_HOURS = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

OUT_OF_STOCK_PATTERNS = [
    r"uitverkocht", r"niet (op |meer )?voorraad", r"niet beschikbaar",
    r"tijdelijk niet leverbaar", r"binnenkort beschikbaar",
    r"out of stock", r"sold out", r"op dit moment niet",
]

IN_STOCK_PATTERNS = [
    r"in winkelwagen", r"toevoegen aan winkelwagen", r"koop nu",
    r"bestel nu", r"add to cart", r"op voorraad", r"in voorraad",
    r"direct leverbaar", r"vandaag besteld",
]

TCG_KEYWORDS = [
    "booster", "etb", "elite trainer", "trainer box",
    "collection box", "collection", "tin", "blister",
    "pakje", "pack", "kaart", "card", "tcg",
    "ex box", "v box", "vmax", "bundle",
    "scarlet", "violet", "obsidian", "temporal", "stellar",
    "prismatic", "surging", "twilight", "shrouded", "151",
    "destined rivals", "journey together",
]

TCG_EXCLUDE_KEYWORDS = [
    "knuffel", "plush", "figuur", "figure", "poster", "pin",
    "mok", "mug", "tas", "bag", "shirt", "kleding", "sokken",
    "lunchbox", "rugzak", "backpack", "telefoonhoesje",
    "videogame", "nintendo switch", "3ds", "amiibo",
    "sticker", "agenda", "kalender",
]


def is_tcg_product(name: str) -> bool:
    name_lower = name.lower()
    for excl in TCG_EXCLUDE_KEYWORDS:
        if excl in name_lower:
            return False
    for kw in TCG_KEYWORDS:
        if kw in name_lower:
            return True
    return False


def load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def normalize_name(name: str) -> str:
    return re.sub(r"Pok[eé]mon", "Pokemon", name, flags=re.I)


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def cooldown_passed(last_notified: Optional[str]) -> bool:
    if not last_notified:
        return True
    elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last_notified)
    return elapsed > timedelta(hours=NOTIFY_COOLDOWN_HOURS)




def fetch_page(url: str) -> Optional[str]:
    # Eerste poging met gewone requests
    try:
        resp = requests.get(url, headers=HEADERS, timeout=(5, 10), allow_redirects=True)
        if resp.status_code == 403:
            raise requests.HTTPError("403")
        resp.raise_for_status()
        return resp.text
    except (requests.HTTPError, requests.RequestException):
        pass

    # Tweede poging met cloudscraper bij 403 of andere fout
    try:
        print(f"  [retry] Cloudscraper poging voor {url}", flush=True)
        scraper = cloudscraper.create_scraper()
        resp = scraper.get(url, headers=HEADERS, timeout=(5, 15))
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"  [FOUT] Cloudscraper ook mislukt voor {url}: {e}", file=sys.stderr)
        return None

def process_product(prod_state: dict, key: str, name: str, url: str, available: bool,
                    notify_on_new: bool = False) -> Optional[dict]:
    entry = prod_state.setdefault(key, {})
    prev_status = entry.get("in_stock")
    entry.update({"name": name, "url": url, "in_stock": available, "last_checked": now_utc()})
    if available:
        is_new = prev_status is None
        came_back = prev_status is False
        if (came_back or (is_new and notify_on_new)) and cooldown_passed(entry.get("last_notified")):
            entry["last_notified"] = datetime.now(timezone.utc).isoformat()
            return {"name": name, "url": url}
    else:
        entry.pop("last_notified", None)
    return None


def shopify_handle(url: str) -> Optional[str]:
    m = re.search(r"/products/([^/?#]+)", url)
    return m.group(1) if m else None


def fetch_shopify_collection(domain: str, collection_handle: str) -> list[dict]:
    products = []
    page = 1
    while True:
        try:
            resp = requests.get(
                f"https://{domain}/collections/{collection_handle}/products.json",
                params={"limit": 250, "page": page},
                headers=HEADERS,
                timeout=(5, 15),
            )
            if not resp.ok:
                break
            batch = resp.json().get("products", [])
            if not batch:
                break
            products.extend(batch)
            if len(batch) < 250:
                break
            page += 1
            time.sleep(0.3)
        except requests.RequestException:
            break
    return products


def fetch_shopify_bulk(domain: str, handles: set) -> dict[str, bool]:
    result = {}
    page = 1
    remaining = set(handles)
    while remaining:
        try:
            resp = requests.get(
                f"https://{domain}/products.json",
                params={"limit": 250, "page": page},
                headers=HEADERS,
                timeout=(5, 15),
            )
            if not resp.ok:
                break
            products = resp.json().get("products", [])
            if not products:
                break
            for p in products:
                h = p.get("handle", "")
                if h in remaining:
                    result[h] = any(v.get("available", False) for v in p.get("variants", []))
                    remaining.discard(h)
            if len(products) < 250:
                break
            page += 1
            time.sleep(0.5)
        except requests.RequestException:
            break
    return result


def check_jsonld_availability(soup: BeautifulSoup) -> Optional[bool]:
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
    text = soup.get_text(" ", strip=True).lower()
    for pattern in OUT_OF_STOCK_PATTERNS:
        if re.search(pattern, text):
            return False
    for pattern in IN_STOCK_PATTERNS:
        if re.search(pattern, text):
            return True
    return None


def is_in_stock_html(url: str, html: str, selector: Optional[str]) -> Optional[bool]:
    soup = BeautifulSoup(html, "lxml")
    for check in (check_jsonld_availability, check_meta_availability):
        result = check(soup)
        if result is not None:
            return result
    if selector:
        result = check_selector(soup, selector)
        if result is not None:
            return result
    return text_heuristic(soup)


def check_shopify_category(store: dict, state: dict) -> tuple[list[dict], bool]:
    url = store["url"]
    m = re.search(r"/collections/([^/?#]+)", url)
    if not m:
        print("  [!] Geen collection handle gevonden in URL", flush=True)
        return [], False
    domain = urlparse(url).netloc
    handle = m.group(1)
    products = fetch_shopify_collection(domain, handle)
    if not products:
        print("  [!] Geen producten gevonden in collectie", flush=True)
        return [], False
    print(f"  → {len(products)} producten in collectie", flush=True)
    cat_state = state.setdefault(store["id"], {"products": {}})
    prod_state = cat_state.setdefault("products", {})
    cat_state["last_checked"] = now_utc()
    notify_on_new = store.get("notify_on_new", False)
    notifications = []
    for p in products:
        available = any(v.get("available", False) for v in p.get("variants", []))
        name = normalize_name(p["title"])
        prod_url = f"https://{domain}/products/{p['handle']}"
        notif = process_product(prod_state, p["handle"], name, prod_url, available, notify_on_new)
        if notif:
            notifications.append(notif)
    in_stock = sum(1 for e in prod_state.values() if e.get("in_stock"))
    print(f"  → {in_stock}/{len(products)} op voorraad, {len(notifications)} nieuw op voorraad", flush=True)
    return notifications, True


def get_woocommerce_max_page(soup: BeautifulSoup) -> int:
    max_page = 1
    for a in soup.select("a.page-numbers, .woocommerce-pagination a"):
        try:
            n = int(a.get_text(strip=True))
            if n > max_page:
                max_page = n
        except ValueError:
            continue
    return max_page


def parse_woocommerce_cards(soup: BeautifulSoup) -> list[dict]:
    results = []
    for card in soup.select("li.product, div.product"):
        classes = card.get("class", [])
        if "instock" in classes:
            available = True
        elif "outofstock" in classes:
            available = False
        else:
            continue
        link = card.find("a", href=True)
        if not link:
            continue
        prod_url = link.get("href", "")
        if not prod_url:
            continue
        name_el = card.select_one(".woocommerce-loop-product__title, h2, h3")
        name = name_el.get_text(strip=True) if name_el else (
            link.get("title") or link.get_text(strip=True)
        )
        name = normalize_name(name[:120])
        if not name:
            continue
        results.append({"name": name, "url": prod_url, "available": available})
    return results


def parse_jouwweb_cards(soup: BeautifulSoup) -> list[dict]:
    """JouwWeb webshops — producten in .product-list of .products-grid."""
    results = []
    for card in soup.select(".product-list__item, .product-item, article.product"):
        # Beschikbaarheid via class of data-attribuut
        classes = " ".join(card.get("class", []))
        if "out-of-stock" in classes or "uitverkocht" in classes:
            available = False
        elif "in-stock" in classes or "available" in classes:
            available = True
        else:
            # Kijk naar tekst in de kaart
            text = card.get_text(" ", strip=True).lower()
            if any(re.search(p, text) for p in OUT_OF_STOCK_PATTERNS):
                available = False
            elif any(re.search(p, text) for p in IN_STOCK_PATTERNS):
                available = True
            else:
                continue

        link = card.find("a", href=True)
        if not link:
            continue
        prod_url = link.get("href", "")
        if not prod_url.startswith("http"):
            continue
        name_el = card.select_one("h2, h3, .product-title, .product-name")
        name = name_el.get_text(strip=True) if name_el else link.get_text(strip=True)
        name = normalize_name(name[:120])
        if not name:
            continue
        results.append({"name": name, "url": prod_url, "available": available})
    return results


def parse_shopware_cards(soup: BeautifulSoup) -> list[dict]:
    """Shopware webshops — producten in .product-box of .cms-element-product-listing."""
    results = []
    for card in soup.select(".product-box, .product-card, article[class*='product']"):
        text = card.get_text(" ", strip=True).lower()
        if any(re.search(p, text) for p in OUT_OF_STOCK_PATTERNS):
            available = False
        elif any(re.search(p, text) for p in IN_STOCK_PATTERNS):
            available = True
        else:
            # Kijk naar uitverkocht badge
            badge = card.select_one(".badge, .product-badge, .is-soldout")
            if badge and "uitverkocht" in badge.get_text(strip=True).lower():
                available = False
            else:
                available = True  # default: op voorraad als geen signaal

        link = card.select_one("a.product-image-link, a[href*='/detail/'], a.product-name")
        if not link:
            link = card.find("a", href=True)
        if not link:
            continue
        prod_url = link.get("href", "")
        if not prod_url.startswith("http"):
            continue
        name_el = card.select_one(".product-name, h2, h3")
        name = name_el.get_text(strip=True) if name_el else link.get("title", "") or link.get_text(strip=True)
        name = normalize_name(name[:120])
        if not name:
            continue
        results.append({"name": name, "url": prod_url, "available": available})
    return results


def parse_magento_cards(soup: BeautifulSoup) -> list[dict]:
    """Magento webshops — producten in .product-items of .products-grid."""
    results = []
    for card in soup.select(".product-item, .item.product"):
        classes = " ".join(card.get("class", []))
        if "out-of-stock" in classes:
            available = False
        else:
            stock_el = card.select_one(".stock, .availability")
            if stock_el:
                stock_text = stock_el.get_text(strip=True).lower()
                if any(re.search(p, stock_text) for p in OUT_OF_STOCK_PATTERNS):
                    available = False
                else:
                    available = True
            else:
                text = card.get_text(" ", strip=True).lower()
                if any(re.search(p, text) for p in OUT_OF_STOCK_PATTERNS):
                    available = False
                elif any(re.search(p, text) for p in IN_STOCK_PATTERNS):
                    available = True
                else:
                    continue

        link = card.select_one("a.product-item-link, a.product-item-photo")
        if not link:
            link = card.find("a", href=True)
        if not link:
            continue
        prod_url = link.get("href", "")
        if not prod_url.startswith("http"):
            continue
        name_el = card.select_one(".product-item-name, strong.product-item-name, .product-name")
        name = name_el.get_text(strip=True) if name_el else link.get("title", "") or link.get_text(strip=True)
        name = normalize_name(name[:120])
        if not name:
            continue
        results.append({"name": name, "url": prod_url, "available": available})
    return results


def get_magento_max_page(soup: BeautifulSoup) -> int:
    max_page = 1
    for a in soup.select(".pages a, .pagination a"):
        try:
            n = int(a.get_text(strip=True))
            if n > max_page:
                max_page = n
        except ValueError:
            continue
    return max_page


def check_woocommerce_category(store: dict, state: dict, soup: BeautifulSoup) -> tuple[list[dict], bool]:
    base_url = store["url"].rstrip("/")
    max_page = get_woocommerce_max_page(soup)
    all_products = parse_woocommerce_cards(soup)
    for page in range(2, max_page + 1):
        page_url = f"{base_url}/page/{page}/"
        html = fetch_page(page_url)
        if not html:
            break
        all_products.extend(parse_woocommerce_cards(BeautifulSoup(html, "lxml")))
        time.sleep(0.5)
    if not all_products:
        print("  [!] Geen WooCommerce product cards gevonden", flush=True)
        return [], False
    pages_str = f" ({max_page} pagina's)" if max_page > 1 else ""
    print(f"  → {len(all_products)} producten gevonden{pages_str}", flush=True)
    cat_state = state.setdefault(store["id"], {"products": {}})
    prod_state = cat_state.setdefault("products", {})
    cat_state["last_checked"] = now_utc()
    notify_on_new = store.get("notify_on_new", False)
    notifications = []
    for p in all_products:
        key = urlparse(p["url"]).path.strip("/").replace("/", "-")
        notif = process_product(prod_state, key, p["name"], p["url"], p["available"], notify_on_new)
        if notif:
            notifications.append(notif)
    in_stock = sum(1 for e in prod_state.values() if e.get("in_stock"))
    print(f"  → {in_stock}/{len(all_products)} op voorraad, {len(notifications)} nieuw op voorraad", flush=True)
    return notifications, True


def check_generic_category(store: dict, state: dict, soup: BeautifulSoup,
                            parse_fn, platform_name: str) -> tuple[list[dict], bool]:
    """Generieke category checker voor JouwWeb, Shopware, Magento etc."""
    base_url = store["url"].rstrip("/")
    all_products = parse_fn(soup)

    # Paginering proberen
    max_page = get_magento_max_page(soup)
    for page in range(2, max_page + 1):
        page_url = f"{base_url}?p={page}"
        html = fetch_page(page_url)
        if not html:
            break
        all_products.extend(parse_fn(BeautifulSoup(html, "lxml")))
        time.sleep(0.5)

    if not all_products:
        print(f"  [!] Geen producten gevonden ({platform_name})", flush=True)
        return [], False

    print(f"  → {len(all_products)} producten gevonden ({platform_name})", flush=True)
    cat_state = state.setdefault(store["id"], {"products": {}})
    prod_state = cat_state.setdefault("products", {})
    cat_state["last_checked"] = now_utc()
    notify_on_new = store.get("notify_on_new", False)
    notifications = []
    for p in all_products:
        key = urlparse(p["url"]).path.strip("/").replace("/", "-")
        notif = process_product(prod_state, key, p["name"], p["url"], p["available"], notify_on_new)
        if notif:
            notifications.append(notif)
    in_stock = sum(1 for e in prod_state.values() if e.get("in_stock"))
    print(f"  → {in_stock}/{len(all_products)} op voorraad, {len(notifications)} nieuw op voorraad", flush=True)
    return notifications, True


def check_category(store: dict, state: dict) -> tuple[list[dict], bool]:
    url = store["url"]
    if re.search(r"/collections/[^/?#]+", url):
        return check_shopify_category(store, state)
    html = fetch_page(url)
    if not html:
        return [], False
    soup = BeautifulSoup(html, "lxml")

    # WooCommerce
    if soup.select("li.product, div.product"):
        return check_woocommerce_category(store, state, soup)

    # Magento
    if soup.select(".product-item, .item.product, .products-grid"):
        cards = parse_magento_cards(soup)
        if cards:
            return check_generic_category(store, state, soup, parse_magento_cards, "Magento")

    # Shopware
    if soup.select(".product-box, .product-card, .cms-element-product-listing"):
        cards = parse_shopware_cards(soup)
        if cards:
            return check_generic_category(store, state, soup, parse_shopware_cards, "Shopware")

    # JouwWeb
    if soup.select(".product-list__item, .product-item, article.product"):
        cards = parse_jouwweb_cards(soup)
        if cards:
            return check_generic_category(store, state, soup, parse_jouwweb_cards, "JouwWeb")

    print("  [!] Platform niet herkend voor categorie-modus", flush=True)
    return [], False


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
        print(f"  [FOUT] Telegram melding mislukt naar {chat_id}: {e}", file=sys.stderr)
        return False


def build_notification(name: str, url: str) -> str:
    return (
        f"🟢 <b>OP VOORRAAD!</b>\n\n"
        f"📦 <b>{name}</b>\n"
        f"🛒 <a href=\"{url}\">{url}</a>\n\n"
        f"⚡ Wees er snel bij!"
    )


def queue_free_alerts(notifications: list[dict]) -> None:
    """Voeg meldingen toe aan pending_free.json voor het gratis kanaal."""
    pending_file = STATE_FILE.parent / "pending_free.json"
    existing = []
    if pending_file.exists():
        try:
            existing = json.loads(pending_file.read_text(encoding="utf-8"))
        except Exception:
            existing = []

    now = datetime.now(timezone.utc).isoformat()
    existing_urls = {item["url"] for item in existing}

    added = 0
    for notif in notifications:
        if notif["url"] not in existing_urls:
            existing.append({
                "name": notif["name"],
                "url": notif["url"],
                "queued_at": now,
            })
            added += 1

    pending_file.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    if added:
        print(f"  [✓] {added} melding(en) in wachtrij voor gratis kanaal (6 uur)", flush=True)


def queue_paid_alerts(notifications: list[dict]) -> None:
    """Voeg meldingen toe aan pending_paid.json voor het betaalde kanaal (5 min vertraging)."""
    pending_file = STATE_FILE.parent / "pending_paid.json"
    existing = []
    if pending_file.exists():
        try:
            existing = json.loads(pending_file.read_text(encoding="utf-8"))
        except Exception:
            existing = []

    now = datetime.now(timezone.utc).isoformat()
    existing_urls = {item["url"] for item in existing}

    added = 0
    for notif in notifications:
        if notif["url"] not in existing_urls:
            existing.append({
                "name": notif["name"],
                "url": notif["url"],
                "queued_at": now,
            })
            added += 1

    pending_file.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    if added:
        print(f"  [✓] {added} melding(en) in wachtrij voor betaald kanaal (5 min)", flush=True)


def notify_with_delay(notifications: list[dict]) -> None:
    """Stuur direct naar privé chat, wachtrij voor betaald en gratis kanaal."""
    if not notifications:
        return
    # Stap 1 — direct naar jou
    for notif in notifications:
        msg = build_notification(notif["name"], notif["url"])
        sent = send_telegram(msg, TELEGRAM_CHAT_ID)
        if sent:
            print(f"  [✓] Privé melding verstuurd: {notif['name']}", flush=True)
    # Stap 2 — wachtrij voor betaald kanaal (5 min via send_paid_alerts.py)
    queue_paid_alerts(notifications)
    # Stap 3 — wachtrij voor gratis kanaal (6 uur via send_free_alerts.py)
    queue_free_alerts(notifications)


def main() -> int:
    print("Script gestart.", flush=True)
    stores_data = load_json(STORES_FILE)
    state = load_json(STATE_FILE)
    stores = stores_data.get("stores", [])
    active_stores = [s for s in stores if s.get("active", True)]

    print(f"Checking {len(active_stores)} entr{'y' if len(active_stores) == 1 else 'ies'}...\n", flush=True)

    shopify_cache: dict[str, bool] = {}
    shopify_by_domain: dict[str, set] = defaultdict(set)

    for store in active_stores:
        if store.get("type", "product") == "product":
            handle = shopify_handle(store["url"])
            if handle:
                domain = urlparse(store["url"]).netloc
                shopify_by_domain[domain].add(handle)

    for domain, handles in shopify_by_domain.items():
        print(f"[Shopify] {domain} — {len(handles)} producten ophalen...", flush=True)
        availability = fetch_shopify_bulk(domain, handles)
        for handle, available in availability.items():
            shopify_cache[f"{domain}/{handle}"] = available
        print(f"  → {len(availability)} gevonden, {len(handles) - len(availability)} niet gevonden\n", flush=True)

    state_changed = False

    for store in active_stores:
        store_type = store.get("type", "product")
        print(f"→ {store['name']} [{store_type}]", flush=True)

        if store_type == "category":
            notifications, changed = check_category(store, state)
            if changed:
                state_changed = True
            before = len(notifications)
            notifications = [n for n in notifications if is_tcg_product(n["name"])]
            filtered = before - len(notifications)
            if filtered:
                print(f"  [filter] {filtered} niet-TCG producten genegeerd", flush=True)
            notify_with_delay(notifications)
            time.sleep(1)

        else:
            url = store["url"]
            store_id = store["id"]
            selector = store.get("selector")

            handle = shopify_handle(url)
            if handle:
                domain = urlparse(url).netloc
                cache_key = f"{domain}/{handle}"
                if cache_key not in shopify_cache:
                    print("  [?] Niet gevonden in Shopify catalog", flush=True)
                    continue
                status = shopify_cache[cache_key]
            else:
                html = fetch_page(url)
                if html is None:
                    continue
                status = is_in_stock_html(url, html, selector)
                time.sleep(1)

            if status is None:
                print("  [?] Status onbekend", flush=True)
                continue

            prev = state.setdefault(store_id, {})
            prev_status = prev.get("in_stock")
            prev["in_stock"] = status
            prev["last_checked"] = now_utc()

            if status:
                print("  [✓] OP VOORRAAD", flush=True)
                if prev_status is False and cooldown_passed(prev.get("last_notified")):
                    state_changed = True
                    if is_tcg_product(store["name"]):
                        notify_with_delay([{"name": store["name"], "url": url}])
                        prev["last_notified"] = datetime.now(timezone.utc).isoformat()
                    else:
                        print(f"  [filter] Geen TCG-product, melding overgeslagen", flush=True)
            else:
                print("  [✗] Niet op voorraad", flush=True)
                if prev_status is True:
                    prev.pop("last_notified", None)
                    state_changed = True

    if state_changed:
        save_json(STATE_FILE, state)
        print("\nState opgeslagen.", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
