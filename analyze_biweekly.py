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

MAX_CANDIDATES = 800
MAX_TOKENS = 18000
MAX_ENTRIES_PER_WALLET = 150   # je Wallet reichen 150 Messpunkte; alles darueber
                               # kostet Token-Abrufe ohne den Standardfehler noch
                               # nennenswert zu senken (SE faellt mit 1/sqrt(n))
DEADLINE_MIN = 300             # harte Wanduhr-Grenze; GitHub bricht bei 360 ab.
                               # Ohne diese Schranke lief der Job mit 1200/30000
                               # rechnerisch 364 Min und waere mitten in der
                               # Messung abgebrochen -- ohne brauchbare Ausgabe.
MIN_TRADES_FOR_SIGNAL = 5
# Vorfilter auf der Leaderboard-Zeile, VOR jeder Messung. Stand 14.09.2026 warf
# er 21 von 50 Zeilen weg, alle am ROI -- also 42 % der Kandidaten, bevor auch
# nur eine CLV berechnet wurde. Das war der eigentliche Engpass, nicht
# MAX_CANDIDATES. Ein Wallet mit hohem Umsatz und 8 % ROI ist genau das Profil,
# das die VOL-Schnitte finden sollen; der alte Wert 0.10 hat sie sofort wieder
# aussortiert und die Verbreiterung des Scans damit weitgehend wirkungslos
# gemacht. Die CLV-Messung weiter unten ist der ehrliche Filter -- dieser hier
# soll nur offensichtlichen Unsinn fernhalten.
MIN_PNL = 25000
MIN_ROI = 0.05
ACTIVE_EDGE_MIN = 1.5
MAX_ACTIVE = 60

# --- Schrumpfung (2026-09-14 neu gefasst) ----------------------------------
# Vorher: base_edge = clv7 * used/(used+800) -- eine einzige globale Konstante,
# die zwei voellig verschiedene Korrekturen vermischte und beide falsch traf.
#
# (a) MESSRAUSCHEN. Ein Wallet, das Tagesmaerkte handelt (Fussball, Esports),
#     wird 7 Tage nach Einstieg gegen einen Preis von 0 oder 1 gemessen -- der
#     Markt ist laengst aufgeloest. Seine "CLV" ist realisierter Gewinn je
#     Anteil mit einer Streuung von rund 50c je Trade. Ein Wallet in langsamen
#     Maerkten wird gegen einen echten Zwischenpreis gemessen, Streuung rund
#     12c. Eine globale Konstante bestraft beide gleich und ist damit fuer das
#     eine zu lasch und fuer das andere zu hart. Neu wird je Wallet aus den
#     eigenen Einzel-CLVs der Standardfehler berechnet und damit geschrumpft
#     (Empirical Bayes): faktor = V_TRUE / (V_TRUE + se^2).
# (b) SELEKTIONSVERZERRUNG. Die Kandidaten kommen aus dem Leaderboard, sind
#     also danach ausgewaehlt, dass sie bereits gewonnen haben. Ein Teil jeder
#     gemessenen Edge ist Rueckschau. Das ist ein EIGENER Abschlag und gehoert
#     nicht in dieselbe Zahl wie das Messrauschen.
# Empirisch aus data/analysis_biweekly.json (70 gemessene Wallets, 14.09.2026):
# Streuung der wahren Edge zwischen Wallets rund 5-8c, Messrauschen je Trade
# 12-50c je nach Markttyp. Die alten 800 entsprachen einem Rauschen/Skill-
# Verhaeltnis von etwa 800 -- die Daten stuetzen 4 bis 50.
V_TRUE = 25.0          # angenommene Varianz der ECHTEN Edge zwischen Wallets, in c^2 (SD 5c)
SELECTION_HAIRCUT = 0.6  # Abschlag fuer Leaderboard-Selektion; 1.0 = kein Abschlag
EDGE_CAP = 10.0        # vorher 6.0 -- sonst laufen die starken Wallets alle am Deckel zusammen
# --- Liveness ---------------------------------------------------------------
# Eine gemessene Edge zaehlt nur, wenn das Wallet noch handelt. Ohne diese
# beiden Schranken landen Wallets, die vor Monaten aufgehoert haben, mit ihrer
# historischen CLV in "active" und blockieren dort Plaetze (Stand 10.09.2026:
# 8 von 13 aktiven Wallets ohne einen einzigen Trade in 30 Tagen).
CLV_LOOKBACK_DAYS = 180        # war 120; mehr Einstiege je Wallet -> kleinerer Standardfehler
                               # -> weniger Schrumpfung -> mehr Wallets ueber der Gebuehrengrenze
ACTIVE_MIN_TRADES_30D = 10     # darunter: dormant -> watch, nie active
MAX_WATCH = 80

session_token_budget = {"used": 0}
run_start_ts = time.time()


def out_of_time():
    return (time.time() - run_start_ts) > DEADLINE_MIN * 60


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
    # 2026-09-14: Trichter verbreitert. Der Endpoint deckelt jede Antwort bei 50
    # Zeilen, mehr Kandidaten gibt es also nur ueber mehr SCHNITTE, nicht ueber
    # ein groesseres limit=. Neu wird jede Kategorie auch nach VOL und in jeder
    # Periode gescannt, nicht nur nach PNL -- ein Sharp mit hohem Umsatz und
    # mittlerem Gewinn taucht in der PNL-Rangliste nie auf, hat aber oft die
    # sauberere CLV als ein einmaliger Grosstreffer.
    scans = []
    for cat in CATEGORIES:
        for period in PERIODS:
            scans.append((period, cat, 50, "PNL"))
            scans.append((period, cat, 50, "VOL"))
    for period in PERIODS:
        scans.append((period, None, 50, "PNL"))
        scans.append((period, None, 50, "VOL"))

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
            if pnl < MIN_PNL or roi < MIN_ROI:
                continue
            candidates[addr] = {"name": uname, "pnl": pnl, "vol": vol}
        # Frueher wurde hier bei Erreichen von MAX_CANDIDATES die GESAMTE
        # Scan-Schleife abgebrochen. Damit liefen die spaeteren, spezifischeren
        # Schnitte nie -- ausgerechnet die, die Fach-Sharps finden sollen.
        # Jetzt wird nur noch dieser Schnitt beendet.
        if len(candidates) >= MAX_CANDIDATES or out_of_time():
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
    if session_token_budget["used"] >= MAX_TOKENS or out_of_time():
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
            if not (0.03 <= price <= 0.97):   # war 0.05-0.92; enger Band warf Einstiege weg
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

    # Nicht alle Einstiege messen. Der Standardfehler faellt mit 1/sqrt(n) --
    # von 150 auf 500 Messpunkte gewinnt man rund 40 % SE, kostet aber das
    # Dreifache an Token-Abrufen. Gleichmaessig ueber den Zeitraum ausduennen
    # (nicht die juengsten nehmen), damit die Stichprobe unverzerrt bleibt.
    if len(entries) > MAX_ENTRIES_PER_WALLET:
        step = len(entries) / MAX_ENTRIES_PER_WALLET
        entries = [entries[int(i * step)] for i in range(MAX_ENTRIES_PER_WALLET)]

    weighted_sum = 0.0
    weight_total = 0.0
    used = 0
    weight_sq_total = 0.0
    clv_values = []      # (CLV, Groesse) je Einstieg, fuer den Standardfehler
    resolved_hits = 0    # Einstiege, deren Markt nach 7 Tagen schon aufgeloest war
    for e in entries:
        hist = get_price_history(e["asset"], price_cache)
        if not hist:
            continue
        p7 = price_at_or_after(hist, e["ts"] + 7 * 86400)
        if p7 is None:
            continue
        p7f = float(p7)
        clv = (p7f - e["price"]) * 100.0
        if p7f >= 0.99 or p7f <= 0.01:
            resolved_hits += 1
        weighted_sum += clv * e["size"]
        weight_total += e["size"]
        weight_sq_total += e["size"] ** 2
        clv_values.append((clv, e["size"]))
        used += 1

    flags = []
    if len(activity) >= 5000:
        flags.append("very_high_frequency")
    ts_list = [tr.get("timestamp") for tr in activity if tr.get("timestamp")]
    if len(ts_list) >= 2:
        span_days = (max(ts_list) - min(ts_list)) / 86400
        if span_days and (len(activity) / span_days) > 50:
            flags.append("possible_hft_or_market_maker")

    base["n_entries_used"] = used
    base["n_entries_available"] = len(entries)

    if weight_total <= 0 or used < MIN_TRADES_FOR_SIGNAL:
        return {**base, "sample": "insufficient", "clv7_cents": None, "base_edge": None, "flags": flags}

    clv7 = weighted_sum / weight_total

    # Standardfehler des Wallet-Mittels aus seinen EIGENEN Einzel-CLVs.
    # WICHTIG: clv7 ist ein GROESSENGEWICHTETES Mittel. Eine ungewichtete
    # Varianz durch n zu teilen waere falsch -- ein Wallet mit einem Trade zu
    # 100'000 $ und 400 Trades zu 10 $ haette rechnerisch n = 401, effektiv
    # aber knapp 1. Der Standardfehler waere dann um Groessenordnungen zu
    # klein, die Schrumpfung entsprechend zu schwach und die Edge frei
    # erfunden. Korrekt ist die effektive Stichprobengroesse nach Kish:
    #     n_eff = (Summe w)^2 / Summe w^2
    # zusammen mit der ebenfalls gewichteten Varianz.
    var_entry = sum(w * (c - clv7) ** 2 for c, w in clv_values) / weight_total
    n_eff = (weight_total ** 2 / weight_sq_total) if weight_sq_total > 0 else 1.0
    se2 = var_entry / max(n_eff, 1.0)

    shrink_factor = V_TRUE / (V_TRUE + se2)      # Empirical Bayes
    shrunk = clv7 * shrink_factor * SELECTION_HAIRCUT
    base_edge = round(min(EDGE_CAP, max(0.0, shrunk)), 1)
    legacy_edge = round(min(6.0, max(0.0, clv7 * (used / (used + 800)))), 1)

    return {**base, "sample": "ok", "clv7_cents": round(clv7, 2),
            "clv_sd_per_entry": round(var_entry ** 0.5, 1),
            "clv_se": round(se2 ** 0.5, 2),
            "n_eff": round(n_eff, 1),
            "shrink_factor": round(shrink_factor, 3),
            "resolved_share": round(resolved_hits / used, 2),
            "base_edge": base_edge, "base_edge_legacy_k800": legacy_edge,
            "flags": flags}


def entry_fields(r):
    return {
        "name": r["name"], "address": r["address"],
        "base_edge": r.get("base_edge") or 0.0,
        "clv7_cents": r.get("clv7_cents"),
        "clv_sd_per_entry": r.get("clv_sd_per_entry"),
        "shrink_factor": r.get("shrink_factor"),
        "resolved_share": r.get("resolved_share"),
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
    # Reihenfolge zaehlt: Laeuft das Zeit- oder Token-Budget aus, sollen die
    # bereits verfolgten Wallets gemessen sein, nicht ein zufaelliger Rest.
    tracked = set()
    if prev_watchlist:
        for tier in ("active", "watch"):
            for w in prev_watchlist.get(tier, []):
                tracked.add(str(w.get("address", "")).lower())
    ordered = sorted(candidates.items(),
                     key=lambda kv: (0 if kv[0] in tracked else 1, -(kv[1].get("pnl") or 0)))
    for addr, meta in ordered:
        if out_of_time():
            print(f"ZEITBUDGET erreicht - {len(ordered) - len(results)} Wallets nicht gemessen")
            break
        try:
            results.append(analyze_wallet(addr, meta, price_cache))
        except Exception as e:
            results.append({"address": addr, "name": meta.get("name"), "sample": "error", "error": str(e)})

    results.sort(key=lambda r: (r.get("base_edge") is None, -(r.get("base_edge") or 0)))

    analysis_out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "n_candidates": len(candidates),
        "tokens_fetched": session_token_budget["used"],
        "runtime_min": round((time.time() - run_start_ts) / 60, 1),
        "hit_deadline": out_of_time(),
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

    # Wirkung der neuen Schrumpfung sichtbar machen: alt gegen neu, je Wallet.
    measured = [r for r in results if r.get("sample") == "ok"]
    if measured:
        print("\n--- Schrumpfung alt (k=800) gegen neu (Empirical Bayes x Selektionsabschlag) ---")
        print(f"{'Wallet':<24} {'CLV roh':>8} {'SD/Trade':>9} {'n_eff':>7} {'Faktor':>7} {'aufgel.':>8} {'alt':>5} {'neu':>5}")
        for r in sorted(measured, key=lambda x: -(x.get("base_edge") or 0))[:25]:
            print(f"{(r.get('name') or '')[:24]:<24} "
                  f"{r.get('clv7_cents', 0):>7.2f}c "
                  f"{r.get('clv_sd_per_entry', 0):>8.1f}c "
                  f"{r.get('n_eff', 0):>7.1f} "
                  f"{r.get('shrink_factor', 0):>7.3f} "
                  f"{r.get('resolved_share', 0):>7.0%} "
                  f"{r.get('base_edge_legacy_k800', 0):>5.1f} "
                  f"{r.get('base_edge', 0):>5.1f}")
        n_old = sum(1 for r in measured if (r.get("base_edge_legacy_k800") or 0) >= ACTIVE_EDGE_MIN
                    and (r.get("trades_30d") or 0) >= ACTIVE_MIN_TRADES_30D and not r.get("flags"))
        n_new = sum(1 for r in measured if (r.get("base_edge") or 0) >= ACTIVE_EDGE_MIN
                    and (r.get("trades_30d") or 0) >= ACTIVE_MIN_TRADES_30D and not r.get("flags"))
        print(f"\ntragfaehige Wallets (Edge >= {ACTIVE_EDGE_MIN}, aktiv, ungeflaggt): alt {n_old} -> neu {n_new}")
        print("ACHTUNG: Die Edges liegen jetzt auf einer anderen Skala. Die Schwellen im")
        print("Tagesbrief (Netto-Edge >= 2) muessen im selben Zug mitskaliert werden,")
        print("sonst wird p_hat = Kurs + Edge systematisch zu hoch und Kelly setzt zu gross.")


if __name__ == "__main__":
    main()
