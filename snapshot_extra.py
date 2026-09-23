#!/usr/bin/env python3
"""
snapshot_extra.py — Zusatz-Snapshot fuer den Polymarket-Tagesbrief.

Laeuft NEBEN pull.py auf Slavis Mac (der Mac erreicht die Polymarket-API direkt,
die Cowork-Cloud nicht). Schreibt drei Dateien nach <repo>/data/ und committet sie,
damit der Tagesbrief sie ueber raw.githubusercontent.com lesen kann.

Erzeugt:
  data/account_state.json   Kontostand-Rohdaten (Positionen, geschlossene, Aktivitaet, Cashflows)
  data/books.json           Orderbuch-Spitzen (Top 5 Bid/Ask) der relevanten Maerkte
  data/odds.json            Kommende Sport-/Esports-Maerkte + optional Buchmacherquoten

Aufruf:
  python3 snapshot_extra.py --repo ~/Projekte/polymarket/polymarket-brief
  python3 snapshot_extra.py --repo ... --push          # committet und pusht
  ODDS_API_KEY=... python3 snapshot_extra.py --repo ...  # mit Quoten (the-odds-api.com)

Abhaengigkeit: requests
"""

import argparse, json, os, subprocess, sys, time
from datetime import datetime, timezone, timedelta

import requests

WALLET = "0x0fd4d56894d6e81cb9b8348c772c5eaa4dd2e72f"
DATA_API = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "polymarket-brief-snapshot/1.0"
TIMEOUT = 25

# Maerkte, in denen ein Sharp mindestens so viel bewegt hat, kommen ins Orderbuch
MIN_SHARP_USDC = 250.0
# Wie weit zurueck Sharp-Kaeufe fuer die Orderbuch-Auswahl zaehlen
SHARP_LOOKBACK_H = 48
# Wie weit voraus Sportmaerkte gesammelt werden
SPORTS_LOOKAHEAD_H = 72


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.replace(microsecond=0).isoformat()


def get(url, params=None, tries=3):
    """GET mit kurzem Backoff. Gibt (json, fehler) zurueck - wirft nicht."""
    last = None
    for i in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json(), None
            last = f"HTTP {r.status_code}"
        except Exception as e:                      # Netz, Timeout, JSON
            last = f"{type(e).__name__}: {e}"
        time.sleep(1.5 * (i + 1))
    return None, last


def paged_activity(user, limit=500, page=100, kind="TRADE"):
    """Aktivitaet paginiert holen, bis limit oder Ende. Meldet Abbruch ehrlich."""
    out, offset, truncated, err = [], 0, False, None
    while len(out) < limit:
        batch, e = get(f"{DATA_API}/activity",
                       {"user": user, "limit": page, "offset": offset, "type": kind})
        if e:
            err = e
            break
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page:
            break
        offset += page
    else:
        truncated = True
    return out[:limit], truncated, err


# ---------------------------------------------------------------- Kontostand

def build_account_state():
    """Rohdaten des eigenen Wallets plus abgeleitete Cashflows.

    Bewusst KEINE Bewertung und KEIN geschaetzter Cash-Stand: der Brief soll
    Annahmen selbst kennzeichnen, nicht hier vorgerechnet bekommen.
    """
    state = {"wallet": WALLET, "fetched_at_utc": iso(now_utc()), "errors": []}

    for key, url, params in (
        ("positions", f"{DATA_API}/positions",
         {"user": WALLET, "sizeThreshold": 0.1, "limit": 200}),
        ("closed_positions", f"{DATA_API}/closed-positions",
         {"user": WALLET, "limit": 200}),
        ("value", f"{DATA_API}/value", {"user": WALLET}),
    ):
        data, err = get(url, params)
        state[key] = data if data is not None else []
        if err:
            state["errors"].append({"source": key, "error": err})

    acts, truncated, err = paged_activity(WALLET, limit=2000)
    state["activity"] = acts
    state["activity_truncated"] = truncated
    if err:
        state["errors"].append({"source": "activity", "error": err})

    # Cashflows aus der Aktivitaet - reine Summierung, keine Annahme ueber Einlagen
    flows = {"buys_usdc": 0.0, "sells_usdc": 0.0, "redeem_usdc": 0.0,
             "n_trades": 0, "n_redeems": 0}
    for a in acts:
        t = (a.get("type") or "").upper()
        usd = float(a.get("usdcSize") or 0)
        if t == "TRADE":
            flows["n_trades"] += 1
            if (a.get("side") or "").upper() == "BUY":
                flows["buys_usdc"] += usd
            else:
                flows["sells_usdc"] += usd
        elif t in ("REDEEM", "REWARD", "CONVERSION"):
            flows["n_redeems"] += 1
            flows["redeem_usdc"] += usd
    state["cashflows"] = {k: (round(v, 6) if isinstance(v, float) else v)
                          for k, v in flows.items()}
    state["cashflows_note"] = (
        "Nur Handels- und Redemption-Fluesse aus der Aktivitaet. Einzahlungen und "
        "Auszahlungen sind hier NICHT enthalten - der Brief muss sie getrennt belegen.")
    return state


# ---------------------------------------------------------------- Orderbuecher

def collect_book_tokens(repo_data):
    """Welche Token brauchen ein Orderbuch?

    1. alle eigenen offenen Positionen
    2. Maerkte, in denen ein aktives Watchlist-Wallet zuletzt gross gekauft hat
    """
    wanted, why = {}, {}

    acct = repo_data.get("account_state") or {}
    for p in acct.get("positions") or []:
        a = p.get("asset")
        if a:
            wanted[a] = p.get("conditionId")
            why.setdefault(a, []).append("eigene Position")

    wl = repo_data.get("watchlist") or {}
    cutoff = time.time() - SHARP_LOOKBACK_H * 3600
    for entry in wl.get("active") or []:
        scopes = entry.get("active_scopes") or []
        if not scopes:
            continue
        addr = entry.get("address")
        acts, _, err = paged_activity(addr, limit=300)
        if err:
            continue
        for a in acts:
            if (a.get("side") or "").upper() != "BUY":
                continue
            if float(a.get("usdcSize") or 0) < MIN_SHARP_USDC:
                continue
            if float(a.get("timestamp") or 0) < cutoff:
                continue
            tok = a.get("asset")
            if not tok:
                continue
            wanted[tok] = a.get("conditionId")
            why.setdefault(tok, []).append(
                f"{entry.get('name')} {a.get('outcome')} @{a.get('price')} "
                f"{round(float(a.get('usdcSize') or 0))} USDC")
        time.sleep(0.2)
    return wanted, why


def build_books(repo_data):
    tokens, why = collect_book_tokens(repo_data)
    out = {"fetched_at_utc": iso(now_utc()), "n_tokens": len(tokens),
           "min_sharp_usdc": MIN_SHARP_USDC, "lookback_hours": SHARP_LOOKBACK_H,
           "books": [], "errors": []}

    for tok, cond in tokens.items():
        book, err = get(f"{CLOB}/book", {"token_id": tok})
        if err or not book:
            out["errors"].append({"token": tok, "error": err or "leer"})
            continue

        def top(side, best_is_max):
            rows = [(float(x["price"]), float(x["size"])) for x in (book.get(side) or [])]
            rows.sort(key=lambda r: r[0], reverse=best_is_max)
            return [{"price": p, "size": s} for p, s in rows[:5]]

        bids = top("bids", True)      # bester Bid = hoechster
        asks = top("asks", False)     # bester Ask = niedrigster
        out["books"].append({
            "token_id": tok,
            "conditionId": cond,
            "book_timestamp_ms": book.get("timestamp"),
            "best_bid": bids[0]["price"] if bids else None,
            "best_ask": asks[0]["price"] if asks else None,
            "spread": round(asks[0]["price"] - bids[0]["price"], 4) if (bids and asks) else None,
            "bids": bids,
            "asks": asks,
            "reason": why.get(tok, []),
        })
        time.sleep(0.15)
    return out


# ---------------------------------------------------------------- Sport/Quoten

SPORT_HINTS = ("nfl-", "nba-", "mlb-", "nhl-", "cfb-", "cs2-", "lol-", "dota2-",
               "val-", "unl-", "epl-", "ucl-", "laliga-", "seriea-", "bundesliga-",
               "atp-", "wta-", "f1-")


def build_odds():
    out = {"fetched_at_utc": iso(now_utc()), "markets": [],
           "bookmaker_odds": [], "errors": [], "notes": []}

    lo = now_utc()
    hi = lo + timedelta(hours=SPORTS_LOOKAHEAD_H)
    data, err = get(f"{GAMMA}/markets", {
        "active": "true", "closed": "false",
        "end_date_min": iso(lo), "end_date_max": iso(hi),
        "order": "liquidityNum", "ascending": "false", "limit": 200})
    if err:
        out["errors"].append({"source": "gamma", "error": err})
        return out

    for m in data or []:
        slug = (m.get("slug") or "")
        if not any(h in slug for h in SPORT_HINTS):
            continue
        out["markets"].append({
            "question": m.get("question"), "slug": slug,
            "conditionId": m.get("conditionId"),
            "clobTokenIds": m.get("clobTokenIds"),
            "outcomes": m.get("outcomes"), "outcomePrices": m.get("outcomePrices"),
            "bestBid": m.get("bestBid"), "bestAsk": m.get("bestAsk"),
            "spread": m.get("spread"), "volume24hr": m.get("volume24hr"),
            "liquidityNum": m.get("liquidityNum"),
            "gameStartTime": m.get("gameStartTime"), "endDate": m.get("endDate"),
            "updatedAt": m.get("updatedAt"),
        })

    key = os.environ.get("ODDS_API_KEY")
    if not key:
        out["notes"].append(
            "ODDS_API_KEY nicht gesetzt - keine Buchmacherquoten. Ohne unabhaengige "
            "Vergleichsquoten kann der Brief kein Sport-p-Dach herleiten.")
        return out

    # the-odds-api.com, Gratis-Stufe ~500 Abrufe/Monat.
    # Esports-Abdeckung ist dort duenn - das wird hier ehrlich vermerkt, nicht kaschiert.
    for sport in ("soccer_uefa_nations_league", "americanfootball_nfl",
                  "basketball_nba", "icehockey_nhl", "baseball_mlb"):
        odds, e = get("https://api.the-odds-api.com/v4/sports/%s/odds" % sport,
                      {"apiKey": key, "regions": "eu,uk", "markets": "h2h",
                       "oddsFormat": "decimal"}, tries=2)
        if e:
            out["errors"].append({"source": sport, "error": e})
            continue
        for ev in odds or []:
            books = []
            for b in ev.get("bookmakers") or []:
                for mk in b.get("markets") or []:
                    if mk.get("key") != "h2h":
                        continue
                    books.append({"bookmaker": b.get("key"),
                                  "last_update": b.get("last_update"),
                                  "outcomes": [{"name": o.get("name"),
                                                "price": o.get("price")}
                                               for o in mk.get("outcomes") or []]})
            if books:
                out["bookmaker_odds"].append({
                    "sport": sport, "commence_time": ev.get("commence_time"),
                    "home_team": ev.get("home_team"), "away_team": ev.get("away_team"),
                    "bookmakers": books})
        time.sleep(0.3)

    out["notes"].append(
        "Quoten sind Rohwerte. Normalisierung (q_i = 1/Quote_i, p_i = q_i/Summe q) "
        "macht der Brief - und muss pruefen, ob die Buecher wirklich unabhaengig sind "
        "(1xBet, Melbet, 22bet, Megapari, Paripesa, 20Bet gehoeren zusammen).")
    return out


# ---------------------------------------------------------------- Ablauf

def write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)
    print("  geschrieben: %s (%.1f KB)" % (path, os.path.getsize(path) / 1024))


def read_if_there(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="Pfad zum Repo polymarket-brief")
    ap.add_argument("--push", action="store_true", help="committen und pushen")
    ap.add_argument("--skip", default="", help="Komma-Liste: account,books,odds")
    args = ap.parse_args()

    repo = os.path.expanduser(args.repo)
    ddir = os.path.join(repo, "data")
    if not os.path.isdir(ddir):
        sys.exit("data/ nicht gefunden unter %s - falscher --repo Pfad?" % repo)
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    repo_data = {"watchlist": read_if_there(os.path.join(ddir, "watchlist.json"))}
    written = []

    if "account" not in skip:
        print("Kontostand ...")
        st = build_account_state()
        write(os.path.join(ddir, "account_state.json"), st)
        repo_data["account_state"] = st
        written.append("data/account_state.json")
    else:
        repo_data["account_state"] = read_if_there(os.path.join(ddir, "account_state.json"))

    if "books" not in skip:
        print("Orderbuecher ...")
        write(os.path.join(ddir, "books.json"), build_books(repo_data))
        written.append("data/books.json")

    if "odds" not in skip:
        print("Sportmaerkte und Quoten ...")
        write(os.path.join(ddir, "odds.json"), build_odds())
        written.append("data/odds.json")

    # Track Record: der Brief liefert die Datei, dieses Script traegt sie nur mit.
    tr = os.path.join(ddir, "track_record.json")
    if os.path.exists(tr):
        written.append("data/track_record.json")
        print("  track_record.json vorhanden - wird mitcommittet")
    else:
        print("  HINWEIS: data/track_record.json fehlt. Die Datei aus dem Tagesbrief "
              "hierher legen, dann ueberlebt der Track Record jeden Lauf.")

    if args.push and written:
        msg = "snapshot_extra %s" % iso(now_utc())
        try:
            subprocess.run(["git", "-C", repo, "add"] + written, check=True)
            r = subprocess.run(["git", "-C", repo, "diff", "--cached", "--quiet"])
            if r.returncode == 0:
                print("nichts veraendert - kein Commit")
            else:
                subprocess.run(["git", "-C", repo, "commit", "-m", msg], check=True)
                subprocess.run(["git", "-C", repo, "push"], check=True)
                print("gepusht.")
        except subprocess.CalledProcessError as e:
            print("git fehlgeschlagen: %s" % e, file=sys.stderr)
            sys.exit(1)

    print("fertig.")


if __name__ == "__main__":
    main()
