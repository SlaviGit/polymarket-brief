Befund und Korrektur fuer analyze_biweekly.py (Lauf vom 15.09.2026, exit 1)

URSACHE
gamma-api.polymarket.com/markets filtert ohne Parameter standardmaessig auf
closed=false. Client.market() uebergibt nur condition_ids. Fuer jeden bereits
aufgeloesten Markt kommt daher [] zurueck, der Eintrag faellt unter
skipped['missing_market'] und verschwindet aus der Messung.

Live geprueft am 16.09.2026:
  ?condition_ids=0x9c66114d... (BTC 100k 2024, closed)            -> []
  ?condition_ids=0x9c66114d...&closed=true                        -> 1 Objekt,
       closed=true, closedTime="2024-12-05 04:46:26+00"
  ?condition_ids=0xa5ac4cdc... (Ukraine 90 Tage, closed)          -> []
  ?condition_ids=0x876506d8... (Fed Sept 2026, offen)             -> 1 Objekt
  ?condition_ids=0xc31f5893... (Barcelona, offen)                 -> 1 Objekt,
       gameStartTime="2026-09-16 19:30:00+00", sportsMarketType="moneyline"

Folgen:
  Kern  Die 7-Tage-Messung betrifft naturgemaess Maerkte, die inzwischen
        aufgeloest sind. Messbar bleiben nur Einstiege in heute noch offene
        Maerkte. Deshalb 4042 Markt-Lookups, aber nur 1664 History-Aufrufe.
  Sport Strukturell unmoeglich. benchmark() verlangt gameStartTime <= now,
        der Sportmarkt schliesst aber kurz nach Anpfiff. Ein Sporteinstieg ist
        damit entweder 'immature' oder 'missing_market'. metrics.sports kann
        MIN_EVENTS=5 nie erreichen -- unabhaengig von jedem Budget.

Das Publish-Tor hat korrekt gehandelt: eine Watchlist mit 1 aktivem Wallet
gegen bisher 14 darf den Bestand nicht ersetzen. Die Meldung ist nur
irrefuehrend -- Fehlerquote 0.0 und run_error=None sind korrekt, es ist nichts
fehlgeschlagen, es hat nur fast nichts qualifiziert.


--- 1. Client.market(): geschlossene Maerkte mitnehmen -----------------------

    def market(self, condition):
        if condition not in self.markets:
            found = None
            # gamma filtert ohne Parameter auf closed=false. Aufgeloeste Maerkte
            # kommen nur mit closed=true zurueck. Die Kernmessung betrifft
            # ueberwiegend aufgeloeste Maerkte, daher closed=true zuerst.
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

Budget: im schlechtesten Fall zwei Aufrufe je Markt. Beobachtet wurden 4042
eindeutige Maerkte, also bis rund 8000 Aufrufe.
    MAX_MARKET_REQUESTS = 12000
Laufzeit: der Lauf brauchte 57m38s von 300 Minuten. Rund 4000 Zusatzaufrufe zu
je etwa 0.5 s sind knapp 35 Minuten, zusammen etwa 95 Minuten. Passt.
History-Aufrufe steigen mit der Ueberlebensrate deutlich an:
    MAX_TOKENS = 12000


--- 2. timestamp(): gamma-Zeitformat "2024-12-05 04:46:26+00" ----------------

Leerzeichen als Trenner und zweistelliger Offset. datetime.fromisoformat
akzeptiert das erst ab Python 3.11; darunter ValueError -> None ->
'unknown_closure_time' bzw. 'sports_missing_start'. Betrifft genau die beiden
Felder, auf denen die Messung aufsetzt.

    def timestamp(x):
        n = number(x)
        if n is not None:
            return n
        s = str(x).strip().replace('Z', '+00:00')
        s = re.sub(r'([+-]\d{2})$', r'\1:00', s)   # "+00" -> "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            return dt.timestamp() if dt.tzinfo else None
        except ValueError:
            return None

Zusaetzlich im Workflow die Version festnageln, statt dem Runner-Standard zu
vertrauen:
    - uses: actions/setup-python@v5
      with:
        python-version: '3.12'


--- 3. roster_coverage(): misst das Falsche -----------------------------------

Gezaehlt wird nur sample == 'ok'. Ein Wallet, das sauber abgerufen wurde, aber
schlicht keine 5 Ereignisse hat, zaehlt wie ein Abbruch. Trennen:

    def roster_coverage(results, previous):
        prior = {r['address'].lower() for tier in ('active', 'watch', 'overflow')
                 for r in previous.get(tier, [])}
        if not prior:
            return 1.0
        # abgedeckt = ohne Fehler gemessen, nicht = hat qualifiziert
        done = {r['address'].lower() for r in results
                if r.get('sample') in ('ok', 'insufficient')}
        return len(prior & done) / len(prior)

Und als zweites, ehrlicheres Tor gegen ein stilles Leerraeumen der Watchlist
(haette den Lauf vom 15.09. ebenfalls gestoppt, aber mit der richtigen
Begruendung):

    prev_active = len(previous.get('active', []))
    qualified = len(proposed['active'])
    no_collapse = prev_active == 0 or qualified >= max(3, 0.5*prev_active)
    publish = (bool(results) and not run_error
               and failure_rate <= MAX_FAILURE_RATE
               and coverage >= MIN_ROSTER_COVERAGE
               and no_collapse)

Meldung entsprechend:
    print(f'Watchlist NICHT ersetzt: Abdeckung {coverage:.0%}, '
          f'Fehlerquote {failure_rate:.1%}, aktiv {qualified} (vorher '
          f'{prev_active}), run_error={run_error}, partial={partial}. '
          f'Vorschlag in data/watchlist_proposed.json.')


--- 4. Nachrangig: Stichprobe nach Ereignis statt nach Fill -------------------

MIN_EVENTS zaehlt Ereignisse, MAX_ENTRIES_PER_WALLET=50 zieht aber 50 Fills
zufaellig ueber 180 Tage. Das maximiert die Zahl verschiedener Maerkte (also
gamma-Aufrufe) und minimiert die Buendelung zu Ereignissen -- genau verkehrt
herum. Die bisher veroeffentlichte Watchlist weist n_entries_used von 169 bis
456 je Wallet aus, also rund das Zehnfache der neuen Obergrenze.
Vorschlag: eligible nach conditionId gruppieren, die juengsten N Ereignisse
waehlen und deren Fills vollstaendig nehmen. Erreicht MIN_EVENTS mit deutlich
weniger Markt-Lookups.


--- Workflow --------------------------------------------------------------

"Commit and push results" laeuft nur bei Erfolg. Deshalb liegt weder das neue
analysis_biweekly.json noch data/watchlist_proposed.json im Repo -- beide
wurden lokal geschrieben und mit dem Runner verworfen (404 auf
raw.githubusercontent.com/.../data/watchlist_proposed.json, geprueft 16.09.).
Damit die Diagnose beim naechsten Fehlschlag erhalten bleibt:

    - name: Commit and push results
      if: always()

oder mindestens den Vorschlag und den Report als Artefakt hochladen
(actions/upload-artifact mit if: always()).
