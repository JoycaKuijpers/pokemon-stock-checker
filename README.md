# ⚡ Pokémon Stock Checker

Automatisch Pokémon producten monitoren op Nederlandse en Belgische webshops. Ontvang een Telegram-notificatie zodra iets op voorraad is.

## Hoe het werkt

```
Chrome extensie  →  GitHub API  →  stores.json  ←  GitHub Actions  →  Telegram
     (jij)                         (config)          (checker)          (alert)
```

1. **Chrome extensie** — Voeg producten toe via de browser. Schrijft direct naar `stores.json` op GitHub.
2. **GitHub Actions** — Draait elke 15 minuten, leest `stores.json`, checkt elke webshop.
3. **Telegram** — Stuurt een bericht zodra een product van "niet op voorraad" naar "op voorraad" gaat.

---

## Setup

### 1. Repository

Fork of push deze code naar een GitHub repository.

### 2. GitHub Secrets instellen

Ga naar **Settings → Secrets and variables → Actions** en voeg toe:

| Secret | Waarde |
|--------|--------|
| `TELEGRAM_BOT_TOKEN` | Token van @BotFather |
| `TELEGRAM_CHAT_ID` | ID van de community chat (Whop-leden) |
| `TELEGRAM_ADMIN_CHAT_ID` | Jouw persoonlijke chat ID (foutmeldingen) |

### 3. Telegram bot aanmaken

1. Open [@BotFather](https://t.me/BotFather) in Telegram
2. Stuur `/newbot` en volg de stappen
3. Kopieer het token
4. Stuur de bot een bericht, haal dan je Chat ID op via:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```

### 4. Chrome extensie installeren

1. Open Chrome → `chrome://extensions`
2. Schakel **Ontwikkelaarsmodus** in
3. Klik **Uitgepakte extensie laden** → selecteer de `extension/` map
4. Klik het extensie-icoontje → **Instellingen**
5. Vul je GitHub gebruikersnaam, repo-naam en Personal Access Token in
6. Sla op en test Telegram

### 5. Producten toevoegen

1. Ga naar een productpagina op een webshop (bv. bol.com, coolblue.nl, intertoys.nl)
2. Klik het extensie-icoontje
3. Vul een productnaam in
4. Optioneel: klik **Detecteren** voor een CSS selector
5. Klik **Toevoegen aan monitor**

De extensie voegt het product toe aan `stores.json` in je GitHub repo. De volgende GitHub Actions run pikt het op.

---

## Bestandsstructuur

```
pokemon-stock-checker/
├── .github/workflows/
│   └── stock-check.yml       # Draait elke 15 minuten
├── checker/
│   ├── check_stock.py        # Hoofd-script
│   ├── stores.json           # Lijst van te monitoren producten
│   ├── state.json            # Bijgehouden voorraadstatus (auto-update)
│   └── requirements.txt
└── extension/
    ├── manifest.json
    ├── popup.html / popup.js  # Producten toevoegen
    ├── options.html / options.js  # Instellingen
    └── background.js
```

## stores.json formaat

```json
{
  "stores": [
    {
      "id": "uniek-id",
      "name": "Bol.com – Pikachu Ex Box",
      "url": "https://www.bol.com/nl/nl/p/...",
      "selector": null,
      "added_at": "2026-05-06T12:00:00Z",
      "active": true
    }
  ]
}
```

- `selector` — CSS selector voor de "in winkelwagen" knop. `null` = automatische detectie.
- `active` — Zet op `false` om tijdelijk te pauzeren.

## Ondersteunde detectiemethoden

1. **JSON-LD structured data** — Meest betrouwbaar (bol.com, mediamarkt, fnac)
2. **Open Graph meta tags** — Veel webshops
3. **CSS selector** — Handmatig ingesteld via extensie
4. **Tekstheuristiek** — Zoekt naar Nederlandse tekst als "Uitverkocht", "In winkelwagen", etc.

## Frequentie aanpassen

In `.github/workflows/stock-check.yml`:
```yaml
- cron: "*/15 * * * *"   # Elke 15 minuten
- cron: "*/5 * * * *"    # Elke 5 minuten (minimum GitHub Actions)
- cron: "*/30 * * * *"   # Elke 30 minuten
```

> **Let op:** GitHub Actions gratis tier = 2.000 minuten/maand. Elke 15 min = ~2.880 runs/maand (net over de limiet voor een private repo). Gebruik elke 30 min voor public repos of als je binnen de limiet wilt blijven.
