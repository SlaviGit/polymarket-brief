#!/usr/bin/env python3
"""Daily Polymarket data pull for polymarket-brief.
Runs on GitHub Actions (normal internet, no restrictions). Writes plain
JSON into data/ which the daily tip task then reads via
raw.githubusercontent.com instead of calling the live APIs itself.
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

WALLETS = {
    "Somali-Nationalist": "0x1f9e15f39bbd5d163b0eacf0fbef647ced9e4f1a",
    "eCash": "0x62cf46cd4c3af254dccfc37a7f93de265b4b5826",
    "coali10": "0x7bc14171ccb0d3e6bac219ec6a76211826e28db4",
    "RememberAmalek": "0x6139c42e48cf190e67a0a85d492413b499336b7a",
    "DirkDiggler67": "0xaab9f5e600a5dd88fe3a6f93313b180f6220a08d",
    "tetrose": "0x74471a007ddcc488f6d57b5e86dfb35a8d48a16d",
    "Netrol": "0x23c8a4c266d10ba5846837eac391fea89ed6f293",
    "Slavi": "0x0FD4d56894D6e81CB9b8348C772C5Eaa4dd2E72f",
}

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
HEADERS = {"User-Agent": "polymarket-brief-bot/1.0"}
RETRIES = 3
SLEEP = 0.4


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


def main():
    errors = []

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
        save("markets.json", data)
    else:
        errors.append("markets")
    time.sleep(SLEEP)

    wallets_out = {}
    for name, addr in WALLETS.items():
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
        wallets_out[name] = {"address": addr, **entry}
        save(f"wallets/{addr}.json", wallets_out[name])

    save("wallets_index.json", {n: a for n, a in WALLETS.items()})

    meta = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "wallets": list(WALLETS.keys()),
        "errors": errors,
    }
    save("meta.json", meta)
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
