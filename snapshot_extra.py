#!/usr/bin/env python3
"""
snapshot_extra.py -- Zusatz-Snapshot fuer den Polymarket-Tagesbrief.

Laeuft als Schritt im GitHub-Workflow "Daily Polymarket Pull", direkt nach
pull.py. Schreibt drei Dateien nach data/. Das Committen uebernimmt der
bestehende Schritt "Commit and push data" -- dieses Script fasst git nicht an.

Warum es existiert: Die Cowork-Cloud, in der der Tagesbrief gebaut wird,
erreicht die Polymarket-API nicht (Egress-Policy, CONNECT 403). Der
GitHub-Runner erreicht sie. Also holt dieser Schritt die Daten, die der Brief
sonst muehsam einzeln nachladen muesste -- oder gar nicht bekommt.

Erzeugt:
  data/account_state.json  Kontostand-Rohdaten des eigenen Wallets
  data/books.json          Orderbuch-Spitzen der relevanten Maerkte
  data/odds.json           Kommende Sport-/Esports-Maerkte + optional Quoten

Nur Standardbibliothek -- keine zusaetzliche Installation im Workflow noetig.
Das Script beendet sich IMMER mit 0: ein Teilausfall darf den Daily Pull nicht
rot faerben. Fehler landen als "errors" in der jeweiligen Datei.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

WALLET = "0x0fd4d56894d6e81cb9b8348c772c5eaa4dd2e72f"
DATA_API = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

TIMEOUT = 25
MIN_SHARP_USDC = 250.0      # ab dieser Kaufgroesse ist ein Sharp-Trade relevant
SHARP_LOOKBACK_H = 48       # so weit zurueck zaehlen Sharp-Kaeufe
SPORTS_LOOKAHEAD_H = 72     # so weit voraus werden Sportmaerkte gesammelt
MAX_BOOK_TOKENS = 80        # Deckel, damit der stuendliche Lauf kurz bleibt
ODDS_HOURS = {5, 11}        # Quoten nur zu diesen UTC-Stunden (Gratis-Kontingent)


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get(url, params=None, tries=3):
    """GET mit Backoff. Gibt (daten, fehlertext) zurueck und wirft nie."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "polymarket-brief-snapshot/2.0",
                              "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code in (400, 401, 403, 404):
                break                      # kein Retry bei klaren Absagen
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        time.sleep(1.5 * (i + 1))
    return None, last


def paged_activity(user, limit=500, page=100):
    """Aktivitaet paginiert. Meldet ehrlich, wenn am Deckel abgeschnitten."""
    out, offset, err = [], 0, None
    while len(out) < limit:
        batch, e = get("%s/activity" % DATA_API,
                       {"user": user, "limit": page, "offset": offset,
                        "type": "TRADE"})
        if e:
            err = e
            break
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return out[:limit], len(out) >= limit, err


# ------------------------------------------------------------------ Konto

def build_account_state():
    """Rohdaten des eigenen Wallets plus Summen der Handels-Cashflows.

    Bewusst ohne Bewertung und ohne geschaetzten Cash-Stand: der Brief soll
    seine Annahmen selbst kennzeichnen, nicht hier vorgerechnet bekommen.
    """
    st = {"wallet": WALLET, "fetched_at_utc": iso(now_utc()), "errors": []}

    for key, url, params in (
        ("positions", "%s/positions" % DATA_API,
         {"user": WALLET, "sizeThreshold": 0.1, "limit": 200}),
        ("closed_positions", "%s/closed-positions" % DATA_API,
         {"user": WALLET, "limit": 200}),
        ("value", "%s/value" % DATA_API, {"user": WALLET}),
    ):
        data, err = get(url, params)
        st[key] = data if data is not None else []
        if err:
            st["errors"].append({"source": key, "error": err})

    acts, truncated, err = paged_activity(WALLET, limit=2000)
    st["activity"] = acts
    st["activity_truncated"] = truncated
    if err:
        st["errors"].append({"source": "activity", "error": err})

    f = {"buys_usdc": 0.0, "sells_usdc": 0.0, "redeem_usdc": 0.0,
         "n_trades": 0, "n_redeems": 0}
    for a in acts:
        t = (a.get("type") or "").upper()
        usd = float(a.get("usdcSize") or 0)
        if t == "TRADE":
            f["n_trades"] += 1
            if (a.get("side") or "").upper() == "BUY":
                f["buys_usdc"] += usd
            else:
                f["sells_usdc"] += usd
        elif t in ("REDEEM", "REWARD", "CONVERSION"):
            f["n_redeems"] += 1
            f["redeem_usdc"] += usd
    st["cashflows"] = {k: (round(v, 6) if isinstance(v, float) else v)
                       for k, v in f.items()}
    st["cashflows_note"] = (
        "Nur Handels- und Redemption-Fluesse. Ein- und Auszahlungen sind NICHT "
        "enthalten -- die muss der Brief getrennt belegen.")
    return st


# ------------------------------------------------------------- Orderbuecher

def collect_book_tokens(watchlist, account):
    """Welche Token brauchen ein Orderbuch?

    1. jede eigene offene Position
    2. Maerkte, in denen ein aktives Watchlist-Wallet zuletzt gross gekauft hat
    """
    wanted, why = {}, {}

    for p in (account or {}).get("positions") or []:
        tok = p.get("asset")
        if tok:
            wanted[tok] = p.get("conditionId")
            why.setdefault(tok, []).append("eigene Position: %s %s" % (
                (p.get("title") or "")[:60], p.get("outcome")))

    cutoff = time.time() - SHARP_LOOKBACK_H * 3600
    for entry in (watchlist or {}).get("active") or []:
        if not (entry.get("active_scopes") or []):
            continue
        acts, _, err = paged_activity(entry.get("address"), limit=300)
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
            why.setdefault(tok, []).append("%s %s @%s %s USDC" % (
                entry.get("name"), a.get("outcome"), a.get("price"),
                round(float(a.get("usdcSize") or 0))))
        time.sleep(0.2)
    return wanted, why


def build_books(watchlist, account):
    tokens, why = collect_book_tokens(watchlist, account)
    capped = len(tokens) > MAX_BOOK_TOKENS
    items = list(tokens.items())[:MAX_BOOK_TOKENS]

    out = {"fetched_at_utc": iso(now_utc()),
           "n_tokens": len(items), "capped_at": MAX_BOOK_TOKENS if capped else None,
           "min_sharp_usdc": MIN_SHARP_USDC, "lookback_hours": SHARP_LOOKBACK_H,
           "books": [], "errors": []}

    for tok, cond in items:
        book, err = get("%s/book" % CLOB, {"token_id": tok})
        if err or not book:
            out["errors"].append({"token": tok, "error": err or "leere Antwort"})
            continue

        def side(name, best_is_max):
            rows = []
            for x in (book.get(name) or []):
                try:
                    rows.append((float(x["price"]), float(x["size"])))
                except (KeyError, TypeError, ValueError):
                    continue
            rows.sort(key=lambda r: r[0], reverse=best_is_max)
            return [{"price": p, "size": s} for p, s in rows[:5]]

        bids = side("bids", True)    # bester Bid = hoechster Preis
        asks = side("asks", False)   # bester Ask = niedrigster Preis
        out["books"].append({
            "token_id": tok,
            "conditionId": cond,
            "book_timestamp_ms": book.get("timestamp"),
            "best_bid": bids[0]["price"] if bids else None,
            "best_ask": asks[0]["price"] if asks else None,
            "spread": (round(asks[0]["price"] - bids[0]["price"], 4)
                       if (bids and asks) else None),
            "bids": bids, "asks": asks,
            "reason": why.get(tok, []),
        })
        time.sleep(0.12)
    return out


# --------------------------------------------------------- Sport und Quoten

SPORT_HINTS = ("nfl-", "nba-", "mlb-", "nhl-", "cfb-", "cbb-", "cs2-", "lol-",
               "dota2-", "val-", "r6-", "ow2-", "unl-", "ucl-", "uel-", "epl-",
               "laliga-", "seriea-", "bundesliga-", "ligue1-", "mls-", "atp-",
               "wta-", "f1-", "ufc-", "boxing-")


def build_odds(force_odds=False):
    out = {"fetched_at_utc": iso(now_utc()), "markets": [],
           "bookmaker_odds": [], "errors": [], "notes": []}

    lo, hi = now_utc(), now_utc() + timedelta(hours=SPORTS_LOOKAHEAD_H)
    data, err = get("%s/markets" % GAMMA, {
        "active": "true", "closed": "false",
        "end_date_min": iso(lo), "end_date_max": iso(hi),
        "order": "liquidityNum", "ascending": "false", "limit": 200})
    if err:
        out["errors"].append({"source": "gamma", "error": err})
        return out

    for m in data or []:
        slug = m.get("slug") or ""
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
    out["notes"].append(
        "Preise in dieser Datei stammen aus Gamma und koennen veraltet sein "
        "(updatedAt pruefen). Massgeblich ist das Orderbuch in books.json.")

    key = os.environ.get("ODDS_API_KEY")
    if not key:
        out["notes"].append(
            "ODDS_API_KEY nicht gesetzt -- keine Buchmacherquoten. Ohne "
            "unabhaengige Vergleichsquoten kann der Brief kein Sport-p-Dach "
            "herleiten und gibt korrekterweise keinen Sporttipp.")
        return out

    if not force_odds and now_utc().hour not in ODDS_HOURS:
        out["notes"].append(
            "Quotenabruf uebersprungen: laeuft nur zu den UTC-Stunden %s, damit "
            "das Gratis-Kontingent reicht." % sorted(ODDS_HOURS))
        return out

    # the-odds-api.com: Gratis-Stufe rund 500 Abrufe/Monat.
    # Esports-Abdeckung ist dort duenn -- das wird vermerkt, nicht kaschiert.
    for sport in ("soccer_uefa_nations_league", "soccer_uefa_champs_league",
                  "soccer_epl", "americanfootball_nfl", "basketball_nba"):
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
                    books.append({
                        "bookmaker": b.get("key"),
                        "last_update": b.get("last_update"),
                        "outcomes": [{"name": o.get("name"), "price": o.get("price")}
                                     for o in mk.get("outcomes") or []]})
            if books:
                out["bookmaker_odds"].append({
                    "sport": sport, "commence_time": ev.get("commence_time"),
                    "home_team": ev.get("home_team"),
                    "away_team": ev.get("away_team"), "bookmakers": books})
        time.sleep(0.3)

    out["notes"].append(
        "Quoten sind Rohwerte. Die Normalisierung (q_i = 1/Quote_i, "
        "p_i = q_i/Summe q) macht der Brief -- und muss pruefen, ob die Buecher "
        "wirklich unabhaengig sind: 1xBet, Melbet, 22bet, Megapari, Paripesa "
        "und 20Bet gehoeren zusammen und zaehlen als EINE Quelle.")
    return out


# ------------------------------------------------------------------ Ablauf

def write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)
    print("  geschrieben: %s (%.1f KB)" % (path, os.path.getsize(path) / 1024.0))


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".", help="Repo-Wurzel (enthaelt data/)")
    ap.add_argument("--odds", action="store_true",
                    help="Quoten unabhaengig von der Uhrzeit abrufen")
    ap.add_argument("--skip", default="",
                    help="Komma-Liste: account,books,odds")
    args = ap.parse_args()

    ddir = os.path.join(os.path.expanduser(args.repo), "data")
    if not os.path.isdir(ddir):
        print("data/ nicht gefunden unter %s -- nichts zu tun." % args.repo,
              file=sys.stderr)
        return 0                                  # bewusst kein harter Fehler

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    watchlist = read_json(os.path.join(ddir, "watchlist.json"))
    account = None

    if "account" not in skip:
        print("Kontostand ...")
        try:
            account = build_account_state()
            write(os.path.join(ddir, "account_state.json"), account)
        except Exception as e:
            print("  account_state fehlgeschlagen: %s" % e, file=sys.stderr)
    if account is None:
        account = read_json(os.path.join(ddir, "account_state.json"))

    if "books" not in skip:
        print("Orderbuecher ...")
        try:
            write(os.path.join(ddir, "books.json"),
                  build_books(watchlist, account))
        except Exception as e:
            print("  books fehlgeschlagen: %s" % e, file=sys.stderr)

    if "odds" not in skip:
        print("Sportmaerkte und Quoten ...")
        try:
            write(os.path.join(ddir, "odds.json"), build_odds(args.odds))
        except Exception as e:
            print("  odds fehlgeschlagen: %s" % e, file=sys.stderr)

    if not os.path.exists(os.path.join(ddir, "track_record.json")):
        print("  Hinweis: data/track_record.json fehlt. Datei aus dem "
              "Tagesbrief einmal hochladen, dann wird sie mitcommittet.")

    print("fertig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
