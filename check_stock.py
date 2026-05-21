#!/usr/bin/env python3
"""
Pokémon Stock Checker
Monitors Dutch/Belgian webshop categories and alerts when products come back in stock.
"""

import hashlib
import json
import os
import random
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).parent
STORES_FILE = next(
    (p for p in [SCRIPT_DIR / "stores.json", SCRIPT_DIR.parent / "stores.json"] if p.exists()),
    SCRIPT_DIR / "stores.json",
)
STATE_FILE = STORES_FILE.parent / "state.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "")

NOTIFY_COOLDOWN_HOURS = 2
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_BASE = 2  # seconden; wachttijd verdubbelt per poging
ERROR_ALERT_THRESHOLD = 3  # Telegram-alert na dit aantal opeenvolgende mislukte checks
MAX_WORKERS = 10  # Aantal stores dat tegelijk gecheckt wordt

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def get_headers() -> dict:
    """Geeft browser-headers terug met een willekeurige User-Agent."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
    }


# Aparte headers voor JSON API calls (Shopify) — geen browser-navigatie-indicatoren
def get_api_headers() -> dict:
    """Geeft minimale headers voor JSON API calls (geen browser-navigatie-indicatoren)."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, */*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
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


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def _get_with_retry(url: str, **kwargs) -> requests.Response:
    """GET met exponential backoff. Herprobeert alleen bij verbindingsfouten en 5xx-responses."""
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = requests.get(url, **kwargs)
            if resp.ok or resp.status_code < 500:
                # 2xx = succes; 4xx = client-fout, geen zin om te herproberen
                resp.raise_for_status()
                return resp
            # 5xx = serverfout, wel herproberen
            last_exc = requests.HTTPError(f"Server error {resp.status_code}", response=resp)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
        if attempt < RETRY_ATTEMPTS - 1:
            wait = RETRY_BACKOFF_BASE ** attempt + random.uniform(0, 1)
            time.sleep(wait)
    raise last_exc


def fetch_page(url: str) -> Optional[str]:
    try:
        resp = _get_with_retry(url, headers=get_headers(), timeout=(5, 10), allow_redirects=True)
        return resp.text
    except requests.RequestException as e:
        print(f"  [FOUT] Kon {url} niet ophalen na {RETRY_ATTEMPTS} pogingen: {e}", file=sys.stderr)
        return None


# ── Product state tracking ────────────────────────────────────────────────────

def process_product(prod_state: dict, key: str, name: str, url: str, available: bool,
                    notify_on_new: bool = False, image: str = "", price: str = "") -> Optional[dict]:
    """
    Update state for one product. Returns a notification dict when:
    - Product transitions from out-of-stock → in-stock, OR
    - Product is new (never seen before) AND notify_on_new is True
      (used for pre-order/empty categories).
    """
    entry = prod_state.setdefault(key, {})
    prev_status = entry.get("in_stock")  # None = first time, True/False = known

    entry.update({"name": name, "url": url, "in_stock": available, "last_checked": now_utc()})

    if available:
        is_new = prev_status is None
        came_back = prev_status is False
        if (came_back or (is_new and notify_on_new)) and cooldown_passed(entry.get("last_notified")):
            entry["last_notified"] = datetime.now(timezone.utc).isoformat()
            return {"name": name, "url": url, "image": image, "price": price}
    else:
        entry.pop("last_notified", None)

    return None


# ── Shopify ───────────────────────────────────────────────────────────────────

def shopify_handle(url: str) -> Optional[str]:
    m = re.search(r"/products/([^/?#]+)", url)
    return m.group(1) if m else None


def fetch_shopify_collection(domain: str, collection_handle: str) -> list[dict]:
    """Fetch all products from a Shopify collection via the public JSON API."""
    products = []
    page = 1
    while True:
        try:
            resp = _get_with_retry(
                f"https://{domain}/collections/{collection_handle}/products.json",
                params={"limit": 250, "page": page},
                headers=get_api_headers(),
                timeout=(5, 15),
            )
            batch = resp.json().get("products", [])
            if not batch:
                break
            products.extend(batch)
            if len(batch) < 250:
                break
            page += 1
            time.sleep(0.3)
        except (requests.RequestException, ValueError):
            break
    return products


def fetch_shopify_bulk(domain: str, handles: set) -> dict[str, bool]:
    """Fetch availability for individual Shopify products by handle (product mode)."""
    result = {}
    page = 1
    remaining = set(handles)
    while remaining:
        try:
            resp = _get_with_retry(
                f"https://{domain}/products.json",
                params={"limit": 250, "page": page},
                headers=get_api_headers(),
                timeout=(5, 15),
            )
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
        except (requests.RequestException, ValueError):
            break
    return result


# ── HTML availability helpers (product-page mode) ────────────────────────────

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


# ── Category checkers ─────────────────────────────────────────────────────────

def check_shopify_category(store: dict, state: dict) -> tuple[list[dict], bool]:
    """Check a Shopify /collections/ page. Returns (notifications, success)."""
    url = store["url"]
    m = re.search(r"/collections/([^/?#]+)", url)
    if not m:
        print("  [!] Geen collection handle gevonden in URL")
        return [], False

    domain = urlparse(url).netloc
    handle = m.group(1)
    products = fetch_shopify_collection(domain, handle)

    if not products:
        print("  [!] Geen producten gevonden in collectie")
        return [], False

    print(f"  → {len(products)} producten in collectie")

    cat_state = state.setdefault(store["id"], {"products": {}})
    prod_state = cat_state.setdefault("products", {})
    cat_state["last_checked"] = now_utc()

    notify_on_new = store.get("notify_on_new", False)
    notifications = []
    for p in products:
        available = any(v.get("available", False) for v in p.get("variants", []))
        name = normalize_name(p["title"])
        prod_url = f"https://{domain}/products/{p['handle']}"
        image = (p.get("images") or [{}])[0].get("src", "")
        raw_price = (p.get("variants") or [{}])[0].get("price", "")
        price = f"€ {raw_price}" if raw_price else ""
        notif = process_product(prod_state, p["handle"], name, prod_url, available, notify_on_new, image, price)
        if notif:
            notifications.append(notif)

    in_stock = sum(1 for e in prod_state.values() if e.get("in_stock"))
    print(f"  → {in_stock}/{len(products)} op voorraad, {len(notifications)} nieuw op voorraad")
    return notifications, True


def get_woocommerce_max_page(soup: BeautifulSoup) -> int:
    """Extract the highest page number from WooCommerce pagination links."""
    max_page = 1
    for a in soup.select("a.page-numbers, .woocommerce-pagination a"):
        try:
            n = int(a.get_text(strip=True))
            if n > max_page:
                max_page = n
        except ValueError:
            continue
    return max_page


def _card_stock_from_button_or_text(card: BeautifulSoup) -> Optional[bool]:
    """Fallback stock detection via add-to-cart button or page text."""
    btn = card.select_one("a.add_to_cart_button, button.add_to_cart_button, .ajax_add_to_cart")
    if btn:
        return not (btn.get("disabled") or "disabled" in btn.get("class", []))
    text = card.get_text(" ", strip=True).lower()
    if any(re.search(p, text) for p in OUT_OF_STOCK_PATTERNS):
        return False
    if any(re.search(p, text) for p in IN_STOCK_PATTERNS):
        return True
    return None


def parse_woocommerce_cards(soup: BeautifulSoup) -> list[dict]:
    """Extract product name, url and stock status from WooCommerce product cards."""
    results = []
    for card in soup.select("li.product, div.product, article.product"):
        classes = card.get("class", [])
        if "instock" in classes:
            available = True
        elif "outofstock" in classes:
            available = False
        else:
            # No stock class — try button / text heuristic
            available = _card_stock_from_button_or_text(card)
            if available is None:
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

        img = card.select_one("img")
        image = img.get("src") or img.get("data-src") or img.get("data-lazy-src", "") if img else ""
        price_el = card.select_one(".price .woocommerce-Price-amount, .price")
        price = price_el.get_text(strip=True) if price_el else ""

        results.append({"name": name, "url": prod_url, "available": available, "image": image, "price": price})
    return results


def check_woocommerce_category(store: dict, state: dict, soup: BeautifulSoup) -> tuple[list[dict], bool]:
    """
    Check a WooCommerce category page — including all paginated pages.
    Uses the 'instock'/'outofstock' CSS classes; no per-product request needed.
    Returns (notifications, success).
    """
    base_url = store["url"].rstrip("/")
    max_page = get_woocommerce_max_page(soup)

    # Collect products from page 1 (already fetched) + remaining pages
    all_products = parse_woocommerce_cards(soup)

    for page in range(2, max_page + 1):
        page_url = f"{base_url}/page/{page}/"
        html = fetch_page(page_url)
        if not html:
            break
        all_products.extend(parse_woocommerce_cards(BeautifulSoup(html, "lxml")))
        time.sleep(0.5)

    if not all_products:
        print("  [!] Geen WooCommerce product cards gevonden")
        return [], False

    pages_str = f" ({max_page} pagina's)" if max_page > 1 else ""
    print(f"  → {len(all_products)} producten gevonden{pages_str}")
    return _apply_category_state(store, state, all_products)


# ── WooCommerce custom loop theme (e.g. tcgcompany.nl) ───────────────────────

def parse_woocommerce_loop_cards(soup: BeautifulSoup) -> list[dict]:
    """Parse custom WooCommerce themes that wrap products in div.inner-loop-product-holder.
    Stock is encoded as CSS classes: stock-instock / stock-one / stock-few / stock-many = available;
    stock-other / stock-outofstock / stock-backorder = not available.
    """
    IN_STOCK_CLASSES = {"stock-instock", "stock-one", "stock-few", "stock-many"}
    results = []
    for card in soup.select("div.inner-loop-product-holder"):
        classes = set(card.get("class", []))
        stock_cls = classes & (IN_STOCK_CLASSES | {"stock-other", "stock-outofstock", "stock-backorder"})
        if not stock_cls:
            continue
        available = bool(stock_cls & IN_STOCK_CLASSES)

        link = card.select_one("a.woocommerce-loop-product__link, a.woocommerce-LoopProduct-link, a[href]")
        if not link:
            continue
        prod_url = link.get("href", "")
        if not prod_url or not prod_url.startswith("http"):
            continue

        name_el = card.select_one(".woocommerce-loop-product__title, h2, h3")
        name = normalize_name((name_el.get_text(strip=True) if name_el else link.get_text(strip=True))[:120])
        if not name:
            continue

        img = card.select_one("img")
        image = (img.get("src") or img.get("data-src") or img.get("data-lazy-src", "")) if img else ""
        price_el = card.select_one(".woocommerce-Price-amount, .price")
        price = price_el.get_text(strip=True) if price_el else ""

        results.append({"name": name, "url": prod_url, "available": available, "image": image, "price": price})
    return results


def check_woocommerce_loop_category(store: dict, state: dict, soup: BeautifulSoup) -> tuple[list[dict], bool]:
    """Check a custom WooCommerce loop theme, including pagination."""
    base_url = store["url"].rstrip("/")
    max_page = get_woocommerce_max_page(soup)
    all_products = parse_woocommerce_loop_cards(soup)
    for page in range(2, max_page + 1):
        html = fetch_page(f"{base_url}/page/{page}/")
        if not html:
            break
        all_products.extend(parse_woocommerce_loop_cards(BeautifulSoup(html, "lxml")))
        time.sleep(0.5)
    if not all_products:
        print("  [!] Geen custom WooCommerce loop cards gevonden")
        return [], False
    pages_str = f" ({max_page} pagina's)" if max_page > 1 else ""
    print(f"  → {len(all_products)} producten gevonden{pages_str}")
    return _apply_category_state(store, state, all_products)


# ── WooCommerce Blocks ────────────────────────────────────────────────────────

def parse_woocommerce_block_cards(soup: BeautifulSoup) -> list[dict]:
    """Parse WooCommerce Gutenberg blocks product grid (wc-block-grid__product)."""
    results = []
    for card in soup.select("li.wc-block-grid__product"):
        link = card.select_one("a.wc-block-grid__product-link, a[href]")
        if not link:
            continue
        prod_url = link.get("href", "")
        if not prod_url or not prod_url.startswith("http"):
            continue
        name_el = card.select_one(".wc-block-grid__product-title, h2, h3")
        raw = name_el.get_text(strip=True) if name_el else link.get_text(strip=True)
        name = normalize_name(raw[:120])
        if not name:
            continue
        available = _card_stock_from_button_or_text(card)
        if available is None:
            continue
        img = card.select_one("img")
        image = img.get("src") or img.get("data-src") or img.get("data-lazy-src", "") if img else ""
        price_el = card.select_one(".wc-block-grid__product-price, .price")
        price = price_el.get_text(strip=True) if price_el else ""
        results.append({"name": name, "url": prod_url, "available": available, "image": image, "price": price})
    return results


def check_woocommerce_blocks_category(store: dict, state: dict, soup: BeautifulSoup) -> tuple[list[dict], bool]:
    """Check a WooCommerce Blocks product grid, including paginated pages."""
    base_url = store["url"].rstrip("/")
    max_page = get_woocommerce_max_page(soup)
    all_products = parse_woocommerce_block_cards(soup)
    for page in range(2, max_page + 1):
        html = fetch_page(f"{base_url}/page/{page}/")
        if not html:
            break
        all_products.extend(parse_woocommerce_block_cards(BeautifulSoup(html, "lxml")))
        time.sleep(0.5)
    if not all_products:
        print("  [!] Geen WooCommerce Blocks product cards gevonden")
        return [], False
    pages_str = f" ({max_page} pagina's)" if max_page > 1 else ""
    print(f"  → {len(all_products)} producten gevonden{pages_str}")
    return _apply_category_state(store, state, all_products)


# ── Magento 2 ─────────────────────────────────────────────────────────────────

def parse_magento_cards(soup: BeautifulSoup) -> list[dict]:
    """Parse Magento 2 category product listing (li.product-item)."""
    results = []
    for card in soup.select("li.product-item"):
        link = card.select_one("a.product-item-link")
        if not link:
            continue
        prod_url = link.get("href", "")
        name = normalize_name(link.get_text(strip=True)[:120])
        if not name or not prod_url:
            continue
        stock_el = card.select_one(".stock")
        if stock_el:
            available = "available" in stock_el.get("class", [])
        else:
            btn = card.select_one(".action.tocart")
            if btn:
                available = not btn.get("disabled")
            else:
                available = _card_stock_from_button_or_text(card)
                if available is None:
                    continue
        img = card.select_one("img.product-image-photo, img")
        image = img.get("src") or img.get("data-src", "") if img else ""
        price_el = card.select_one(".price-wrapper .price, .special-price .price, .price")
        price = price_el.get_text(strip=True) if price_el else ""
        results.append({"name": name, "url": prod_url, "available": available, "image": image, "price": price})
    return results


def check_magento_category(store: dict, state: dict, soup: BeautifulSoup) -> tuple[list[dict], bool]:
    """Check a Magento 2 category page including pagination (?p=N)."""
    base_url = re.sub(r"[?&]p=\d+", "", store["url"]).rstrip("?& /")
    max_page = 1
    for a in soup.select(".pages .item a, .pages-items a"):
        try:
            n = int(a.get_text(strip=True))
            if n > max_page:
                max_page = n
        except ValueError:
            continue
    all_products = parse_magento_cards(soup)
    for page in range(2, max_page + 1):
        sep = "&" if "?" in base_url else "?"
        html = fetch_page(f"{base_url}{sep}p={page}")
        if not html:
            break
        all_products.extend(parse_magento_cards(BeautifulSoup(html, "lxml")))
        time.sleep(0.5)
    if not all_products:
        print("  [!] Geen Magento product cards gevonden")
        return [], False
    pages_str = f" ({max_page} pagina's)" if max_page > 1 else ""
    print(f"  → {len(all_products)} producten gevonden{pages_str}")
    return _apply_category_state(store, state, all_products)


# ── Shopware 6 ────────────────────────────────────────────────────────────────

def parse_shopware_cards(soup: BeautifulSoup) -> list[dict]:
    """Parse Shopware 6 product listing (div.card.product-box)."""
    results = []
    for card in soup.select("div.card.product-box, .product-box"):
        link = card.select_one("a.product-name, a.product-image-link")
        if not link:
            continue
        prod_url = link.get("href", "")
        if not prod_url or not prod_url.startswith("http"):
            continue
        name_el = card.select_one(".product-name")
        raw = name_el.get_text(strip=True) if name_el else link.get_text(strip=True)
        name = normalize_name(raw[:120])
        if not name:
            continue
        btn = card.select_one("button.btn-buy, .btn-buy")
        if btn:
            available = not (btn.get("disabled") or "disabled" in btn.get("class", []))
        else:
            available = _card_stock_from_button_or_text(card)
            if available is None:
                continue
        img = card.select_one("img.product-image, img")
        image = img.get("src") or img.get("data-src", "") if img else ""
        price_el = card.select_one(".product-price, .price-unit-reference")
        price = price_el.get_text(strip=True) if price_el else ""
        results.append({"name": name, "url": prod_url, "available": available, "image": image, "price": price})
    return results


def check_shopware_category(store: dict, state: dict, soup: BeautifulSoup) -> tuple[list[dict], bool]:
    """Check a Shopware 6 category page including pagination (?p=N)."""
    base_url = re.sub(r"[?&]p=\d+", "", store["url"]).rstrip("?& /")
    max_page = 1
    for el in soup.select(".pagination a, .pagination-nav a"):
        try:
            n = int(el.get_text(strip=True))
            if n > max_page:
                max_page = n
        except ValueError:
            continue
    all_products = parse_shopware_cards(soup)
    for page in range(2, max_page + 1):
        sep = "&" if "?" in base_url else "?"
        html = fetch_page(f"{base_url}{sep}p={page}")
        if not html:
            break
        all_products.extend(parse_shopware_cards(BeautifulSoup(html, "lxml")))
        time.sleep(0.5)
    if not all_products:
        print("  [!] Geen Shopware product cards gevonden")
        return [], False
    pages_str = f" ({max_page} pagina's)" if max_page > 1 else ""
    print(f"  → {len(all_products)} producten gevonden{pages_str}")
    return _apply_category_state(store, state, all_products)


# ── Playwright (JS-rendered sites) ───────────────────────────────────────────

def fetch_page_js(url: str, wait_selector: Optional[str] = None) -> Optional[str]:
    """Laad een JS-rendered pagina met Playwright en geef de gerenderde HTML terug."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                locale="nl-NL",
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=8_000)
                except PWTimeout:
                    pass
            else:
                page.wait_for_timeout(3_000)
            html = page.content()
            browser.close()
        return html
    except Exception as e:
        print(f"  [FOUT] Playwright kon {url} niet laden: {e}", file=sys.stderr)
        return None


def fetch_js_api_responses(url: str, json_keys: tuple = ("products", "items", "results")) -> list[dict]:
    """
    Laad een pagina met Playwright en onderschep JSON API-responses.
    Geeft producten terug uit de eerste response die een van de opgegeven sleutels bevat.
    """
    captured: list[dict] = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                locale="nl-NL",
            )
            page = ctx.new_page()

            def on_response(response):
                if response.status != 200:
                    return
                if "json" not in response.headers.get("content-type", ""):
                    return
                try:
                    data = response.json()
                    for key in json_keys:
                        items = data.get(key)
                        if isinstance(items, list) and items:
                            captured.extend(items)
                            return
                        # Geneste structuur (bijv. {"results": {"products": [...]}})
                        if isinstance(data.get(key), dict):
                            sub = data[key].get("products") or data[key].get("items")
                            if isinstance(sub, list) and sub:
                                captured.extend(sub)
                                return
                except Exception:
                    pass

            page.on("response", on_response)
            page.goto(url, wait_until="networkidle", timeout=35_000)
            browser.close()
    except Exception as e:
        print(f"  [FOUT] Playwright API-onderschepping mislukt voor {url}: {e}", file=sys.stderr)
    return captured


# ── Dreamland ─────────────────────────────────────────────────────────────────

def parse_dreamland_api_products(products: list[dict]) -> list[dict]:
    """Vertaal Dreamland API-producten naar ons standaardformaat."""
    results = []
    for p in products:
        name = normalize_name((p.get("name") or p.get("title") or "")[:120])
        if not name:
            continue

        url = p.get("url") or p.get("link") or p.get("slug") or ""
        if url and not url.startswith("http"):
            url = f"https://www.dreamland.nl{url}"
        if not url:
            continue

        # Voorraad bepalen — diverse veldnamen proberen
        out_of_stock = p.get("outOfStock") or p.get("out_of_stock")
        avail_raw = p.get("available") or p.get("inStock") or p.get("availability")
        if out_of_stock is not None:
            available = not bool(out_of_stock)
        elif avail_raw is not None:
            available = (
                bool(avail_raw) if isinstance(avail_raw, bool)
                else str(avail_raw).lower() in ("true", "in_stock", "instock", "available", "1")
            )
        else:
            continue  # Geen voorraadinfo → overslaan

        image = p.get("image") or p.get("imageUrl") or p.get("img") or ""
        if image and not image.startswith("http"):
            image = f"https://www.dreamland.nl{image}"

        price_raw = p.get("price") or p.get("priceValue") or ""
        price = f"€ {price_raw}" if price_raw else ""

        results.append({"name": name, "url": url, "available": available, "image": image, "price": price})
    return results


def check_dreamland_category(store: dict, state: dict) -> tuple[list[dict], bool]:
    """Check een Dreamland categorie/zoekpagina via Playwright API-onderschepping."""
    print("  [JS] Dreamland — Playwright API-onderschepping...")
    raw = fetch_js_api_responses(store["url"])
    if not raw:
        print("  [!] Geen producten onderschept via Dreamland API")
        return [], False

    all_products = parse_dreamland_api_products(raw)
    if not all_products:
        print("  [!] Geen parseerbare producten in Dreamland API-response")
        return [], False

    print(f"  → {len(all_products)} producten gevonden via API-onderschepping")
    return _apply_category_state(store, state, all_products)


# ── Lobbes ────────────────────────────────────────────────────────────────────

def parse_lobbes_cards(soup: BeautifulSoup) -> list[dict]:
    """Parse Lobbes.nl productkaarten na JS-rendering."""
    results = []
    selectors = [
        "[class*='product-card']", "[class*='productcard']",
        "[class*='product-item']", "[class*='product_item']",
        "[class*='product-tile']", "[class*='producttile']",
        "article[class]",
    ]
    cards = []
    for sel in selectors:
        cards = soup.select(sel)
        if cards:
            break

    for card in cards:
        link = card.find("a", href=True)
        if not link:
            continue
        prod_url = link.get("href", "")
        if not prod_url:
            continue
        if not prod_url.startswith("http"):
            prod_url = f"https://www.lobbes.nl{prod_url}"

        name_el = card.select_one("h2, h3, [class*='title'], [class*='name']")
        name = normalize_name((name_el.get_text(strip=True) if name_el else link.get_text(strip=True))[:120])
        if not name:
            continue

        available = _card_stock_from_button_or_text(card)
        if available is None:
            available = True  # Geen duidelijke indicator → aannemen dat het beschikbaar is

        img = card.select_one("img")
        image = (img.get("src") or img.get("data-src") or img.get("data-lazy-src", "")) if img else ""
        price_el = card.select_one("[class*='price']")
        price = price_el.get_text(strip=True) if price_el else ""

        results.append({"name": name, "url": prod_url, "available": available, "image": image, "price": price})
    return results


def check_lobbes_category(store: dict, state: dict) -> tuple[list[dict], bool]:
    """Check een Lobbes.nl categoriepagina via Playwright."""
    print("  [JS] Lobbes — Playwright rendering...")
    html = fetch_page_js(store["url"], wait_selector="[class*='product']")
    if not html:
        return [], False

    soup = BeautifulSoup(html, "lxml")
    all_products = parse_lobbes_cards(soup)
    if not all_products:
        print("  [!] Geen Lobbes productkaarten gevonden na JS-rendering")
        return [], False

    print(f"  → {len(all_products)} producten gevonden")
    return _apply_category_state(store, state, all_products)


# ── Shared state helper ───────────────────────────────────────────────────────

def _apply_category_state(store: dict, state: dict, all_products: list[dict]) -> tuple[list[dict], bool]:
    """Update state and collect notifications for a parsed product list."""
    cat_state = state.setdefault(store["id"], {"products": {}})
    prod_state = cat_state.setdefault("products", {})
    cat_state["last_checked"] = now_utc()
    notify_on_new = store.get("notify_on_new", False)
    notifications = []
    for p in all_products:
        key = urlparse(p["url"]).path.strip("/").replace("/", "-")
        notif = process_product(prod_state, key, p["name"], p["url"], p["available"], notify_on_new,
                                p.get("image", ""), p.get("price", ""))
        if notif:
            notifications.append(notif)
    in_stock = sum(1 for e in prod_state.values() if e.get("in_stock"))
    print(f"  → {in_stock}/{len(all_products)} op voorraad, {len(notifications)} nieuw op voorraad")
    return notifications, True


# ── Platform auto-detect ──────────────────────────────────────────────────────

def check_category(store: dict, state: dict) -> tuple[list[dict], bool]:
    """Auto-detect platform and check the category page."""
    url = store["url"]
    domain = urlparse(url).netloc

    # JS-rendered sites — vaste domein-detectie, altijd via Playwright
    if "dreamland.nl" in domain:
        return check_dreamland_category(store, state)
    if "lobbes.nl" in domain:
        return check_lobbes_category(store, state)

    # Shopify: URL contains /collections/<handle>
    if re.search(r"/collections/[^/?#]+", url):
        return check_shopify_category(store, state)

    html = fetch_page(url)
    if not html:
        return [], False

    soup = BeautifulSoup(html, "lxml")

    if soup.select("div.inner-loop-product-holder"):
        return check_woocommerce_loop_category(store, state, soup)

    if soup.select("li.product, div.product, article.product"):
        return check_woocommerce_category(store, state, soup)

    if soup.select("li.wc-block-grid__product"):
        return check_woocommerce_blocks_category(store, state, soup)

    if soup.select("li.product-item"):
        return check_magento_category(store, state, soup)

    if soup.select("div.card.product-box, .product-box"):
        return check_shopware_category(store, state, soup)

    print("  [!] Platform niet herkend voor categorie-modus")
    return [], False


# ── Telegram ──────────────────────────────────────────────────────────────────

def _send_telegram_to(chat_id: str, message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
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
        print(f"  [FOUT] Telegram melding mislukt: {e}", file=sys.stderr)
        return False


def send_telegram(message: str) -> bool:
    """Stuur een voorraadmelding naar de community chat."""
    if not TELEGRAM_CHAT_ID:
        print("  [SKIP] TELEGRAM_CHAT_ID niet geconfigureerd.")
        return False
    return _send_telegram_to(TELEGRAM_CHAT_ID, message)


def send_telegram_admin(message: str) -> bool:
    """Stuur een beheerdersmelding (fouten, waarschuwingen) alleen naar de admin."""
    if not TELEGRAM_ADMIN_CHAT_ID:
        print("  [SKIP] TELEGRAM_ADMIN_CHAT_ID niet geconfigureerd.")
        return False
    return _send_telegram_to(TELEGRAM_ADMIN_CHAT_ID, message)


def build_notification(name: str, url: str, price: str = "") -> str:
    price_line = f"\n💰 <b>{price}</b>" if price else ""
    return (
        f"🟢 <b>OP VOORRAAD!</b>\n\n"
        f"🎮 <b>{name}</b>{price_line}\n"
        f"🛒 <a href=\"{url}\">{url}</a>\n\n"
        f"⚡ Wees er snel bij!"
    )


def send_telegram_photo(image_url: str, caption: str) -> bool:
    """Stuur een voorraadmelding met productfoto naar de community chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
            json={"chat_id": TELEGRAM_CHAT_ID, "photo": image_url,
                  "caption": caption, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"  [WARN] sendPhoto mislukt, fallback naar tekst: {e}", file=sys.stderr)
        return False


# ── Error tracking ────────────────────────────────────────────────────────────

def update_store_errors(store_id: str, state: dict, success: bool) -> Optional[str]:
    """
    Houdt opeenvolgende mislukte checks bij per store.
    Geeft een alert-tekst terug als de drempel bereikt wordt, anders None.
    """
    errors = state.setdefault("_errors", {})
    entry = errors.setdefault(store_id, {"count": 0})
    if success:
        entry["count"] = 0
        return None
    entry["count"] += 1
    if entry["count"] == ERROR_ALERT_THRESHOLD:
        return (
            f"⚠️ <b>Stock checker fout</b>\n\n"
            f"Store <code>{store_id}</code> heeft {ERROR_ALERT_THRESHOLD} "
            f"opeenvolgende checks mislukt. Controleer de URL of webshop."
        )
    return None


def stock_state_hash(state: dict) -> str:
    """Hash alleen de voorraad- en notificatiedata, zodat last_checked geen onnodige commits triggert."""
    def strip_timestamps(obj: object) -> object:
        if isinstance(obj, dict):
            return {k: strip_timestamps(v) for k, v in obj.items() if k not in ("last_checked", "_errors")}
        return obj

    canonical = json.dumps(strip_timestamps(state), sort_keys=True, ensure_ascii=False)
    return hashlib.md5(canonical.encode()).hexdigest()


# ── Per-store worker ──────────────────────────────────────────────────────────

def check_single_store(store: dict, state: dict, shopify_cache: dict) -> None:
    """Verwerk één store: check voorraad, update state, stuur Telegram-meldingen."""
    store_type = store.get("type", "product")
    store_id = store["id"]
    print(f"-> {store['name']} [{store_type}]")

    if store_type == "category":
        notifications, success = check_category(store, state)
        alert = update_store_errors(store_id, state, success)
        if alert:
            send_telegram_admin(alert)
        for notif in notifications:
            msg = build_notification(notif["name"], notif["url"], notif.get("price", ""))
            image = notif.get("image", "")
            if image:
                sent = send_telegram_photo(image, msg) or send_telegram(msg)
            else:
                sent = send_telegram(msg)
            if sent:
                print(f"  [OK] Melding verstuurd: {notif['name']}")
        time.sleep(1)
        return

    # Individual product mode
    url = store["url"]
    selector = store.get("selector")

    handle = shopify_handle(url)
    if handle:
        domain = urlparse(url).netloc
        cache_key = f"{domain}/{handle}"
        if cache_key not in shopify_cache:
            print("  [?] Niet gevonden in Shopify catalog")
            update_store_errors(store_id, state, success=False)
            return
        status = shopify_cache[cache_key]
    else:
        html = fetch_page(url)
        if html is None:
            alert = update_store_errors(store_id, state, success=False)
            if alert:
                send_telegram_admin(alert)
            return
        status = is_in_stock_html(url, html, selector)
        time.sleep(1)

    if status is None:
        print("  [?] Status onbekend")
        alert = update_store_errors(store_id, state, success=False)
        if alert:
            send_telegram_admin(alert)
        return

    update_store_errors(store_id, state, success=True)

    prev = state.setdefault(store_id, {})
    prev_status = prev.get("in_stock")
    prev["in_stock"] = status
    prev["last_checked"] = now_utc()

    if status:
        print("  [+] OP VOORRAAD")
        if prev_status is False and cooldown_passed(prev.get("last_notified")):
            sent = send_telegram(build_notification(store["name"], url))
            if sent:
                prev["last_notified"] = datetime.now(timezone.utc).isoformat()
                print("  [OK] Telegram melding verstuurd")
    else:
        print("  [-] Niet op voorraad")
        if prev_status is True:
            prev.pop("last_notified", None)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    stores_data = load_json(STORES_FILE)
    state = load_json(STATE_FILE)
    stores = stores_data.get("stores", [])
    active_stores = [s for s in stores if s.get("active", True)]

    print(f"Checking {len(active_stores)} entr{'y' if len(active_stores) == 1 else 'ies'} "
          f"met {MAX_WORKERS} parallelle workers...\n")

    initial_hash = stock_state_hash(state)

    # Pre-fetch Shopify availability in bulk for individual-product entries
    shopify_cache: dict[str, bool] = {}
    shopify_by_domain: dict[str, set] = defaultdict(set)

    for store in active_stores:
        if store.get("type", "product") == "product":
            handle = shopify_handle(store["url"])
            if handle:
                domain = urlparse(store["url"]).netloc
                shopify_by_domain[domain].add(handle)

    for domain, handles in shopify_by_domain.items():
        print(f"[Shopify] {domain} — {len(handles)} producten ophalen...")
        availability = fetch_shopify_bulk(domain, handles)
        for handle, available in availability.items():
            shopify_cache[f"{domain}/{handle}"] = available
        print(f"  -> {len(availability)} gevonden, {len(handles) - len(availability)} niet gevonden\n")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(check_single_store, store, state, shopify_cache): store
            for store in active_stores
        }
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                store = futures[future]
                print(f"  [FOUT] Onverwachte fout bij {store['name']}: {e}", file=sys.stderr)

    if stock_state_hash(state) != initial_hash:
        save_json(STATE_FILE, state)
        print("\nState opgeslagen.")
    else:
        print("\nGeen voorraadwijzigingen — state niet opgeslagen.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

