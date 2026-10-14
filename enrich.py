"""
enrich.py - adds the per-stock detail the briefs used to fetch one TradingView call at a time.

For each scan row it adds an "enrich" block:
  daily   - prior-day high/low, EMA20/EMA50/SMA200, RSI(14), ATR(14), 1M/3M %, 52-week high,
            nearest resistance above / support below (daily swing highs/lows, last ~60 bars),
            volatility squeeze (sessions with Bollinger Bands inside Keltner Channels, last 10)
  intraday- today's 5-minute picture (regular session): opening range (first 5-min candle),
            high of day, VWAP, last price, higher-lows count, and today's premarket high/low
  news    - headlines from the 48 hours before the scan (Massive news API; needs MASSIVE_API_KEY)
            with Massive's per-stock sentiment, count, tone and the top 3 headlines

Data: Yahoo Finance (yfinance) for bars, Massive for news. Nothing here calls TradingView,
so the briefs' TradingView rate limits never come into play. Every part is best-effort:
a failure leaves that block as null and the brief falls back to the connector for that name.
"""

import os
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = None


def _yahoo(sym):
    return sym.split(":")[-1].replace(".", "-")


def _r(x, d=2):
    try:
        return None if x is None or x != x else round(float(x), d)
    except Exception:
        return None


# ---------------------------------------------------------------- daily
def _daily_block(df, ref_price):
    import pandas as pd
    c, h, l = df["Close"], df["High"], df["Low"]
    ema = lambda s, n: s.ewm(span=n, adjust=False, min_periods=n).mean()
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rsi = 100 - 100 / (1 + up / dn)
    prev = c.shift()
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    # squeeze: BB(20, 2) inside KC(20-EMA +/- 1.5 x ATR20), counted over the last 10 sessions
    mid = c.rolling(20).mean()
    sd = c.rolling(20).std(ddof=0)
    e20 = ema(c, 20)
    atr20 = tr.rolling(20).mean()
    inside = ((mid + 2 * sd) < (e20 + 1.5 * atr20)) & ((mid - 2 * sd) > (e20 - 1.5 * atr20))
    squeeze_10 = int(inside.tail(10).sum())

    # swing highs/lows: a bar higher (lower) than the 3 bars on each side, last ~60 bars
    w = df.tail(63)
    hs, ls = w["High"].tolist(), w["Low"].tolist()
    swing_hi = [hs[i] for i in range(3, len(hs) - 3)
                if hs[i] == max(hs[i - 3:i + 4])]
    swing_lo = [ls[i] for i in range(3, len(ls) - 3)
                if ls[i] == min(ls[i - 3:i + 4])]
    ref = ref_price if ref_price else float(c.iloc[-1])
    above = sorted(x for x in swing_hi + [float(w["High"].max())] if x > ref * 1.002)
    below = sorted((x for x in swing_lo + [float(w["Low"].min())] if x < ref * 0.998), reverse=True)

    def perf(n):
        return _r((c.iloc[-1] / c.iloc[-1 - n] - 1) * 100, 1) if len(c) > n else None

    return {
        "as_of": str(df.index[-1].date()),
        "close": _r(c.iloc[-1]),
        "prior_day_high": _r(h.iloc[-1]),
        "prior_day_low": _r(l.iloc[-1]),
        "ema20": _r(ema(c, 20).iloc[-1]), "ema50": _r(ema(c, 50).iloc[-1]),
        "sma200": _r(c.rolling(200).mean().iloc[-1]),
        "rsi14": _r(rsi.iloc[-1], 1), "atr14": _r(atr.iloc[-1]),
        "atr_pct": _r(atr.iloc[-1] / c.iloc[-1] * 100, 1),
        "perf_1m_pct": perf(21), "perf_3m_pct": perf(63),
        "high_52w": _r(h.tail(252).max()),
        "pct_below_52w_high": _r((1 - c.iloc[-1] / h.tail(252).max()) * 100, 1),
        "high_20d": _r(h.tail(20).max()), "low_20d": _r(l.tail(20).min()),
        "resistance_above": [_r(x) for x in above[:3]],
        "support_below": [_r(x) for x in below[:3]],
        "squeeze_sessions_last10": squeeze_10,
        "squeeze_fired": squeeze_10 >= 5,
    }


# ------------------------------------------------------------- intraday
def _intraday_block(df5, day):
    """df5: 5-minute bars incl. pre/post market, index in ET. day: date (ET)."""
    t = df5[df5.index.date == day]
    if t.empty:
        return None
    hhmm = t.index.strftime("%H:%M")
    pre = t[(hhmm >= "04:00") & (hhmm < "09:30")]
    reg = t[(hhmm >= "09:30") & (hhmm < "16:00")]
    out = {
        "date": str(day),
        "premarket_high": _r(pre["High"].max()) if len(pre) else None,
        "premarket_low": _r(pre["Low"].min()) if len(pre) else None,
        "premarket_last": _r(pre["Close"].iloc[-1]) if len(pre) else None,
        "bars_5m": int(len(reg)),
    }
    if reg.empty:
        return out
    tp = (reg["High"] + reg["Low"] + reg["Close"]) / 3
    vol = reg["Volume"].replace(0, float("nan"))
    vwap = (tp * reg["Volume"]).cumsum() / reg["Volume"].cumsum()
    lows = reg["Low"].tolist()
    higher_lows = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i - 1])
    first = reg.iloc[0]
    out.update({
        "or_high": _r(first["High"]), "or_low": _r(first["Low"]),
        "open": _r(first["Open"]),
        "high_of_day": _r(reg["High"].max()), "low_of_day": _r(reg["Low"].min()),
        "last": _r(reg["Close"].iloc[-1]), "last_bar": reg.index[-1].strftime("%H:%M"),
        "vwap": _r(vwap.iloc[-1]),
        "above_vwap": bool(reg["Close"].iloc[-1] > vwap.iloc[-1]),
        "bars_closed_below_vwap": int((reg["Close"] < vwap).sum()),
        "higher_lows": higher_lows,
        "volume_today": int(reg["Volume"].sum()),
        "pct_from_open": _r((reg["Close"].iloc[-1] / first["Open"] - 1) * 100, 1),
        "pct_off_high": _r((1 - reg["Close"].iloc[-1] / reg["High"].max()) * 100, 1),
    })
    return out


# ----------------------------------------------------------------- news
def _news_block(sym, hours=48):
    key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if not key:
        return None
    import requests
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    ticker = sym.split(":")[-1]
    params = {"ticker": ticker, "published_utc.gte": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "order": "desc", "sort": "published_utc", "limit": 50}
    hdr = {"Authorization": f"Bearer {key}"}
    try:
        r = requests.get("https://api.massive.com/v2/reference/news", params=params,
                         headers=hdr, timeout=20)
        if r.status_code == 429:
            time.sleep(30)
            r = requests.get("https://api.massive.com/v2/reference/news", params=params,
                             headers=hdr, timeout=20)
        if r.status_code != 200:
            return None
        arts = r.json().get("results") or []
    except Exception:
        return None
    labels = [i.get("sentiment") for a in arts for i in (a.get("insights") or [])
              if i.get("ticker") == ticker]
    cnt = Counter(labels)
    tone = ("none" if not arts else "positive" if cnt["positive"] > cnt["negative"]
            else "negative" if cnt["negative"] > cnt["positive"] else "neutral")
    top = []
    for a in arts[:3]:
        ins = next((i for i in (a.get("insights") or []) if i.get("ticker") == ticker), {})
        top.append({"published_utc": a.get("published_utc"), "title": a.get("title"),
                    "publisher": (a.get("publisher") or {}).get("name"),
                    "sentiment": ins.get("sentiment"),
                    "reason": (ins.get("sentiment_reasoning") or "")[:200],
                    "url": a.get("article_url")})
    return {"hours": hours, "count": len(arts), "tone": tone,
            "positive": cnt["positive"], "negative": cnt["negative"], "neutral": cnt["neutral"],
            "top": top}


# ------------------------------------------------------------------ main
def enrich_rows(rows, intraday=False, news_top=6, ref_key="close"):
    """Adds row["enrich"] = {daily, intraday, news}. Returns a short status string."""
    if not rows:
        return "no rows"
    t0 = time.time()
    try:
        import yfinance as yf
    except ImportError:
        return "yfinance not installed - enrichment skipped"
    syms = [r["symbol"] for r in rows]
    tick = [_yahoo(s) for s in syms]
    daily, five = {}, {}
    try:
        data = yf.download(tick, period="14mo", interval="1d", group_by="ticker",
                           auto_adjust=False, progress=False, threads=False)
        for s, t in zip(syms, tick):
            try:
                df = (data[t] if len(tick) > 1 else data).dropna(subset=["Close"])
                if len(df) >= 60:
                    daily[s] = df
            except Exception:
                pass
    except Exception as e:
        print(f"enrich: daily download failed: {e}")
    if intraday:
        try:
            data = yf.download(tick, period="5d", interval="5m", prepost=True, group_by="ticker",
                               auto_adjust=False, progress=False, threads=False)
            for s, t in zip(syms, tick):
                try:
                    df = (data[t] if len(tick) > 1 else data).dropna(subset=["Close"])
                    if len(df):
                        df.index = df.index.tz_convert(ET) if ET else df.index
                        five[s] = df
                except Exception:
                    pass
        except Exception as e:
            print(f"enrich: 5-minute download failed: {e}")
    today = datetime.now(ET).date() if ET else datetime.now().date()
    n_news = 0
    for i, r in enumerate(rows):
        s = r["symbol"]
        ref = r.get(ref_key) or r.get("premarket_close") or r.get("last_close")
        e = {"daily": None, "intraday": None, "news": None}
        try:
            if s in daily:
                e["daily"] = _daily_block(daily[s], ref)
        except Exception as ex:
            print(f"enrich: daily {s}: {ex}")
        try:
            if s in five:
                e["intraday"] = _intraday_block(five[s], today)
        except Exception as ex:
            print(f"enrich: intraday {s}: {ex}")
        if i < news_top:
            if n_news:
                time.sleep(12.5)  # Massive free tier: 5 calls a minute
            e["news"] = _news_block(s)
            n_news += 1
        r["enrich"] = e
    have_news = "with news" if os.environ.get("MASSIVE_API_KEY") else "no news (MASSIVE_API_KEY not set)"
    return (f"enriched {len(daily)}/{len(rows)} daily"
            + (f", {len(five)}/{len(rows)} intraday" if intraday else "")
            + f", {have_news} for top {min(news_top, len(rows))}; {time.time() - t0:.0f}s")
