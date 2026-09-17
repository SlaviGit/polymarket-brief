name: Biweekly Sharp-Wallet Analysis

on:
  schedule:
    # Cron kennt kein natives "alle 14 Tage" — läuft am 1. und 15. jedes
    # Monats um 03:00 UTC, das entspricht ungefähr alle 2 Wochen.
    - cron: '0 3 1,15 * *'
  workflow_dispatch: {}

# Verhindert, dass zwei Läufe (Cron + manueller Trigger, oder zwei manuelle
# hintereinander) gleichzeitig laufen und sich beim Push in die Quere kommen.
concurrency:
  group: biweekly-analysis
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  analyze:
    runs-on: ubuntu-latest
    timeout-minutes: 180
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      # Exit 1 heisst "Watchlist nicht ersetzt, Vorschlag liegt vor" -- ein
      # Befund, keine Fehlfunktion. Der Schritt darf daran nicht sterben, sonst
      # geht genau die Diagnose verloren, fuer die das Tor gebaut wurde.
      # Bewertet wird der Code erst nach dem Commit.
      - name: Run biweekly analysis
        id: analyze
        run: |
          set +e
          python3 analyze_biweekly.py
          code=$?
          set -e
          echo "code=$code" >> "$GITHUB_OUTPUT"
          echo "Analyse beendet mit Exit-Code $code."

      - name: Commit and push results
        if: always()
        run: |
          git config user.name "polymarket-bot"
          git config user.email "actions@users.noreply.github.com"
          git add data/
          git diff --cached --quiet || git commit -m "Biweekly sharp-wallet analysis $(date -u +%Y-%m-%dT%H:%M:%SZ)"
          # Falls in der Zwischenzeit (z.B. ein manueller Commit am Skript)
          # etwas anderes auf main gelandet ist: nachziehen statt scheitern.
          # data/ wird ausschliesslich hier geschrieben, ein echter Konflikt
          # ist darum praktisch ausgeschlossen -- nur der stale Checkout.
          git fetch origin main
          git rebase origin/main
          git push origin HEAD:main

      # Erst jetzt urteilen: 0 gruen, 1 gruen mit Warnung, alles andere rot.
      - name: Bewertung
        if: always()
        run: |
          code='${{ steps.analyze.outputs.code }}'
          case "$code" in
            0)
              echo "Watchlist ersetzt." ;;
            1)
              echo "::warning title=Watchlist nicht ersetzt::Das Publish-Tor hat gegriffen. Vorschlag in data/watchlist_proposed.json, Begruendung in data/analysis_biweekly.json (no_collapse, active_floor, baseline_comparable, skipped_histogram). Beides ist committet." ;;
            *)
              echo "::error title=Analyse fehlgeschlagen::Exit-Code ${code:-kein}. Lauf abgebrochen oder Bestand nicht sauber gemessen -- siehe run_error, failure_rate und roster_coverage in data/analysis_biweekly.json."
              exit 1 ;;
          esac
