#!/usr/bin/env python3
"""Two-weekly full Sharp-Wallet re-measurement for polymarket-brief.

Runs on GitHub Actions (unrestricted internet). Scans the leaderboard for
candidate wallets, measures each one's Closing Line Value (price move 7
days after entry, volume-weighted) from real trade history, and writes a
shrunk base-edge estimate per wallet. This replaces the old workflow of
running pull.py + analyze.py by hand on a Mac.

Output: data/analysis_biweekly.json
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
HEADERS = {"User-Agent": "polymarket-brief-bot/1.0"}
RETRIES = 3
SLEEP = 0.25

# Existing watchlist – always re-measured even if they drop off the
# leaderboard temporarily. Keep in sync with the daily task's WATCHLIST.
KNOWN_WALLETS = {
    "Somali-Nationalist": "0x1f9e15f39bbd5d163b0eacf0fbef647ced9e4f1a",
    "eCash": "0x62cf46cd4c3af254dccfc37a7f93de265b4b5826",
    "coali10": "0x7bc14171ccb0d3e6bac219ec6a76211826e28db4",
    "RememberAmalek": "0x6139c42e48cf190e67a0a85d492413b499336b7a",
    "DirkDiggler67": "0xaab9f5e600a5dd88fe3a6f93313b180f6220a08d",
    "tetrose": "0x74471a007ddcc488f6d57b5e86dfb35a8d48a16d",
    "BobInvestments": "0x41816fc1ebdfeb33f6356f2655ab499253b3de86",
}

# Usernames already investigated and rejected — never re-add automatically.
AUSSORTIERT = {
    "pleaseplease123", "sainttroplay", "wr0ngw4yb3tt0r", "Flaznorp",
    "ferrariChampions2026", "balthazar", "totoro3miyazaki", "BillyGating",
    "Talvez10", "11vsldfdsgfkjgos", "WTSA", "Jsram", "matanovik",
    "gambamaster", "theowalcott", "ColomboHex", "ExplosiveNinja",
    "zofgkt1111", "e46m3", "Railcool", "Mysaria", "SASGOLD", "donthackme",
    "suhail-frenz-account187", "northdrawer", "hansama231", "robban888",
    "b324u", "CongoleseBorat", "Elenes", "JnStrtPrdctnMrkts", "cigarettes",
    "merod", "korda77", "mustbethewater", "fishalive", "mintblade",
    "frostrizz", "Len9311238", "sparklingwater123", "GRIMDRIP", "RepTrump",
    "endlessFate", "Anjun", "gaven-willwin", "CryptoVagabond", "Hourglass",
    "Corlys", "alwayslatetotheparty", "godblessme2026", "quietparcel",
    "smallreceipt", "dddtrips", "Mustafa0101", "VictorLudorum", "TwoEyes",
    "truthteller", "Trump2028", "BigRabbit",
}

MAX_CANDIDATES = 70          # total wallets analyzed this run
MAX_TOKENS = 1500            # total unique price-history lookups this run
MIN_TRADES_FOR_SIGNAL = 5

session_token_budget = {"used": 0}


def fetch(url, is_json=True):
    last_err = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if is_json else raw
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


def load_candidates():
    candidates = {}  # address -> {"name": userName or known name, "pnl":..., "vol":...}
    for name, addr in KNOWN_WALLETS.items():
        candidates[addr.lower()] = {"name": name, "pnl": None, "vol": None}

    for period in ("MONTH", "ALL"):
        url = f"https://data-api.polymarket.com/v1/leaderboard?timePeriod={period}&orderBy=PNL&limit=100"
        data = fetch(url)
        time.sleep(SLEEP)
        if not isinstance(data, list):
            continue
        for row in data:
            addr = str(row.get("proxyWallet", "")).lower()
            uname = row.get("userName", "")
            if not addr:
                continue
            if uname in AUSSORTIERT:
                continue
            if addr in candidates:
                continue
            pnl = row.get("pnl")
            vol = row.get("vol")
            if pnl is None or vol is None or vol <= 0:
                continue
            roi = pnl / vol
            if pnl < 50000 or roi < 0.10:
                continue
            candidates[addr] = {"name": uname, "pnl": pnl, "vol": vol}
            if len(candidates) >= MAX_CANDIDATES:
                break
        if len(candidates) >= MAX_CANDIDATES:
            break

    return candidates


def get_activity(addr):
    url = f"https://data-api.polymarket.com/activity?user={addr}&limit=500&type=TRADE"
    data = fetch(url)
    time.sleep(SLEEP)
    return data if isinstance(data, list) else []


def price_at_or_after(history, target_ts):
    """history: list of {t, p} sorted ascending. Returns nearest point at or
    after target_ts, or the last point if target_ts is beyond the series
    (market already resolved/closed by then)."""
    if not history:
        return None
    for point in history:
        t = point.get("t")
        if t is not None and t >= target_ts:
            return point.get("p")
    return history[-1].get("p")


def get_price_history(token_id, cache):
    if token_id in cache:
        return cache[token_id]
    if session_token_budget["used"] >= MAX_TOKENS:
        cache[token_id] = None
        return None
    url = f"https://clob.polymarket.com/prices-history?market={token_id}&interval=max&fidelity=1440"
    data = fetch(url)
    time.sleep(SLEEP)
    session_token_budget["used"] += 1
    hist = None
    if isinstance(data, dict) and isinstance(data.get("history"), list):
        hist = sorted(
            (p for p in data["history"] if isinstance(p.get("t"), (int, float))),
            key=lambda p: p["t"],
        )
    cache[token_id] = hist
    return hist


def analyze_wallet(addr, meta, price_cache):
    activity = get_activity(addr)
    entries = []
    for tr in activity:
        try:
            if tr.get("side") != "BUY" or tr.get("type") != "TRADE":
                continue
            price = float(tr.get("price", 0))
            if not (0.05 <= price <= 0.92):
                continue
            asset = tr.get("asset")
            ts = tr.get("timestamp")
            size = float(tr.get("size", 0)) or 1.0
            if not asset or ts is None:
                continue
            entries.append({"asset": asset, "price": price, "ts": int(ts), "size": size})
        except Exception:
            continue

    if len(entries) < MIN_TRADES_FOR_SIGNAL:
        return {
            "address": addr, "name": meta["name"], "pnl": meta["pnl"], "vol": meta["vol"],
            "n_trades_total": len(activity), "n_entries_used": len(entries),
            "sample": "insufficient", "clv7_cents": None, "base_edge": None,
            "flags": [],
        }

    weighted_sum = 0.0
    weight_total = 0.0
    used = 0
    for e in entries:
        hist = get_price_history(e["asset"], price_cache)
        if not hist:
            continue
        p7 = price_at_or_after(hist, e["ts"] + 7 * 86400)
        if p7 is None:
            continue
        clv = (float(p7) - e["price"]) * 100.0  # in cents
        weighted_sum += clv * e["size"]
        weight_total += e["size"]
        used += 1

    flags = []
    if len(activity) >= 5000:
        flags.append("very_high_frequency")
    if len(activity) and len(activity) >= 200:
        span_days = None
        try:
            ts_list = [tr.get("timestamp") for tr in activity if tr.get("timestamp")]
            if len(ts_list) >= 2:
                span_days = (max(ts_list) - min(ts_list)) / 86400
                if span_days and (len(activity) / span_days) > 50:
                    flags.append("possible_hft_or_market_maker")
        except Exception:
            pass

    if weight_total <= 0 or used < MIN_TRADES_FOR_SIGNAL:
        return {
            "address": addr, "name": meta["name"], "pnl": meta["pnl"], "vol": meta["vol"],
            "n_trades_total": len(activity), "n_entries_used": used,
            "sample": "insufficient", "clv7_cents": None, "base_edge": None,
            "flags": flags,
        }

    clv7 = weighted_sum / weight_total
    shrunk = clv7 * (used / (used + 800))
    base_edge = round(min(6.0, max(0.0, shrunk)), 1)

    return {
        "address": addr, "name": meta["name"], "pnl": meta["pnl"], "vol": meta["vol"],
        "n_trades_total": len(activity), "n_entries_used": used,
        "sample": "ok", "clv7_cents": round(clv7, 2), "base_edge": base_edge,
        "flags": flags,
    }


def main():
    candidates = load_candidates()
    price_cache = {}
    results = []
    for addr, meta in candidates.items():
        try:
            results.append(analyze_wallet(addr, meta, price_cache))
        except Exception as e:
            results.append({
                "address": addr, "name": meta.get("name"), "sample": "error",
                "error": str(e),
            })

    results.sort(key=lambda r: (r.get("base_edge") is None, -(r.get("base_edge") or 0)))

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "n_candidates": len(candidates),
        "tokens_fetched": session_token_budget["used"],
        "results": results,
    }
    save("analysis_biweekly.json", out)
    print(json.dumps({"n_candidates": len(candidates), "tokens_fetched": session_token_budget["used"]}))


if __name__ == "__main__":
    main()
