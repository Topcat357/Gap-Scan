#!/usr/bin/env python3
"""
premarket_gap_scan.py - Trevor's premarket gap scanner + "gappers holding" check.

Runs on your PC (not in the cloud). Queries TradingView's screener service directly,
from your own connection, so it isn't affected by the cloud connector's rate limits.

USAGE
  python premarket_gap_scan.py              Premarket gap scan, standard filters (run ~8:00-9:25 ET)
  python premarket_gap_scan.py --lowfloat   Premarket gap scan, low-float momentum filters
  python premarket_gap_scan.py --holding    After the open (~9:40-10:00 ET): re-check today's
                                            gappers - are they holding above the open and premarket low?

REAL-TIME DATA
  Set the environment variable TV_SESSIONID to your TradingView "sessionid" cookie.
  (In Chrome/Edge: open tradingview.com while logged in -> F12 -> Application -> Cookies
   -> https://www.tradingview.com -> copy the value of "sessionid".)
  Windows, one time:   setx TV_SESSIONID "paste-value-here"   (then open a NEW terminal)
  Without it, TradingView may return DELAYED data. Treat the cookie like a password.
  Real-time also depends on what your TradingView plan includes.

OUTPUT (folder set by GAP_SCAN_DIR, default: Documents\\GapScan)
  gap_watchlist_YYYY-MM-DD.md        Mobile-friendly summary
  gap_watchlist_YYYY-MM-DD.csv       Full table
  gap_watchlist_YYYY-MM-DD_tv.txt    Import into a TradingView watchlist (Import list...)
  gap_watchlist_latest.json          Used by --holding and by the live brief
  gap_watchlist_latest.md            Copy of the newest scan (what the phone briefs read)
  gap_holding_YYYY-MM-DD.md          Output of --holding
  gap_holding_latest.md              Copy of the newest holding check

REQUIRES
  pip install requests        (on Windows also: pip install tzdata, for New York time)
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, date
from pathlib import Path

try:  # Always use New York time, even on GitHub's servers (which run on UTC).
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # Windows without the tzdata package: fall back to PC local time
    ET = None


def now_et():
    return datetime.now(ET) if ET else datetime.now()


def today_et():
    return now_et().date()

try:
    import requests
except ImportError:
    sys.exit("Missing package. Run:  pip install requests")

# ----------------------------------------------------------------------------
# SETTINGS - adjust freely
# ----------------------------------------------------------------------------
OUT_DIR = Path(os.environ.get("GAP_SCAN_DIR", Path.home() / "Documents" / "GapScan"))
SCAN_URL = "https://scanner.tradingview.com/america/scan"
EXCHANGES = ["NASDAQ", "NYSE", "AMEX"]
ROW_LIMIT = 25

# Standard profile - matches the Live Market Brief's "Premarket Gappers" screen
STANDARD = {
    "price": (1, 100),            # previous close, $
    "pm_change_min": 4.0,         # premarket % change (long-only, so gap UP)
    "pm_volume_min": 100_000,     # premarket shares traded
    "avg_volume_min": 1_000_000,  # 30-day average daily volume
    "mcap_min": 300_000_000,      # market cap, $
    "float_max": None,            # no float limit
}

# Low-float momentum profile - small floats move fastest on news, but also fade hardest
LOWFLOAT = {
    "price": (1, 100),
    "pm_change_min": 8.0,
    "pm_volume_min": 200_000,
    "avg_volume_min": None,
    "mcap_min": None,
    "float_max": 20_000_000,      # shares in the float
}

SCAN_COLUMNS = [
    "name", "description", "close", "premarket_close", "premarket_change",
    "premarket_volume", "premarket_high", "premarket_low",
    "float_shares_outstanding", "relative_volume_10d_calc",
    "average_volume_30d_calc", "market_cap_basic", "sector",
    "earnings_release_next_date",
]

HOLDING_COLUMNS = [
    "name", "close", "open", "high", "low", "change",
    "premarket_high", "premarket_low", "relative_volume_intraday|5", "volume",
]

# ----------------------------------------------------------------------------
# TradingView request helpers
# ----------------------------------------------------------------------------
def f(left, op, right):
    return {"left": left, "operation": op, "right": right}


def post_scan(payload):
    """POST to the screener with up to 3 attempts on rate limits."""
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    }
    cookies = {}
    sid = os.environ.get("TV_SESSIONID", "").strip()
    if sid:
        cookies["sessionid"] = sid

    for attempt in range(1, 4):
        r = requests.post(SCAN_URL, data=json.dumps(payload), headers=headers,
                          cookies=cookies, timeout=20)
        if r.status_code == 429:
            print(f"Rate-limited by TradingView (attempt {attempt}/3), waiting 30s...")
            time.sleep(30)
            continue
        if r.status_code != 200:
            sys.exit(f"TradingView returned HTTP {r.status_code}: {r.text[:300]}")
        return r.json()
    sys.exit("Still rate-limited after 3 attempts. Try again in a few minutes.")


def rows_to_dicts(resp, columns):
    out = []
    for row in resp.get("data", []):
        d = dict(zip(columns, row.get("d", [])))
        d["symbol"] = row.get("s", "")
        out.append(d)
    return out

# ----------------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------------
def num(v, digits=2):
    return "-" if v is None else f"{v:,.{digits}f}"


def big(v):
    if v is None:
        return "-"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"{v / div:.1f}{unit}"
    return f"{v:,.0f}"


def earnings_str(ts):
    if not ts:
        return "-"
    try:
        d = datetime.fromtimestamp(ts).date()
    except (OverflowError, OSError, ValueError):
        return "-"
    days = (d - today_et()).days
    tag = " (EARNINGS SOON)" if 0 <= days <= 2 else ""
    return f"{d.isoformat()}{tag}"

# ----------------------------------------------------------------------------
# Premarket gap scan
# ----------------------------------------------------------------------------
def build_filters(p):
    flt = [
        f("type", "equal", "stock"),
        f("is_primary", "equal", True),
        f("exchange", "in_range", EXCHANGES),
        f("active_symbol", "equal", True),
        f("close", "in_range", list(p["price"])),
        f("premarket_change", "greater", p["pm_change_min"]),
        f("premarket_volume", "greater", p["pm_volume_min"]),
    ]
    if p["avg_volume_min"]:
        flt.append(f("average_volume_30d_calc", "greater", p["avg_volume_min"]))
    if p["mcap_min"]:
        flt.append(f("market_cap_basic", "greater", p["mcap_min"]))
    if p["float_max"]:
        flt.append(f("float_shares_outstanding", "in_range", [0, p["float_max"]]))
    return flt


def run_scan(profile_name):
    p = LOWFLOAT if profile_name == "lowfloat" else STANDARD
    payload = {
        "markets": ["america"],
        "symbols": {"query": {"types": []}, "tickers": []},
        "options": {"lang": "en"},
        "columns": SCAN_COLUMNS,
        "filter": build_filters(p),
        "sort": {"sortBy": "premarket_volume", "sortOrder": "desc"},
        "range": [0, ROW_LIMIT],
    }
    resp = post_scan(payload)
    rows = rows_to_dicts(resp, SCAN_COLUMNS)
    total = resp.get("totalCount", len(rows))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = today_et().isoformat()
    stamp = now_et().strftime("%Y-%m-%d %H:%M")
    live_note = ("live (logged-in session)" if os.environ.get("TV_SESSIONID")
                 else "MAY BE DELAYED - TV_SESSIONID not set")

    # CSV
    csv_path = OUT_DIR / f"gap_watchlist_{today}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["symbol"] + SCAN_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    # TradingView import list
    tv_path = OUT_DIR / f"gap_watchlist_{today}_tv.txt"
    tv_path.write_text(",".join(r["symbol"] for r in rows), encoding="utf-8")

    # JSON for --holding and the live brief
    json_path = OUT_DIR / "gap_watchlist_latest.json"
    json_path.write_text(json.dumps({
        "date": today, "run_at": stamp, "profile": profile_name,
        "data": live_note, "total_matches": total, "rows": rows,
    }, indent=2), encoding="utf-8")

    # Mobile-friendly markdown
    lines = [
        f"# Premarket Gappers - {stamp} ET ({profile_name})",
        f"Data: {live_note}. {total} matches, top {len(rows)} by premarket volume.",
        "No news check here - the brief grades catalysts.",
        "",
    ]
    for r in rows:
        lines += [
            f"**{r.get('name')}** ({r['symbol']}) - {r.get('description') or ''}",
            f"- Gap {num(r.get('premarket_change'), 1)}% | PM ${num(r.get('premarket_close'))}"
            f" (prev close ${num(r.get('close'))})",
            f"- PM vol {big(r.get('premarket_volume'))} | PM high ${num(r.get('premarket_high'))}"
            f" / low ${num(r.get('premarket_low'))}",
            f"- Float {big(r.get('float_shares_outstanding'))} | Mkt cap {big(r.get('market_cap_basic'))}"
            f" | Avg vol {big(r.get('average_volume_30d_calc'))}",
            f"- Sector: {r.get('sector') or '-'} | Next earnings: "
            f"{earnings_str(r.get('earnings_release_next_date'))}",
            "",
        ]
    md_path = OUT_DIR / f"gap_watchlist_{today}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    (OUT_DIR / "gap_watchlist_latest.md").write_text("\n".join(lines), encoding="utf-8")

    print("\n".join(lines))
    print(f"Saved to {OUT_DIR}")

# ----------------------------------------------------------------------------
# After the open: are the gappers holding?
# ----------------------------------------------------------------------------
def run_holding():
    json_path = OUT_DIR / "gap_watchlist_latest.json"
    if not json_path.exists():
        sys.exit("No watchlist yet. Run the premarket scan first.")
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    if saved.get("date") != today_et().isoformat():
        print(f"Note: watchlist is from {saved.get('date')}, not today.")
    tickers = [r["symbol"] for r in saved.get("rows", [])]
    if not tickers:
        sys.exit("Watchlist is empty - nothing to check.")

    payload = {"symbols": {"tickers": tickers}, "columns": HOLDING_COLUMNS}
    rows = rows_to_dicts(post_scan(payload), HOLDING_COLUMNS)

    holding, strong, failing = [], [], []
    for r in rows:
        c, o, pml, pmh = r.get("close"), r.get("open"), r.get("premarket_low"), r.get("premarket_high")
        if c is None or o is None:
            continue
        above_open = c > o
        above_pml = pml is None or c > pml
        if above_open and above_pml:
            (strong if pmh is not None and c > pmh else holding).append(r)
        else:
            failing.append(r)

    def line(r):
        return (f"- **{r.get('name')}** ${num(r.get('close'))} ({num(r.get('change'), 1)}%) | "
                f"open ${num(r.get('open'))} | PM high ${num(r.get('premarket_high'))} / "
                f"low ${num(r.get('premarket_low'))} | intraday RVOL "
                f"{num(r.get('relative_volume_intraday|5'), 1)}")

    stamp = now_et().strftime("%Y-%m-%d %H:%M")
    lines = [f"# Gappers Holding - {stamp} ET", ""]
    lines += ["## Above premarket high (strongest)"] + ([line(r) for r in strong] or ["- none"]) + [""]
    lines += ["## Holding above open and PM low"] + ([line(r) for r in holding] or ["- none"]) + [""]
    lines += ["## Failing (below open or PM low) - skip"] + ([line(r) for r in failing] or ["- none"])
    lines += ["", "Confirm the 5-minute opening range on the chart before entering."]

    out = OUT_DIR / f"gap_holding_{today_et().isoformat()}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    (OUT_DIR / "gap_holding_latest.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"Saved to {out}")

# ----------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Premarket gap scan / gappers-holding check")
    ap.add_argument("--lowfloat", action="store_true", help="use the low-float profile")
    ap.add_argument("--holding", action="store_true", help="after-open check of today's list")
    args = ap.parse_args()

    if args.holding:
        run_holding()
    else:
        run_scan("lowfloat" if args.lowfloat else "standard")
