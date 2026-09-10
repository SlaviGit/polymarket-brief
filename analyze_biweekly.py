#!/usr/bin/env python3
"""Two-weekly full Sharp-Wallet re-measurement for polymarket-brief.

Runs on GitHub Actions (unrestricted internet). No wallet addresses are
hardcoded: candidates come entirely from (a) the previous watchlist.json
(for continuity) and (b) a fresh leaderboard scan (for discovery) --
19 separate (period x category x orderBy) slices of the leaderboard
(MONTH/WEEK/ALL x Politics/Economics/Culture/Tech, plus overall PNL and
per-category VOL), each capped by the API itself at 50 rows regardless
of the limit= requested. This is deliberately much wider than a single
overall-PNL scan so a specialist sharp isn't drowned out by sports/
esports whales (2026-09-09 change, widened further same day per
request). Writes:

  data/analysis_biweekly.json  -- full raw results, for audit/reference
  data/watchlist.json          -- the roster the daily task and the daily
                                   pull actually use (active / watch tiers)

watchlist.json is regenerated automatically every run, using the PREVIOUS
watchlist.json (read from the checked-out repo) to decide when a wallet
that turned negative should finally be dropped. Nobody needs to hand-edit
wallet lists anywhere -- promoting/demoting a wallet is fully automatic.
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

# Usernames already investigated and permanently rejected (fraud/quality
# patterns that don't show up in the automatic flags below).
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

MAX_CANDIDATES = 400
MAX_TOKENS = 10000
MIN_TRADES_FOR_SIGNAL = 5
ACTIVE_EDGE_MIN = 1.0
MAX_ACTIVE = 40
# --- Liveness ---------------------------------------------------------------
# Eine gemessene Edge zaehlt nur, wenn das Wallet noch handelt. Ohne diese
# beiden Schranken landen Wallets, die vor Monaten aufgehoert haben, mit ihrer
# historischen CLV in "active" und blockieren dort Plaetze (Stand 10.09.2026:
# 8 von 13 aktiven Wallets ohne einen einzigen Trade in 30 Tagen).
CLV_LOOKBACK_DAYS = 120        # aeltere Trades zaehlen nicht mehr zur CLV
ACTIVE_MIN_TRADES_30D = 10     # darunter: dormant -> watch, nie active
MAX_WATCH = 50

session_token_budget = {"used": 0}


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


def load_previous_watchlist():
    path = os.path.join(DATA_DIR, "watchlist.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def load_candidates(prev_watchlist):
    candidates = {}
    if prev_watchlist:
        for tier in ("active", "watch"):
            for entry in prev_watchlist.get(tier, []):
                addr = str(entry.get("address", "")).lower()
                if addr:
                    candidates.setdefault(addr, {"name": entry.get("name"), "pnl": None, "vol": None})

    # The leaderboard endpoint silently hard-caps at 50 rows per call no
    # matter what `limit` says (tested: limit=25/50/100/300 all return at
    # most 50) -- so more data means more distinct (period, category,
    # orderBy) slices, not a bigger limit= value. Category scans go first
    # since they're the ones likely to surface a sharp who specializes in
    # geopolitics/US-politics/econ rather than a generic top-PNL whale.
    # WEEK is included even though it's empty on some days (e.g. Mondays,
    # per manual observation) -- fetch() + the isinstance check below just
    # skip it harmlessly when that happens. orderBy=VOL surfaces a mostly
    # different population (very high-volume, often low-PNL accounts) --
    # most fail the pnl/roi filter below and the rest get HFT-flagged
    # downstream anyway, but it costs little to check.
    CATEGORIES = ("POLITICS", "ECONOMICS", "CULTURE", "TECH")
    PERIODS = ("MONTH", "WEEK", "ALL")
    scans = []
    for cat in CATEGORIES:
        for period in PERIODS:
            scans.append((period, cat, 50, "PNL"))
    for cat in CATEGORIES:
        scans.append(("ALL", cat, 50, "VOL"))
    for period in PERIODS:
        scans.append((period, None, 50, "PNL"))

    for period, category, limit, order_by in scans:
        url = f"https://data-api.polymarket.com/v1/leaderboard?timePeriod={period}&orderBy={order_by}&limit={limit}"
        if category:
            url += f"&category={category}"
        data = fetch(url)
        time.sleep(SLEEP)
        if not isinstance(data, list):
            continue
        for row in data:
            addr = str(row.get("proxyWallet", "")).lower()
            uname = row.get("userName", "")
            if not addr or uname in AUSSORTIERT or addr in candidates:
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
    now_ts = int(time.time())
    ts_all = [int(tr["timestamp"]) for tr in activity if tr.get("timestamp")]
    last_trade_age_days = ((now_ts - max(ts_all)) / 86400.0) if ts_all else None
    trades_30d = sum(1 for t in ts_all if now_ts - t <= 30 * 86400)
    clv_cutoff_ts = now_ts - CLV_LOOKBACK_DAYS * 86400
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
            if int(ts) < clv_cutoff_ts:
                continue
            entries.append({"asset": asset, "price": price, "ts": int(ts), "size": size})
        except Exception:
            continue

    base = {
        "address": addr, "name": meta["name"], "pnl": meta["pnl"], "vol": meta["vol"],
        "n_trades_total": len(activity), "n_entries_used": len(entries),
        "trades_30d": trades_30d,
        "last_trade_age_days": round(last_trade_age_days, 1) if last_trade_age_days is not None else None,
    }

    if len(entries) < MIN_TRADES_FOR_SIGNAL:
        return {**base, "sample": "insufficient", "clv7_cents": None, "base_edge": None, "flags": []}

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
        clv = (float(p7) - e["price"]) * 100.0
        weighted_sum += clv * e["size"]
        weight_total += e["size"]
        used += 1

    flags = []
    if len(activity) >= 5000:
        flags.append("very_high_frequency")
    ts_list = [tr.get("timestamp") for tr in activity if tr.get("timestamp")]
    if len(ts_list) >= 2:
        span_days = (max(ts_list) - min(ts_list)) / 86400
        if span_days and (len(activity) / span_days) > 50:
            flags.append("possible_hft_or_market_maker")

    base["n_entries_used"] = used if used else len(entries)

    if weight_total <= 0 or used < MIN_TRADES_FOR_SIGNAL:
        return {**base, "sample": "insufficient", "clv7_cents": None, "base_edge": None, "flags": flags}

    clv7 = weighted_sum / weight_total
    shrunk = clv7 * (used / (used + 800))
    base_edge = round(min(6.0, max(0.0, shrunk)), 1)

    return {**base, "sample": "ok", "clv7_cents": round(clv7, 2), "base_edge": base_edge, "flags": flags}


def entry_fields(r):
    return {
        "name": r["name"], "address": r["address"],
        "base_edge": r.get("base_edge") or 0.0,
        "clv7_cents": r.get("clv7_cents"),
        "n_entries_used": r.get("n_entries_used"),
        "trades_30d": r.get("trades_30d"),
        "last_trade_age_days": r.get("last_trade_age_days"),
    }


def build_watchlist(results, prev_watchlist):
    prev_status = {}
    if prev_watchlist:
        for tier in ("active", "watch"):
            for w in prev_watchlist.get(tier, []):
                prev_status[w["address"].lower()] = {"tier": tier, "base_edge": w.get("base_edge") or 0.0}

    by_addr = {r["address"].lower(): r for r in results}

    active, watch, excluded = [], [], []

    for addr, r in by_addr.items():
        prev = prev_status.get(addr)

        if r.get("sample") != "ok":
            if prev and prev["tier"] in ("active", "watch"):
                watch.append({"name": r["name"], "address": r["address"],
                              "base_edge": prev["base_edge"], "clv7_cents": None,
                              "n_entries_used": r.get("n_entries_used"), "reason": "stale_no_new_sample"})
            continue

        edge = r.get("base_edge") or 0.0
        flagged = bool(r.get("flags"))
        dormant = (r.get("trades_30d") or 0) < ACTIVE_MIN_TRADES_30D

        if flagged:
            watch.append({**entry_fields(r), "reason": "hft_flag"})
        elif dormant:
            watch.append({**entry_fields(r), "reason": "dormant"})
        elif edge >= ACTIVE_EDGE_MIN:
            active.append({**entry_fields(r), "reason": "active"})
        elif edge > 0:
            watch.append({**entry_fields(r), "reason": "low_edge"})
        else:
            if prev and prev["tier"] in ("active", "watch") and (prev["base_edge"] or 0) <= 0:
                excluded.append({"name": r["name"], "address": r["address"], "reason": "negative_twice_in_a_row"})
            else:
                watch.append({**entry_fields(r), "reason": "negative_edge_first_time"})

    active.sort(key=lambda w: -w["base_edge"])
    # Wallets that clear the active bar (edge >= ACTIVE_EDGE_MIN, unflagged)
    # but don't fit in MAX_ACTIVE slots must NOT just vanish -- demote them
    # to watch (confirmation-only) instead of dropping them.
    if len(active) > MAX_ACTIVE:
        overflow = active[MAX_ACTIVE:]
        active = active[:MAX_ACTIVE]
        for w in overflow:
            watch.append({**w, "reason": "active_overflow"})

    def watch_priority(w):
        was_tracked = w["address"].lower() in prev_status
        return (0 if was_tracked else 1, -(w["base_edge"] or 0))
    watch.sort(key=watch_priority)
    if len(watch) > MAX_WATCH:
        print(f"WARNUNG: {len(watch) - MAX_WATCH} watch-taugliche Wallets ueber MAX_WATCH={MAX_WATCH} hinaus verworfen")
    watch = watch[:MAX_WATCH]

    return {"active": active, "watch": watch, "excluded_this_run": excluded}


def main():
    prev_watchlist = load_previous_watchlist()
    candidates = load_candidates(prev_watchlist)
    price_cache = {}
    results = []
    for addr, meta in candidates.items():
        try:
            results.append(analyze_wallet(addr, meta, price_cache))
        except Exception as e:
            results.append({"address": addr, "name": meta.get("name"), "sample": "error", "error": str(e)})

    results.sort(key=lambda r: (r.get("base_edge") is None, -(r.get("base_edge") or 0)))

    analysis_out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "n_candidates": len(candidates),
        "tokens_fetched": session_token_budget["used"],
        "results": results,
    }
    save("analysis_biweekly.json", analysis_out)

    watchlist = build_watchlist(results, prev_watchlist)
    watchlist["generated_at_utc"] = analysis_out["generated_at_utc"]
    save("watchlist.json", watchlist)

    print(json.dumps({
        "n_candidates": len(candidates),
        "tokens_fetched": session_token_budget["used"],
        "active": len(watchlist["active"]),
        "watch": len(watchlist["watch"]),
        "excluded_this_run": len(watchlist["excluded_this_run"]),
    }))


if __name__ == "__main__":
    main()
