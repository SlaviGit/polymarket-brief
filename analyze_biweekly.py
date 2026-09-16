#!/usr/bin/env python3
"""Biweekly wallet measurement. Drop-in command: python3 analyze_biweekly.py.

Uses only the Python standard library; retains data paths and legacy JSON keys.
Legacy base_edge/clv7_cents always describe the core seven-day measurement.
Sports closing-price measurements are never substituted for seven-day CLV.
The companion Tagesbrief instructions consume active_scopes and metrics.
Sports-only active wallets intentionally have legacy base_edge 0. No orders are placed.
Fixed shrinkage parameters are assumptions, not fitted empirical Bayes.

Betriebsaenderungen 2026-09-15 (drei Stellen, Messlogik unveraendert):

  1. market()-Aufrufe waren UNBUDGETIERT. Nur prices-history zaehlte gegen
     MAX_TOKENS. Jetzt eigenes Budget (MAX_MARKET_REQUESTS).
  2. Das Publish-Tor verlangte NULL Fehler. Jetzt: Fehlerquote unter
     MAX_FAILURE_RATE UND Abdeckung ueber MIN_ROSTER_COVERAGE.
  3. BudgetExceeded lief in denselben except-Zweig wie echte Fehler. Jetzt
     getrennt: ein Teillauf darf publizieren, wenn die Abdeckung reicht.

Korrekturen 2026-09-16 (Befund aus Lauf #10: 0 Fehler, aber nur 1 aktives
Wallet und roster_coverage 0.125):

  4. Client.market() fragte gamma ohne Filter ab. gamma filtert dort
     standardmaessig auf closed=false und liefert fuer jeden bereits
     aufgeloesten Markt []. Damit fiel jeder Einstieg in einen inzwischen
     aufgeloesten Markt unter skipped['missing_market'] -- also genau die
     Maerkte, um die es bei einer 7-Tage-Messung geht. Belegt am 16.09.2026:
       ?condition_ids=<BTC 100k 2024>              -> []
       ?condition_ids=<BTC 100k 2024>&closed=true  -> 1 Objekt, closedTime gesetzt
       ?condition_ids=<Ukraine 90 Tage>            -> []
       ?condition_ids=<Ukraine 90 Tage>&closed=true-> 1 Objekt, closedTime gesetzt
     Fuer Sport war das strukturell toedlich: benchmark() verlangt
     gameStartTime <= now, der Sportmarkt schliesst aber kurz nach Anpfiff --
     ein Sporteinstieg war damit immer 'immature' oder 'missing_market', und
     metrics.sports konnte MIN_EVENTS nie erreichen. Jetzt wird closed=true
     zuerst versucht, danach der Standardfilter.
  5. timestamp() scheiterte an gammas Zeitformat "2024-12-05 04:46:26+00"
     (Leerzeichen als Trenner, zweistelliger Offset). datetime.fromisoformat
     akzeptiert das erst ab Python 3.11; darunter still None, was zu
     'unknown_closure_time' bzw. 'sports_missing_start' fuehrte. Der Offset
     wird jetzt normalisiert. Im Workflow zusaetzlich python-version pinnen.
  6. roster_coverage() zaehlte nur sample == 'ok' und behandelte damit ein
     sauber gemessenes, aber unterbesetztes Wallet wie einen Abbruch. Jetzt
     zaehlt "ohne Fehler gemessen"; gegen ein stilles Leerraeumen der
     Watchlist schuetzt separat no_collapse.

  Offen und bewusst nicht geaendert: MAX_ENTRIES_PER_WALLET=50 zieht Fills
  zufaellig ueber 180 Tage, waehrend MIN_EVENTS Ereignisse zaehlt. Eine
  Stichprobe nach Ereignis statt nach Fill erreicht MIN_EVENTS mit weniger
  gamma-Aufrufen. Erst nach einem sauberen Lauf angehen.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import random
import statistics
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

AUSSORTIERT = ['11vsldfdsgfkjgos', 'Anjun', 'BigRabbit', 'BillyGating', 'ColomboHex', 'CongoleseBorat', 'Corlys', 'CryptoVagabond', 'Elenes', 'ExplosiveNinja', 'Flaznorp', 'GRIMDRIP', 'Hourglass', 'JnStrtPrdctnMrkts', 'Jsram', 'Len9311238', 'Mustafa0101', 'Mysaria', 'Railcool', 'RepTrump', 'SASGOLD', 'Talvez10', 'Trump2028', 'TwoEyes', 'VictorLudorum', 'WTSA', 'alwayslatetotheparty', 'b324u', 'balthazar', 'cigarettes', 'dddtrips', 'donthackme', 'e46m3', 'endlessFate', 'ferrariChampions2026', 'fishalive', 'frostrizz', 'gambamaster', 'gaven-willwin', 'godblessme2026', 'hansama231', 'korda77', 'matanovik', 'merod', 'mintblade', 'mustbethewater', 'northdrawer', 'pleaseplease123', 'quietparcel', 'robban888', 'sainttroplay', 'smallreceipt', 'sparklingwater123', 'suhail-frenz-account187', 'theowalcott', 'totoro3miyazaki', 'truthteller', 'wr0ngw4yb3tt0r', 'zofgkt1111']
DAY = 86400
MIN_EVENTS = 5

# Budgets. Jeder Aufruf kostet rund 0.6 s. Lauf #10 brauchte fuer 250 Wallets,
# 4042 Markt- und 1664 History-Aufrufe 58 Minuten. Nach Korrektur 4 kostet ein
# aufgeloester Markt einen Aufruf (closed=true trifft sofort), ein noch offener
# zwei; dafuer ueberleben deutlich mehr Einstiege bis zum History-Aufruf.
# Erwartung: rund 4800 Markt- und 5000 bis 8000 History-Aufrufe, etwa 150 Min.
MAX_CANDIDATES = 250
MAX_ENTRIES_PER_WALLET = 50
MAX_MARKET_REQUESTS = 12000
MAX_TOKENS = 12000
DEADLINE_MIN = 300

# Publish-Tor. Ein einzelnes fehlerhaftes Wallet darf ein Update nicht
# verhindern; ein Lauf, der den bisherigen Bestand nicht abgedeckt hat, schon.
MAX_FAILURE_RATE = 0.05
MIN_ROSTER_COVERAGE = 0.80
# Zweites Tor: ein Einbruch der qualifizierten Wallets ist ein Befund, kein
# Update. Haette Lauf #10 (1 aktiv gegen vorher 14) ebenfalls gestoppt -- aber
# mit der richtigen Begruendung statt "Abdeckung 12 %".
MIN_ACTIVE_RETENTION = 0.5
MIN_ACTIVE_ABS = 3

DATA_DIR = Path(__file__).resolve().parent / "data"
ACTIVE_EDGE_MIN = 1.5
MAX_ACTIVE, MAX_WATCH = 60, 80
PRIOR_VARIANCE, SELECTION_HAIRCUT, EDGE_CAP = 25.0, 0.6, 10.0

# gamma liefert Zeitstempel als "2024-12-05 04:46:26+00": Leerzeichen als
# Trenner und zweistelliger Offset. fromisoformat akzeptiert Letzteres erst ab
# Python 3.11.
_SHORT_OFFSET = re.compile(r'([+-]\d{2})$')


def number(x):
    try:
        x = float(x)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def timestamp(x):
    n = number(x)
    if n is not None:
        return n
    text = _SHORT_OFFSET.sub(r'\1:00', str(x).strip().replace('Z', '+00:00'))
    try:
        dt = datetime.fromisoformat(text)
        return dt.timestamp() if dt.tzinfo else None
    except ValueError:
        return None


def atomic_save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            json.dump(value, f, indent=2, allow_nan=False)
            f.write('\n')
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    tmp.replace(path)


class BudgetExceeded(RuntimeError):
    """Zeit- oder Aufrufbudget erschoepft. KEIN inhaltlicher Fehler --

    wird in run() getrennt behandelt, damit ein Teillauf noch publizieren darf."""


class Client:
    def __init__(self, budget=MAX_TOKENS, deadline_min=DEADLINE_MIN,
                 market_budget=MAX_MARKET_REQUESTS):
        self.started = time.monotonic()
        self.deadline = self.started + deadline_min*60
        self.budget = budget
        self.market_budget = market_budget
        self.history_calls = 0
        self.market_calls = 0
        self.markets = {}
        self.histories = {}
        self.errors = []

    def check_deadline(self):
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded('Wall-clock budget exhausted')

    def get(self, base, **params):
        url = base + '?' + urlencode(params)
        for attempt in range(3):
            self.check_deadline()
            try:
                time.sleep(0.25)
                with urlopen(Request(url, headers={'User-Agent': 'polymarket-brief/2.0'}), timeout=30) as r:
                    return json.load(r)
            except (OSError, ValueError) as exc:
                if attempt == 2:
                    self.errors.append({'url': url, 'error': str(exc)})
                    raise RuntimeError(f'API request failed: {url}') from exc
                time.sleep(2 ** attempt)

    def market(self, condition):
        if condition not in self.markets:
            found = None
            # gamma filtert ohne Parameter auf closed=false und liefert fuer
            # aufgeloeste Maerkte []. Die Kernmessung betrifft ueberwiegend
            # aufgeloeste Maerkte, daher closed=true zuerst: ein Aufruf fuer den
            # Normalfall, zwei nur fuer noch offene Maerkte.
            for extra in ({'closed': 'true'}, {}):
                if self.market_calls >= self.market_budget:
                    raise BudgetExceeded('Market request budget exhausted')
                self.market_calls += 1
                rows = self.get('https://gamma-api.polymarket.com/markets',
                                condition_ids=condition, **extra)
                if not isinstance(rows, list):
                    raise ValueError('Invalid market response')
                found = next((r for r in rows if r.get('conditionId') == condition), None)
                if found:
                    break
            self.markets[condition] = found
        return self.markets[condition]

    def history(self, token, target, gap, scope):
        # Keep the benchmark inside a bounded window; never use a later quote.
        key = (token, int(target), scope)
        if key not in self.histories:
            if self.history_calls >= self.budget:
                raise BudgetExceeded('History request budget exhausted')
            self.history_calls += 1
            response = self.get('https://clob.polymarket.com/prices-history', market=token,
                                startTs=int(target-max(gap, 3600)), endTs=int(target+60),
                                fidelity=1 if scope == 'sports' else 60)
            if not isinstance(response, dict) or not isinstance(response.get('history'), list):
                raise ValueError('Invalid history response')
            self.histories[key] = response['history']
        return self.histories[key]


def candidates(client, previous, limit, pages=2):
    found = {}
    rejected = {n.casefold() for n in AUSSORTIERT}
    def add(address, name, **meta):
        address = str(address).lower()
        if re.fullmatch(r'0x[0-9a-f]{40}', address) and str(name).casefold() not in rejected:
            found.setdefault(address, {'name': name, 'pnl': None, 'vol': None, **meta})
    for tier in ('active', 'watch', 'overflow'):
        for row in previous.get(tier, []):
            add(row.get('address'), row.get('name'))
    if len(found) > limit:
        raise ValueError('Candidate budget smaller than existing roster')
    # Scan every slice first, then allocate discovery slots across slices.
    # Thus SPORTS and later categories both get a chance under a small budget.
    pools = []
    # Round-robin categories before deeper pages: do not fill the budget with one category.
    for page in range(pages):
        for period in ('MONTH', 'WEEK', 'ALL'):
            for order in ('PNL', 'VOL'):
                for category in ('SPORTS', 'POLITICS', 'ECONOMICS', 'CULTURE', 'TECH', 'OVERALL'):
                    rows = client.get('https://data-api.polymarket.com/v1/leaderboard',
                                      timePeriod=period, orderBy=order, category=category,
                                      limit=50, offset=50*page)
                    if not isinstance(rows, list):
                        raise ValueError('Invalid leaderboard response')
                    pool = []
                    for row in rows:
                        pnl, vol = number(row.get('pnl')), number(row.get('vol'))
                        # Discovery only. Profit/turnover is not a skill estimate.
                        if pnl is not None and vol is not None and pnl > 0 and vol >= 10000:
                            pool.append((row.get('proxyWallet'), row.get('userName'), pnl, vol))
                    pools.append(pool)
    for rank in range(50):
        for pool in pools:
            if len(found) >= limit:
                return found
            if rank < len(pool):
                addr, name, pnl, vol = pool[rank]
                add(addr, name, pnl=pnl, vol=vol)
    return found


def activity(client, address, now, pages=10):
    seen, result = set(), []
    cutoff = now - 180*DAY
    for page in range(pages):
        rows = client.get('https://data-api.polymarket.com/activity', user=address,
                          type='TRADE', limit=500, offset=500*page,
                          sortBy='TIMESTAMP', sortDirection='DESC', end=int(now))
        if not isinstance(rows, list):
            raise ValueError('Invalid activity response')
        times = []
        for row in rows:
            ts = timestamp(row.get('timestamp'))
            if ts is None:
                continue
            times.append(ts)
            key = tuple(str(row.get(k)) for k in ('transactionHash', 'asset', 'side', 'timestamp', 'size', 'price'))
            if cutoff <= ts <= now and key not in seen and row.get('type') == 'TRADE':
                seen.add(key)
                result.append(row)
        if len(rows) < 500 or (times and min(times) < cutoff):
            return result, False
    return result, True


def price_before(history, target, max_gap):
    valid = [(number(p.get('t')), number(p.get('p'))) for p in history]
    valid = [(t, p) for t, p in valid if t is not None and p is not None
             and target-max_gap <= t <= target and 0 < p < 1]
    return max(valid, key=lambda x: x[0])[1] if valid else None


def benchmark(trade, market, now):
    ts = timestamp(trade.get('timestamp'))
    start = timestamp(market.get('gameStartTime'))
    tags = market.get('tags') or []
    sports = bool(start or market.get('sportsMarketType') or any(
        str(t.get('slug', '')).lower() == 'sports' for t in tags))
    if sports:
        if start is None:
            return None, 'sports_missing_start'
        target, gap, scope = start-60, 15*60, 'sports'
        closed = timestamp(market.get('closedTime'))
        if closed is not None and closed <= target:
            return None, 'closed_before_sports_benchmark'
        if start > now:
            return None, 'immature'
    else:
        target, gap, scope = ts+7*DAY, 2*3600, 'core'
        # Fail closed for historical closed markets with no timestamped closure.
        end = timestamp(market.get('endDate'))
        closed = timestamp(market.get('closedTime'))
        if market.get('closed') and closed is None:
            return None, 'unknown_closure_time'
        if any(t is not None and target >= t for t in (end, closed)):
            return None, 'ended_before_benchmark'
    if target > now or ts >= target:
        return None, 'immature_or_inplay'
    return (scope, target, gap), None


def summarize(events):
    # Within event: share-weighted. Across events: equal weight, including SE.
    values = [sum(c*s for c, s in fills)/sum(s for _, s in fills) for fills in events.values()]
    n = len(values)
    result = {'n_independent_events': n, 'n_entries_used': sum(map(len, events.values())), 'sample': 'insufficient'}
    if n < MIN_EVENTS:
        return result
    mean = statistics.mean(values)
    # Explicit modelling floor avoids certainty from identical repeated values.
    se2 = max(statistics.variance(values)/n, 1.0)
    factor = PRIOR_VARIANCE/(PRIOR_VARIANCE+se2)
    signed = mean*factor*SELECTION_HAIRCUT
    return {**result, 'sample': 'ok', 'mean_clv_cents': mean, 'clv_se': se2**0.5,
            'clv_sd_per_entry': statistics.stdev(values),
            'shrink_factor': factor, 'signed_edge': signed,
            'base_edge': round(min(EDGE_CAP, max(0, signed)), 2)}


def analyze_wallet(client, address, meta, now, pages=10):
    rows, truncated = activity(client, address, now, pages)
    all_rows = rows
    eligible = [r for r in rows if r.get('side') == 'BUY' and not r.get('isCombo')
                and number(r.get('size')) is not None and number(r['size']) > 0
                and number(r.get('price')) is not None and .03 <= number(r['price']) <= .97]
    available = len(eligible)
    # Reproducible fill sampling, not falsely described as uniform over time.
    eligible.sort(key=lambda r: (timestamp(r['timestamp']), str(r.get('transactionHash')), str(r.get('asset'))))
    sampled = available > MAX_ENTRIES_PER_WALLET
    rows = random.Random(address).sample(eligible, MAX_ENTRIES_PER_WALLET) if sampled else eligible
    groups = {'sports': defaultdict(list), 'core': defaultdict(list)}
    skipped = Counter()
    for row in rows:
        p, size = number(row.get('price')), number(row.get('size'))
        if row.get('isCombo') or row.get('side') != 'BUY' or p is None or not 0.03 <= p <= 0.97 or size is None or size <= 0:
            skipped['invalid_or_ineligible_entry'] += 1
            continue
        if not row.get('conditionId') or not row.get('asset'):
            skipped['missing_identifiers'] += 1
            continue
        market = client.market(row['conditionId'])
        if not market:
            skipped['missing_market'] += 1
            continue
        spec, reason = benchmark(row, market, now)
        if not spec:
            skipped[reason] += 1
            continue
        scope, target, gap = spec
        # Do not use pre-entry quotes as a post-entry measurement.
        quote = price_before(client.history(row['asset'], target, gap, scope), target,
                             min(gap, target-timestamp(row['timestamp'])))
        if quote is None or quote <= 0.01 or quote >= 0.99:
            skipped['missing_or_extreme_quote'] += 1
            continue
        events = market.get('events') or []
        event = (events[0].get('id') or events[0].get('slug')) if events else None
        event = event or row.get('eventSlug') or row['conditionId']
        groups[scope][event].append(((quote-p)*100, size))
    metrics = {scope: summarize(events) for scope, events in groups.items()}
    # Legacy consumers do not know market scopes. Preserve their seven-day
    # contract; never feed sports closing CLV into the old probability formula.
    scope = 'core' if metrics['core']['sample'] == 'ok' else None
    chosen = metrics.get(scope, {})
    sports_only = not scope and metrics['sports']['sample'] == 'ok'
    times = [timestamp(r['timestamp']) for r in all_rows]
    return {'address': address, **meta, 'sample': 'ok' if scope or sports_only else 'insufficient',
            'sports_only': sports_only, 'sports_metrics': metrics['sports'],
            'n_entries_available': available, 'entries_sampled': sampled,
            'n_trades_total': len(all_rows),
            'last_trade_age_days': round((now-max(times))/DAY, 1) if times else None,
            'clv_sd_per_entry': chosen.get('clv_sd_per_entry'),
            'clv_se': chosen.get('clv_se'), 'n_eff': chosen.get('n_independent_events', 0),
            'shrink_factor': chosen.get('shrink_factor'), 'resolved_share': 0.0,
            'base_edge_legacy_k800': round(min(6, max(0, chosen.get('mean_clv_cents', 0)*
                chosen.get('n_entries_used', 0)/(chosen.get('n_entries_used', 0)+800))), 1),
            'measured_at_utc': datetime.fromtimestamp(now, timezone.utc).isoformat(),
            'metrics': metrics, 'signal_scope': scope, 'base_edge': chosen.get('base_edge', 0.0),
            'signed_edge': chosen.get('signed_edge'),
            'clv7_cents': metrics['core'].get('mean_clv_cents'),
            'n_entries_used': metrics['core']['n_entries_used'],
            'n_entries_used_all_scopes': sum(m['n_entries_used'] for m in metrics.values()),
            'trades_30d': sum(timestamp(r['timestamp']) >= now-30*DAY for r in all_rows),
            'activity_truncated': truncated, 'skipped': dict(skipped),
            'flags': ['activity_truncated'] if truncated else []}


def build_watchlist(results, previous):
    prior = {r['address'].lower(): r for tier in ('active', 'watch', 'overflow')
             for r in previous.get(tier, [])}
    active, watch, excluded = [], [], []
    seen = {r['address'].lower() for r in results}
    results = list(results) + [dict(address=addr, name=old.get('name'), sample='not_measured')
                              for addr, old in prior.items() if addr not in seen]
    for r in results:
        old = prior.get(r['address'].lower(), {})
        if r.get('sample') != 'ok':
            if old:
                watch.append({**old, **r, 'base_edge': 0.0,
                              'previous_base_edge': old.get('base_edge'),
                              'clv7_cents': None, 'clv_sd_per_entry': None,
                              'shrink_factor': None, 'resolved_share': None,
                              'trades_30d': r.get('trades_30d'),
                              'last_trade_age_days': r.get('last_trade_age_days'),
                              'n_entries_used': r.get('n_entries_used', 0),
                              'metrics': r.get('metrics', {}), 'sports_metrics': {},
                              'signal_scope': None, 'signed_edge': None,
                              'active_scopes': [], 'negative_streak': 0,
                              'negative_streaks': {}, 'reason': 'stale_no_new_sample'})
            continue
        metrics = r.get('metrics', {})
        # Legacy direct callers can still provide a single explicitly scoped metric.
        if not metrics and r.get('signal_scope'):
            metrics = {r['signal_scope']: dict(sample='ok', base_edge=r['base_edge'],
                                             signed_edge=r['signed_edge'])}
        streaks = {}
        for scope in ('core', 'sports'):
            metric = metrics.get(scope, {})
            old_streak = old.get('negative_streaks', {}).get(scope, 0)
            if not old.get('negative_streaks') and old.get('signal_scope') == scope:
                old_streak = old.get('negative_streak', 0)
            streaks[scope] = old_streak+1 if metric.get('sample') == 'ok' and metric['signed_edge'] < 0 else 0
        eligible = [scope for scope, metric in metrics.items()
                    if metric.get('sample') == 'ok' and metric['base_edge'] >= ACTIVE_EDGE_MIN]
        live = (r.get('trades_30d') or 0) >= 10
        reliable = not r.get('activity_truncated')
        active_scopes = eligible if live and reliable else []
        entry = {**r, 'metrics': metrics, 'active_scopes': active_scopes,
                 'negative_streaks': streaks, 'negative_streak': streaks['core']}
        valid = [scope for scope, metric in metrics.items() if metric.get('sample') == 'ok']
        if valid and all(streaks.get(scope, 0) >= 2 for scope in valid):
            excluded.append({**entry, 'reason': 'negative_twice_in_a_row'})
        elif active_scopes:
            active.append({**entry, 'reason': 'active'})
        else:
            reason = 'dormant' if not live else 'incomplete_activity' if not reliable else 'low_edge'
            watch.append({**entry, 'reason': reason})
    def score(r):
        return max([float(r.get('base_edge') or 0)] +
                   [float(v.get('base_edge') or 0) for v in r.get('metrics', {}).values()])
    active.sort(key=lambda r: (-score(r), r['address']))
    watch.extend({**r, 'active_scopes': [], 'reason': 'active_overflow'} for r in active[MAX_ACTIVE:])
    watch.sort(key=lambda r: (-score(r), r['address']))
    return {'schema_version': 1, 'measurement_version': 4, 'active': active[:MAX_ACTIVE],
            'watch': watch[:MAX_WATCH], 'overflow': watch[MAX_WATCH:], 'excluded_this_run': excluded}


def roster_coverage(results, previous):
    """Anteil des bisherigen Bestands, der in diesem Lauf ohne Fehler durchlief.

    Bewusst NICHT "hat qualifiziert": ein sauber gemessenes Wallet mit zu
    wenigen Ereignissen ist kein Abruffehler. Den Qualifikationseinbruch faengt
    separat no_collapse ab."""
    prior = {r['address'].lower() for tier in ('active', 'watch', 'overflow')
             for r in previous.get(tier, [])}
    if not prior:
        return 1.0
    done = {r['address'].lower() for r in results
            if r.get('sample') in ('ok', 'insufficient')}
    return len(prior & done) / len(prior)


def run(args, client=None):
    client = client or Client(args.max_history_requests, args.deadline_min,
                              args.max_market_requests)
    data = Path(args.data_dir)
    path = data/'watchlist.json'
    previous = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(previous, dict):
        raise ValueError('Invalid previous watchlist')
    for tier in ('active', 'watch', 'overflow'):
        if not isinstance(previous.get(tier, []), list):
            raise ValueError('Invalid previous watchlist tier')
        for row in previous.get(tier, []):
            if not isinstance(row, dict) or not re.fullmatch(r'0x[0-9a-fA-F]{40}', str(row.get('address', ''))):
                raise ValueError('Invalid previous wallet address')
    now = time.time()
    results, found, run_error, partial = [], {}, None, False
    try:
        found = candidates(client, previous, args.max_candidates)
        for address, meta in found.items():
            client.check_deadline()
            try:
                results.append(analyze_wallet(client, address, meta, now, args.max_activity_pages))
            except BudgetExceeded:
                raise
            except (RuntimeError, ValueError, TypeError, KeyError) as exc:
                results.append({'address': address, **meta, 'sample': 'error', 'error': str(exc)})
    except BudgetExceeded as exc:
        # Budgetgrenze ist kein inhaltlicher Fehler. Der Lauf ist unvollstaendig,
        # darf aber publizieren, wenn er den bisherigen Bestand abgedeckt hat --
        # sonst friert ein einziger langsamer Tag die Watchlist dauerhaft ein.
        partial = str(exc)
    except (RuntimeError, ValueError, TypeError, KeyError) as exc:
        run_error = str(exc)
    completed = {r['address'] for r in results}
    results.extend({'address': addr, **meta, 'sample': 'not_measured', 'error': run_error or partial}
                   for addr, meta in found.items() if addr not in completed)
    failures = sum(r['sample'] in ('error', 'not_measured') for r in results)
    failure_rate = failures/len(results) if results else 1.0
    coverage = roster_coverage(results, previous)
    stamp = datetime.now(timezone.utc).isoformat()
    proposed = build_watchlist(results, previous)
    prev_active = len(previous.get('active', []))
    qualified = len(proposed['active'])
    # Ein Einbruch der qualifizierten Wallets ist ein Befund, kein Update.
    no_collapse = prev_active == 0 or qualified >= max(MIN_ACTIVE_ABS,
                                                       MIN_ACTIVE_RETENTION*prev_active)
    # Frueher: publish nur bei NULL Fehlern. Bei 250 bis 800 Wallets blockiert
    # dann ein einzelner Timeout jedes Update. Jetzt zaehlt die Quote, der
    # bisherige Bestand muss abgedeckt sein und der Bestand darf nicht einbrechen.
    publish = (bool(results) and not run_error
               and failure_rate <= MAX_FAILURE_RATE
               and coverage >= MIN_ROSTER_COVERAGE
               and no_collapse
               and any(r['sample'] == 'ok' or r.get('sports_only') for r in results))
    scopes = Counter(s for r in proposed['active'] for s in r.get('active_scopes', []))
    reasons = Counter()
    for r in results:
        for key, count in (r.get('skipped') or {}).items():
            reasons[key] += count
    report = {'schema_version': 1, 'measurement_version': 4,
              'generated_at_utc': stamp, 'n_candidates': len(found), 'results': results,
              'tokens_fetched': client.history_calls, 'history_requests': client.history_calls,
              'market_requests': client.market_calls,
              'runtime_min': round((time.monotonic()-client.started)/60, 2),
              'hit_deadline': time.monotonic() >= client.deadline,
              'partial_run': partial, 'failure_rate': round(failure_rate, 4),
              'roster_coverage': round(coverage, 4),
              'active_previous': prev_active, 'active_proposed': qualified,
              'no_collapse': no_collapse,
              'active_scopes_histogram': dict(scopes),
              'skipped_histogram': dict(reasons.most_common()),
              'errors': client.errors, 'run_error': run_error,
              'watchlist_updated': publish and not args.dry_run,
              'warning': 'Legacy edges are core seven-day CLV only. Sports metrics are separate. '
                         'Neither is a calibrated probability or direct Kelly input.'}
    atomic_save(data/'analysis_biweekly.json', report)
    if publish and not args.dry_run:
        atomic_save(path, {**proposed, 'generated_at_utc': stamp})
    elif not publish:
        atomic_save(data/'watchlist_proposed.json', {**proposed, 'generated_at_utc': stamp})
        print(f'Watchlist NICHT ersetzt. Abdeckung {coverage:.0%} (Grenze '
              f'{MIN_ROSTER_COVERAGE:.0%}), Fehlerquote {failure_rate:.1%}, aktiv '
              f'{qualified} gegen vorher {prev_active} (no_collapse={no_collapse}), '
              f'run_error={run_error}, partial={partial}. '
              f'Vorschlag in data/watchlist_proposed.json, Diagnose in '
              f'data/analysis_biweekly.json (skipped_histogram).')
    print(json.dumps({'n_candidates': len(found), 'tokens_fetched': client.history_calls,
                      'market_requests': client.market_calls,
                      'active': len(proposed['active']), 'watch': len(proposed['watch']),
                      'excluded_this_run': len(proposed['excluded_this_run']),
                      'errors': failures, 'failure_rate': round(failure_rate, 4),
                      'roster_coverage': round(coverage, 4),
                      'active_previous': prev_active, 'no_collapse': no_collapse,
                      'active_scopes': dict(scopes),
                      'top_skipped': dict(reasons.most_common(5)),
                      'run_error': run_error, 'partial_run': partial,
                      'watchlist_updated': report['watchlist_updated']}))
    return 0 if publish else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', default=str(DATA_DIR))
    parser.add_argument('--max-candidates', type=int, default=MAX_CANDIDATES)
    parser.add_argument('--max-history-requests', type=int, default=MAX_TOKENS)
    parser.add_argument('--max-market-requests', type=int, default=MAX_MARKET_REQUESTS)
    parser.add_argument('--max-activity-pages', type=int, default=10)
    parser.add_argument('--deadline-min', type=float, default=DEADLINE_MIN)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if min(args.max_candidates, args.max_history_requests, args.max_market_requests,
           args.max_activity_pages, args.deadline_min) < 1 or args.max_activity_pages > 20:
        parser.error('Budgets must be positive; activity pages must be <= 20')
    try:
        return run(args)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f'Run aborted; existing watchlist preserved: {exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
