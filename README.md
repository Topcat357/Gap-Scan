# Gap Scan

Weekday premarket gap scan (8:40 ET) and "gappers holding" check (9:45 ET), run by GitHub Actions.
Results land in `output/` — `gap_watchlist_latest.md` and `gap_holding_latest.md` are the newest.

- Run on demand: Actions tab -> "Premarket gap scan" -> Run workflow (mode: scan, lowfloat or holding).
- Optional live data: add your TradingView `sessionid` cookie as a repository secret named `TV_SESSIONID`.
- The same script runs on a PC: `python premarket_gap_scan.py --help`.
