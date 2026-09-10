#!/usr/bin/env python3
"""Polymarket data pull for polymarket-brief.

Runs on GitHub Actions (normal internet, no restrictions). Writes plain
JSON into data/ which the daily tip task then reads via
raw.githubusercontent.com instead of calling the live APIs itself.

No wallet addresses are hardcoded here. The set of wallets tracked is
read entirely from data/watchlist.json, which the biweekly analysis
workflow (analyze_biweekly.py) regenerates automatically. If that file
is missing or empty, this run simply fetches nothing but Slavi's own
wallet -- a quiet, honest degradation instead of falling back to a
stale hardcoded list.

Seit 10.09.2026 laeuft der Workflow stuendlich statt zweimal taeglich.
Zwei Anpassungen haengen daran:

1. markets.json wird auf die Felder eingedampft, die der Tagesbrief
   wirklich liest. Die Gamma-Antwort schleppt pro Markt ein komplettes
   verschachteltes Event-Objekt plus Bilder, Gebuehrentabellen und
   Token-Ids mit -- rund 70 % der Bytes, die niemand auswertet. Bei 24
   Commits pro Tag statt 2 waere das der groesste Treiber des
   Repo-Wachstums.

2. Eine Wallet-Datei wird nur ueberschrieben, wenn der Kern-Abruf
   (positions) geklappt hat. Bei stuendlichen Laeufen erwischt man
   oefter einen API-Aussetzer; vorher htte ein solcher Lauf die gute
   Datei durch eine unvollstaendige ersetzt.
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

SLAVI_WALLET = "0x0FD4d56894D6e81CB9b8348C772C5Eaa4dd2E72f"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
HEADERS = {"User-Agent": "polymarket-brief-bot/1.0"}
RETRIES = 3
SLEEP = 0.15

# Felder, die der Tagesbrief/Abend-Check aus markets.json liest.
# Grosszuegig gehalten -- lieber ein Feld zu viel als ein fehlendes.
MARKET_FIELDS = (
    "id", "conditionId", "questionID", "question", "slug", "description",
    "outcomes", "outcomePrices", "bestBid", "bestAsk", "lastTradePrice",
    "spread", "liquidity", "liquidityNum", "liquidityClob",
    "volume", "volumeNum", "volume24hr", "volume1wk", "volume1mo",
    "oneHourPriceChange", "oneDayPriceChange", "oneWeekPriceChange",
    "startDate", "startDateIso", "endDate", "endDateIso",
    "active", "closed", "archived", "acceptingOrders", "enableOrderBook",
    "restricted", "competitive",
    "negRisk", "negRiskMarketID", "negRiskOther",
    "groupItemTitle", "groupItemThreshold",
    "umaResolutionStatus", "umaResolutionStatuses",
    "resolutionSource", "resolvedBy",
    "orderPriceMinTickSize", "orderMinSize", "updatedAt",
)

# Aus dem Event-Objekt reicht die Klammer, die Leiter-Sprossen und
# negRisk-Gruppen zusammenhaelt.
EVENT_FIELDS = (
    "id", "slug", "ticker", "title", "endDate",
    "negRisk", "enableNegRisk", "negRiskMarketID",
    "liquidity", "volume24hr",
)


def fetch(url):
    last_err = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(1 + attempt)
    print(f"FEHLER bei {url}: {last_err}")
    return None


def save(path, obj):
    full = os.path.join(DATA_DIR, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        json.dump(obj, f)


def slim_market(m):
    """Nur die ausgewerteten Felder behalten (siehe Modul-Docstring)."""
    out = {k: m[k] for k in MARKET_FIELDS if k in m}
    events = m.get("events")
    if isinstance(events, list) and events:
        out["events"] = [
            {k: e[k] for k in EVENT_FIELDS if k in e}
            for e in events
            if isinstance(e, dict)
        ]
    return out


def load_watchlist_wallets():
    """Build {name: address} from data/watchlist.json (active + watch
    tiers), which the biweekly analysis run maintains automatically.
    No hardcoded wallets anywhere -- an unreadable/missing file just
    means today's pull covers Slavi's own wallet only."""
    path = os.path.join(DATA_DIR, "watchlist.json")
    wallets = {}
    try:
        with open(path) as f:
            wl = json.load(f)
        for tier in ("active", "watch"):
            for entry in wl.get(tier, []):
                name = entry.get("name")
                addr = entry.get("address")
                if name and addr:
                    wallets[name] = addr
    except Exception as e:
        print(f"watchlist.json nicht lesbar oder fehlt ({e}) -- heute keine Watchlist-Wallets, nur Slavis eigenes Wallet")
    return wallets


def main():
    errors = []
    skipped = []

    for period in ("MONTH", "ALL"):
        url = f"https://data-api.polymarket.com/v1/leaderboard?timePeriod={period}&orderBy=PNL&limit=150"
        data = fetch(url)
        if data is not None:
            save(f"leaderboard_{period.lower()}.json", data)
        else:
            errors.append(f"leaderboard_{period}")
        time.sleep(SLEEP)

    url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&order=volume24hr&ascending=false&limit=200"
    data = fetch(url)
    if data is not None:
        markets = data if isinstance(data, list) else data.get("data", data)
        if isinstance(markets, list):
            save("markets.json", [slim_market(m) for m in markets if isinstance(m, dict)])
        else:
            save("markets.json", data)
    else:
        errors.append("markets")
    time.sleep(SLEEP)

    wallets = load_watchlist_wallets()
    wallets["Slavi"] = SLAVI_WALLET

    for name, addr in wallets.items():
        entry = {}
        for key, tpl in (
            ("positions", "https://data-api.polymarket.com/positions?user={}&limit=500"),
            ("closed_positions", "https://data-api.polymarket.com/closed-positions?user={}&limit=500"),
            ("activity", "https://data-api.polymarket.com/activity?user={}&type=TRADE&limit=500"),
            ("value", "https://data-api.polymarket.com/value?user={}"),
        ):
            d = fetch(tpl.format(addr))
            if d is not None:
                entry[key] = d
            else:
                errors.append(f"{name}:{key}")
            time.sleep(SLEEP)

        # Kern-Abruf gescheitert -> lieber die letzte gute Datei stehen
        # lassen, als sie durch eine halbe zu ersetzen.
        target = f"wallets/{addr.lower()}.json"
        if "positions" not in entry and os.path.exists(os.path.join(DATA_DIR, target)):
            skipped.append(name)
            print(f"{name}: positions fehlgeschlagen -- bestehende Datei bleibt unveraendert")
            continue

        save(target, {"address": addr, **entry})

    save("wallets_index.json", {n: a for n, a in wallets.items()})

    meta = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "wallets": list(wallets.keys()),
        "errors": errors,
        "stale_wallets": skipped,
    }
    save("meta.json", meta)
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
