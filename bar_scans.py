"""
bar_scans.py - scans that need daily-bar math the TradingView screener can't do on its own.

How it works:
  1. A broad TradingView screener query narrows the market to plausible names
     (liquidity, above the 50-day, etc.) - fast, one request.
  2. Daily bars for those names are downloaded from Yahoo Finance (yfinance), and
     each scan's exact rules are checked on the bars.

Scans:
  breakout  - new 20-day high on the latest bar, volume >= 1.5x the 20-day average,
              close above the 50-day SMA, 20-day average volume >= 1M shares.
  coil      - last 15 bars span <= 8% of price, average true range of the last 5 bars
              <= 85% of the 20 bars before them (the contraction test), close within
              10% of the 52-week high, close above the 50-day SMA.

Modifiers (params, all optional) - e.g. "in tech", "large caps only", "on 2x volume":
  vol_mult        breakout volume multiple (default 1.5)
  lookback        breakout high lookback in bars (default 20)
  min_avg_vol     minimum 20-day average volume (default 1,000,000)
  sectors         list of TradingView sector names to keep
  mcap_min / mcap_max   market cap limits, $
  price_min / price_max price limits, $
  range_bars / range_max_pct            coil: tight-range window and max span (15, 8)
  range_min_pct   coil: minimum span (2) - filters out buyout-pinned, near-flat stocks
  atr_recent / atr_prior / atr_ratio_max coil: contraction test (5, 20, 0.85)
  near_high_pct   coil: max % below the 52-week high (10)
"""

import math
from datetime import datetime, time as dtime

DEFAULTS = {
    "breakout": {"vol_mult": 1.5, "lookback": 20, "min_avg_vol": 1_000_000},
    "coil": {"min_avg_vol": 1_000_000, "range_bars": 15, "range_max_pct": 8.0, "range_min_pct": 2.0,
             "atr_recent": 5, "atr_prior": 20, "atr_ratio_max": 0.85, "near_high_pct": 10.0},
}

# Plain-English sector shortcuts -> TradingView sector names
SECTOR_ALIASES = {
    "tech": ["Electronic Technology", "Technology Services"],
    "technology": ["Electronic Technology", "Technology Services"],
    "semis": ["Electronic Technology"],
    "software": ["Technology Services"],
    "health": ["Health Technology", "Health Services"],
    "healthcare": ["Health Technology", "Health Services"],
    "biotech": ["Health Technology"],
    "finance": ["Finance"],
    "financials": ["Finance"],
    "energy": ["Energy Minerals", "Industrial Services"],
    "industrials": ["Producer Manufacturing", "Industrial Services", "Transportation"],
    "consumer": ["Consumer Durables", "Consumer Non-Durables", "Consumer Services", "Retail Trade"],
    "retail": ["Retail Trade"],
    "utilities": ["Utilities"],
    "materials": ["Non-Energy Minerals", "Process Industries"],
    "communications": ["Communications"],
}

PREFILTER_COLUMNS = ["name", "description", "close", "SMA50", "price_52_week_high",
                     "average_volume_30d_calc", "market_cap_basic", "sector",
                     "float_shares_outstanding", "earnings_release_next_date",
                     "relative_volume_10d_calc", "change", "RSI", "EMA20", "SMA200"]


def resolve_sectors(sectors):
    out = []
    for s in sectors or []:
        out += SECTOR_ALIASES.get(str(s).strip().lower(), [s])
    return list(dict.fromkeys(out))


def prefilter(kind, p, f, base_filters):
    flt = base_filters() + [
        f("average_volume_30d_calc", "greater", p["min_avg_vol"] * 0.7),
        f("close", "greater", "SMA50"),
    ]
    if kind == "breakout":
        # Today's volume must be meaningfully above normal to have a chance at the vol test
        flt.append(f("relative_volume_10d_calc", "greater", max(1.0, p["vol_mult"] * 0.75)))
    if p.get("sectors"):
        flt.append(f("sector", "in_range", resolve_sectors(p["sectors"])))
    if p.get("mcap_min"):
        flt.append(f("market_cap_basic", "greater", p["mcap_min"]))
    if p.get("mcap_max"):
        flt.append(f("market_cap_basic", "less", p["mcap_max"]))
    if p.get("price_min"):
        flt.append(f("close", "greater", p["price_min"]))
    if p.get("price_max"):
        flt.append(f("close", "less", p["price_max"]))
    return flt


def yahoo_symbol(tv_symbol):
    return tv_symbol.split(":")[-1].replace(".", "-")


def download_bars(symbols, period):
    import yfinance as yf
    out = {}
    tickers = [yahoo_symbol(s) for s in symbols]
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        data = yf.download(chunk, period=period, interval="1d", group_by="ticker",
                           auto_adjust=False, progress=False, threads=True)
        for t in chunk:
            try:
                df = data[t] if len(chunk) > 1 else data
                df = df.dropna(subset=["High", "Low", "Close", "Volume"])
                if len(df):
                    out[t] = df
            except Exception:
                continue
    return out


def session_fraction(now_et, last_bar_date):
    """Fraction of today's regular session elapsed, if the last bar is today's partial bar."""
    if last_bar_date != now_et.date():
        return 1.0
    t = now_et.time()
    if t >= dtime(16, 0) or t < dtime(9, 30):
        return 1.0
    minutes = (now_et.hour * 60 + now_et.minute) - (9 * 60 + 30)
    return max(minutes, 15) / 390.0


def true_range(h, l, prev_c):
    return max(h - l, abs(h - prev_c), abs(l - prev_c))


def check_breakout(df, p, now_et):
    lb = int(p["lookback"])
    if len(df) < lb + 2:
        return None
    H, L, C, V = (df[c].tolist() for c in ("High", "Low", "Close", "Volume"))
    prior_high = max(H[-lb - 1:-1])
    avg_vol = sum(V[-lb - 1:-1]) / lb
    if avg_vol < p["min_avg_vol"] or H[-1] <= prior_high:
        return None
    frac = session_fraction(now_et, df.index[-1].date())
    paced_vol = V[-1] / frac
    vol_ratio = paced_vol / avg_vol if avg_vol else 0
    if vol_ratio < p["vol_mult"]:
        return None
    day_range = H[-1] - L[-1]
    close_pos = (C[-1] - L[-1]) / day_range if day_range else 1.0
    return {
        "bar_date": str(df.index[-1].date()),
        "partial_bar": frac < 1.0,
        "new_high": round(H[-1], 2),
        "prior_20d_high": round(prior_high, 2),
        "closed_above_prior_high": C[-1] > prior_high,
        "close_in_range_pct": round(close_pos * 100),
        "vol_ratio": round(vol_ratio, 2),
        "avg_vol_20d": round(avg_vol),
        "breakout_pct": round((C[-1] / prior_high - 1) * 100, 2),
    }


def check_coil(df, p, high_52w, now_et):
    rb, ar, ap = int(p["range_bars"]), int(p["atr_recent"]), int(p["atr_prior"])
    if len(df) < max(rb, ar + ap + 1, 21):
        return None
    H, L, C, V = (df[c].tolist() for c in ("High", "Low", "Close", "Volume"))
    avg_vol = sum(V[-21:-1]) / 20
    if avg_vol < p["min_avg_vol"]:
        return None
    span = max(H[-rb:]) - min(L[-rb:])
    span_pct = span / C[-1] * 100
    if span_pct > p["range_max_pct"] or span_pct < p.get("range_min_pct", 0):
        return None  # min: a near-flat range usually means a buyout-pinned stock, not a coil
    trs = [true_range(H[i], L[i], C[i - 1]) for i in range(1, len(C))]
    recent = sum(trs[-ar:]) / ar
    prior = sum(trs[-ar - ap:-ar]) / ap
    ratio = recent / prior if prior else math.inf
    if ratio > p["atr_ratio_max"]:
        return None
    hi = max(high_52w or 0, max(H))
    below_high = (1 - C[-1] / hi) * 100 if hi else 100
    if below_high > p["near_high_pct"]:
        return None
    r_hi, r_lo, lo5 = max(H[-rb:]), min(L[-rb:]), min(L[-5:])
    mm_target = r_hi + (r_hi - r_lo)          # measured move: range height added to the breakout
    risk = r_hi - lo5
    return {
        "bar_date": str(df.index[-1].date()),
        "partial_bar": session_fraction(now_et, df.index[-1].date()) < 1.0,
        "range_high": round(r_hi, 2),
        "range_low": round(r_lo, 2),
        "low_5d": round(lo5, 2),
        "mm_target": round(mm_target, 2),
        "rr_5d_stop": round((mm_target - r_hi) / risk, 2) if risk > 0 else None,
        "range_span_pct": round(span_pct, 2),
        "atr_ratio": round(ratio, 2),
        "atr_recent": round(recent, 3),
        "pct_below_52w_high": round(below_high, 2),
        "high_52w": round(hi, 2),
        "avg_vol_20d": round(avg_vol),
    }


def run_bar_scan(kind, params, post_scan, rows_to_dicts, f, base_filters, now_et):
    p = dict(DEFAULTS[kind])
    p.update({k: v for k, v in (params or {}).items() if v is not None})

    payload = {
        "markets": ["america"],
        "symbols": {"query": {"types": []}, "tickers": []},
        "options": {"lang": "en"},
        "columns": PREFILTER_COLUMNS,
        "filter": prefilter(kind, p, f, base_filters),
        "sort": {"sortBy": "average_volume_30d_calc", "sortOrder": "desc"},
        "range": [0, 1500],
    }
    resp = post_scan(payload)
    cands = rows_to_dicts(resp, PREFILTER_COLUMNS)

    if kind == "coil":  # cheap pre-check on the 52-week-high distance before downloading bars
        lim = 1 - (p["near_high_pct"] + 1) / 100
        cands = [r for r in cands if r.get("close") and r.get("price_52_week_high")
                 and r["close"] >= r["price_52_week_high"] * lim]

    bars = download_bars([r["symbol"] for r in cands], "6mo")
    hits = []
    for r in cands:
        df = bars.get(yahoo_symbol(r["symbol"]))
        if df is None:
            continue
        res = (check_breakout(df, p, now_et) if kind == "breakout"
               else check_coil(df, p, r.get("price_52_week_high"), now_et))
        if res:
            r.update(res)
            r["last_close"] = round(float(df["Close"].iloc[-1]), 2)
            hits.append(r)

    if kind == "breakout":
        hits.sort(key=lambda r: r["vol_ratio"], reverse=True)
    else:
        hits.sort(key=lambda r: (r["atr_ratio"], r["range_span_pct"]))
    return hits, len(cands), p
