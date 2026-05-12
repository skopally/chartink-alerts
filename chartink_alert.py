"""
Chartink Nifty 50 — Hourly Alerts + End-of-Day Analysis
--------------------------------------------------------
- Sends a SIMPLE Top 5 Bullish & Bearish email every hour at :30 IST
  (9:30, 10:30, 11:30, 12:30, 13:30, 14:30, 15:30)
- Sends a RICH End-of-Day Analysis email at 16:00 IST with full indicator
  details (RSI, MACD, SMAs, volume) + a next-day open momentum watchlist.

The external scheduler (cron-job.org) is expected to fire this every 30 minutes
during market hours. The script itself decides which fires actually send an
email — extra fires are silently skipped.

NOT investment advice. For information only.
"""

import os
import sys
import time
import random
import smtplib
import requests
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta

# yfinance / pandas are imported lazily inside the EOD enrichment function so that
# the simple hourly email doesn't pay the import cost or fail if those packages
# happen to be unavailable for some reason. See enrich_with_yfinance() below.

# ---------------------------------------------------------------------------
# 1. CONFIGURATION (from environment variables — set as GitHub Secrets)
# ---------------------------------------------------------------------------
SENDER_EMAIL    = os.environ.get("GMAIL_USER", "").strip()
APP_PASSWORD    = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", SENDER_EMAIL).strip()

# Set to "true" to only send emails Mon–Fri during market window; "false" for testing anytime.
MARKET_HOURS_ONLY = os.environ.get("MARKET_HOURS_ONLY", "true").lower() == "true"

# How many top stocks to show in each list (bullish / bearish)
TOP_N = 5

# ---------------------------------------------------------------------------
# 2. NIFTY 50 STOCK LIST  (used to strictly filter out indices and non-Nifty stocks)
# ---------------------------------------------------------------------------
NIFTY_50 = {
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJAJFINSV", "BAJFINANCE", "BEL", "BHARTIARTL",
    "BPCL", "BRITANNIA", "CIPLA", "COALINDIA", "DRREDDY",
    "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK",
    "INFY", "ITC", "JSWSTEEL", "KOTAKBANK", "LT",
    "LTIM", "M&M", "MARUTI", "NESTLEIND", "NTPC",
    "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN",
    "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL",
    "TCS", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
}

# ---------------------------------------------------------------------------
# 3. SCREENER DEFINITIONS  (perfect bullish & perfect bearish)
# ---------------------------------------------------------------------------
BULLISH_CLAUSE = (
    "( {cash} ( "
    "latest close > latest sma( latest close , 20 ) "
    "and latest sma( latest close , 20 ) > latest sma( latest close , 50 ) "
    "and latest sma( latest close , 50 ) > latest sma( latest close , 200 ) "
    "and latest rsi( 14 ) > 55 "
    "and latest macd line( 26 , 12 , 9 ) > latest macd signal( 26 , 12 , 9 ) "
    ") )"
)

BEARISH_CLAUSE = (
    "( {cash} ( "
    "latest close < latest sma( latest close , 20 ) "
    "and latest sma( latest close , 20 ) < latest sma( latest close , 50 ) "
    "and latest sma( latest close , 50 ) < latest sma( latest close , 200 ) "
    "and latest rsi( 14 ) < 45 "
    "and latest macd line( 26 , 12 , 9 ) < latest macd signal( 26 , 12 , 9 ) "
    ") )"
)

CHARTINK_HOME    = "https://chartink.com/screener/"
CHARTINK_PROCESS = "https://chartink.com/screener/process"
IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# 4. WHAT TO SEND BASED ON CURRENT TIME
# ---------------------------------------------------------------------------
def email_kind_now(now_ist) -> str | None:
    """Decide what to send right now based on IST time.

    Returns:
      'hourly' — simple Top 5 Bullish/Bearish (target X:30 IST, X = 9..15)
      'eod'    — full EOD analysis + next-day watchlist (target 16:00 IST)
      None     — skip this run silently

    Tolerates ±5 min on hourly slots and ±15 min on EOD slot so the external
    scheduler doesn't have to fire to the exact second.
    """
    # Weekends: no email
    if now_ist.weekday() >= 5:
        return None

    # If market-hours mode is disabled, treat any run as an EOD run so we can test
    if not MARKET_HOURS_ONLY:
        return 'eod'

    h, m = now_ist.hour, now_ist.minute

    # End-of-day analysis: target 16:00 IST. Accept 15:55 – 16:15.
    if h == 16 and m <= 15:
        return 'eod'
    if h == 15 and m >= 55:
        return 'eod'

    # Hourly emails: target X:30 IST for X in 9..15. Accept :25 – :35.
    if h in (9, 10, 11, 12, 13, 14, 15) and 25 <= m <= 35:
        return 'hourly'

    return None


# ---------------------------------------------------------------------------
# 5. FETCH FROM CHARTINK (with browser-like User-Agent + retry on 429)
# ---------------------------------------------------------------------------
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def fetch_screener(scan_clause: str, max_retries: int = 4) -> list:
    """Run a Chartink scan and return the list of matching stocks.

    Retries on 429 (Too Many Requests) and 5xx errors with exponential backoff.
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            with requests.Session() as session:
                session.headers.update({
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                })

                home = session.get(CHARTINK_HOME, timeout=30)
                home.raise_for_status()
                soup = BeautifulSoup(home.text, "html.parser")
                csrf_tag = soup.select_one("[name='csrf-token']")
                if not csrf_tag:
                    raise RuntimeError("Could not find Chartink CSRF token — site may have changed.")

                session.headers.update({
                    "x-csrf-token": csrf_tag["content"],
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": CHARTINK_HOME,
                    "Origin": "https://chartink.com",
                    "X-Requested-With": "XMLHttpRequest",
                })

                time.sleep(0.5)

                resp = session.post(
                    CHARTINK_PROCESS,
                    data={"scan_clause": scan_clause},
                    timeout=30,
                )
                resp.raise_for_status()
                return resp.json().get("data", []) or []

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            last_error = e
            if status in (429, 500, 502, 503, 504) and attempt < max_retries:
                wait = (5 ** attempt) + random.uniform(0, 3)
                print(f"  ⚠ Chartink returned {status}. Waiting {wait:.1f}s before retry {attempt + 1}/{max_retries}…")
                time.sleep(wait)
                continue
            raise
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < max_retries:
                wait = (5 ** attempt) + random.uniform(0, 3)
                print(f"  ⚠ Network error: {e}. Waiting {wait:.1f}s before retry {attempt + 1}/{max_retries}…")
                time.sleep(wait)
                continue
            raise

    if last_error:
        raise last_error
    return []


def filter_and_rank(rows: list, top_n: int, ascending: bool) -> list:
    """Keep only Nifty 50 stocks, then sort by % change and return top N."""
    nifty_only = [r for r in rows if (r.get("nsecode") or "").upper() in NIFTY_50]

    def pct(r):
        try:
            return float(r.get("per_chg", 0))
        except (TypeError, ValueError):
            return 0.0

    nifty_only.sort(key=pct, reverse=not ascending)
    return nifty_only[:top_n]


# ---------------------------------------------------------------------------
# 6. YFINANCE ENRICHMENT (only used for EOD email)
# ---------------------------------------------------------------------------
def yahoo_ticker(symbol: str) -> str:
    """Convert NSE symbol to Yahoo Finance ticker."""
    return f"{symbol.upper()}.NS"


def compute_indicators(df) -> dict:
    """From a daily OHLCV DataFrame, compute RSI, MACD, SMAs, etc."""
    out = {}
    try:
        if df is None or df.empty or len(df) < 20:
            return out

        close = df['Close']

        # Simple Moving Averages
        out['sma20']  = float(close.rolling(20).mean().iloc[-1])
        out['sma50']  = float(close.rolling(50).mean().iloc[-1])  if len(close) >= 50  else None
        out['sma200'] = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None

        # RSI (14) — Wilder smoothing
        delta = close.diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-9)
        rsi = 100 - (100 / (1 + rs))
        out['rsi'] = float(rsi.iloc[-1])

        # MACD (12, 26, 9)
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal = macd_line.ewm(span=9, adjust=False).mean()
        out['macd']           = float(macd_line.iloc[-1])
        out['macd_signal']    = float(signal.iloc[-1])
        out['macd_hist']      = float((macd_line - signal).iloc[-1])
        out['macd_hist_prev'] = float((macd_line - signal).iloc[-2]) if len(macd_line) >= 2 else None

        # Today's OHLCV
        last = df.iloc[-1]
        out['open']   = float(last['Open'])
        out['high']   = float(last['High'])
        out['low']    = float(last['Low'])
        out['close']  = float(last['Close'])
        out['volume'] = float(last['Volume'])

        # Yesterday's reference levels
        if len(df) >= 2:
            prev = df.iloc[-2]
            out['prev_close'] = float(prev['Close'])
            out['prev_high']  = float(prev['High'])
            out['prev_low']   = float(prev['Low'])

        # 20-day average volume
        if len(df) >= 20:
            out['avg_volume_20'] = float(df['Volume'].rolling(20).mean().iloc[-1])

        # Close position in day's range: 0 = at low, 1 = at high
        rng = out['high'] - out['low']
        out['close_pos'] = (out['close'] - out['low']) / rng if rng > 0 else 0.5

    except Exception as e:
        print(f"  compute_indicators error: {e}")
        return {}

    return out


def enrich_with_yfinance(stocks: list, lookback_days: int = 250) -> list:
    """For each stock, fetch yfinance data and compute indicators.
    Adds an 'indicators' key. Sets it to {} on failure (so email still renders).
    """
    try:
        import yfinance as yf
    except ImportError:
        print("  ⚠ yfinance not installed — EOD enrichment skipped.")
        for s in stocks:
            s['indicators'] = {}
        return stocks

    for s in stocks:
        sym = (s.get('nsecode') or '').upper()
        if not sym:
            s['indicators'] = {}
            continue
        ticker = yahoo_ticker(sym)
        try:
            data = yf.Ticker(ticker).history(period=f"{lookback_days}d", auto_adjust=False)
            inds = compute_indicators(data)
            s['indicators'] = inds
            if inds:
                print(f"  ✓ {sym}: RSI {inds.get('rsi', 0):.1f}, MACD {inds.get('macd', 0):+.2f}")
            else:
                print(f"  ⚠ {sym}: no indicators computed (insufficient data?)")
        except Exception as e:
            print(f"  ⚠ yfinance error for {sym}: {e}")
            s['indicators'] = {}
        time.sleep(0.3)  # be gentle on yahoo
    return stocks


# ---------------------------------------------------------------------------
# 7. MOMENTUM SCORING (next-day open carry-over)
# ---------------------------------------------------------------------------
def momentum_score(inds: dict, direction: str):
    """Returns (score 0-100, list of contributing reasons).

    direction = 'bull' or 'bear'.
    Higher score = stronger end-of-day momentum in that direction.
    Honest framing — this is a carry-over signal, NOT a guaranteed prediction.
    """
    if not inds:
        return 0, ["No indicator data available"]

    score = 0
    reasons = []

    pos            = inds.get('close_pos')
    rsi            = inds.get('rsi')
    macd_hist      = inds.get('macd_hist')
    macd_hist_prev = inds.get('macd_hist_prev')
    close          = inds.get('close')
    prev_high      = inds.get('prev_high')
    prev_low       = inds.get('prev_low')
    sma20          = inds.get('sma20')
    sma50          = inds.get('sma50')
    sma200         = inds.get('sma200')
    vol            = inds.get('volume')
    avg_vol        = inds.get('avg_volume_20')

    if direction == 'bull':
        # Close near day's high
        if pos is not None:
            if pos >= 0.9:
                score += 25; reasons.append(f"Closed at top of day's range ({pos*100:.0f}%)")
            elif pos >= 0.7:
                score += 15; reasons.append(f"Closed in upper range ({pos*100:.0f}%)")

        # Volume confirmation
        if vol and avg_vol and avg_vol > 0:
            ratio = vol / avg_vol
            if ratio > 1.5:
                score += 20; reasons.append(f"Volume {ratio:.1f}× 20D average (surge)")
            elif ratio > 1.0:
                score += 10; reasons.append(f"Volume {ratio:.1f}× 20D average")

        # RSI in strong-but-not-overbought zone
        if rsi is not None:
            if 55 <= rsi <= 70:
                score += 15; reasons.append(f"RSI {rsi:.0f} — strong, not yet overbought")
            elif rsi > 70:
                score += 5;  reasons.append(f"RSI {rsi:.0f} — overbought (caution)")

        # MACD histogram expanding upward
        if macd_hist is not None and macd_hist_prev is not None:
            if macd_hist > macd_hist_prev and macd_hist > 0:
                score += 15; reasons.append("MACD histogram expanding (bullish acceleration)")

        # Breakout above yesterday's high
        if close and prev_high and close > prev_high:
            score += 15; reasons.append(f"Closed above yesterday's high (₹{prev_high:.2f})")

        # Long-term trend intact
        if sma20 and sma50 and sma200 and sma20 > sma50 > sma200:
            score += 10; reasons.append("SMAs cleanly stacked up (20 > 50 > 200)")

    else:  # bear
        if pos is not None:
            if pos <= 0.1:
                score += 25; reasons.append(f"Closed at bottom of day's range ({pos*100:.0f}%)")
            elif pos <= 0.3:
                score += 15; reasons.append(f"Closed in lower range ({pos*100:.0f}%)")

        if vol and avg_vol and avg_vol > 0:
            ratio = vol / avg_vol
            if ratio > 1.5:
                score += 20; reasons.append(f"Volume {ratio:.1f}× 20D average (surge)")
            elif ratio > 1.0:
                score += 10; reasons.append(f"Volume {ratio:.1f}× 20D average")

        if rsi is not None:
            if 30 <= rsi <= 45:
                score += 15; reasons.append(f"RSI {rsi:.0f} — weak, not yet oversold")
            elif rsi < 30:
                score += 5;  reasons.append(f"RSI {rsi:.0f} — oversold (possible bounce risk)")

        if macd_hist is not None and macd_hist_prev is not None:
            if macd_hist < macd_hist_prev and macd_hist < 0:
                score += 15; reasons.append("MACD histogram expanding negatively (bearish acceleration)")

        if close and prev_low and close < prev_low:
            score += 15; reasons.append(f"Closed below yesterday's low (₹{prev_low:.2f})")

        if sma20 and sma50 and sma200 and sma20 < sma50 < sma200:
            score += 10; reasons.append("SMAs cleanly stacked down (20 < 50 < 200)")

    return min(score, 100), reasons


def score_label(score: int) -> str:
    if score >= 80:
        return "🔥 Strong"
    elif score >= 60:
        return "⚡ Moderate"
    elif score >= 40:
        return "💧 Mild"
    else:
        return "💨 Weak"


# ---------------------------------------------------------------------------
# 8. EMAIL TEMPLATES — SIMPLE (hourly) and RICH (EOD)
# ---------------------------------------------------------------------------
def simple_stocks_table(rows: list, accent: str) -> str:
    if not rows:
        return '<p style="color:#666;font-style:italic;margin:8px 0 20px;">No stocks matched right now.</p>'

    body = ""
    for i, r in enumerate(rows, 1):
        symbol = r.get("nsecode", "")
        name   = r.get("name", "")
        price  = r.get("close", "")
        change = r.get("per_chg", "")
        try:
            change_num = float(change)
            change_color = "#16a34a" if change_num >= 0 else "#dc2626"
            change_str = f"{change_num:+.2f}%"
        except (TypeError, ValueError):
            change_color = "#666"
            change_str = str(change)

        body += (
            f'<tr>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;color:#888;font-weight:600;">#{i}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;font-weight:600;">{symbol}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;color:#444;">{name}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">₹{price}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;color:{change_color};font-weight:600;">{change_str}</td>'
            f'</tr>'
        )

    return (
        f'<table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #eee;border-radius:8px;overflow:hidden;margin-bottom:24px;">'
        f'<thead><tr style="background:{accent};color:#fff;">'
        f'<th style="padding:10px 12px;text-align:left;">Rank</th>'
        f'<th style="padding:10px 12px;text-align:left;">Symbol</th>'
        f'<th style="padding:10px 12px;text-align:left;">Name</th>'
        f'<th style="padding:10px 12px;text-align:right;">LTP</th>'
        f'<th style="padding:10px 12px;text-align:right;">% Chg</th>'
        f'</tr></thead>'
        f'<tbody>{body}</tbody>'
        f'</table>'
    )


def build_simple_email_html(bullish: list, bearish: list, ts: datetime) -> str:
    return f"""\
<!DOCTYPE html>
<html><body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f6f7f9;padding:20px;color:#222;">
  <div style="max-width:680px;margin:0 auto;">
    <h2 style="margin:0 0 4px;">Nifty 50 — Top 5 Bullish & Bearish</h2>
    <p style="color:#666;margin:0 0 20px;">{ts.strftime('%A, %d %b %Y · %I:%M %p IST')}</p>

    <h3 style="color:#16a34a;margin:0 0 8px;">🟢 Top 5 Perfect Bullish</h3>
    <p style="color:#666;font-size:13px;margin:0 0 8px;">Price &gt; 20/50/200 SMA · SMAs stacked up · RSI &gt; 55 · MACD bullish · Ranked by today's % gain</p>
    {simple_stocks_table(bullish, "#16a34a")}

    <h3 style="color:#dc2626;margin:0 0 8px;">🔴 Top 5 Perfect Bearish</h3>
    <p style="color:#666;font-size:13px;margin:0 0 8px;">Price &lt; 20/50/200 SMA · SMAs stacked down · RSI &lt; 45 · MACD bearish · Ranked by today's % loss</p>
    {simple_stocks_table(bearish, "#dc2626")}

    <p style="color:#888;font-size:12px;margin-top:24px;border-top:1px solid #eee;padding-top:12px;">
      Source: Chartink screener · Filtered to Nifty 50 stocks only · Automated alert · For information only, not investment advice.
    </p>
  </div>
</body></html>"""


def detail_row(s: dict, i: int, direction: str) -> str:
    """One row in the EOD detail table, with full indicator values."""
    inds = s.get('indicators') or {}
    sym  = s.get('nsecode', '')
    name = s.get('name', '')

    close = inds.get('close')
    if close is None:
        try:
            close = float(s.get('close', 0))
        except (TypeError, ValueError):
            close = 0

    try:
        change_num = float(s.get('per_chg', 0))
    except (TypeError, ValueError):
        change_num = 0
    change_color = '#16a34a' if change_num >= 0 else '#dc2626'
    change_str = f"{change_num:+.2f}%"

    rsi       = inds.get('rsi')
    macd      = inds.get('macd')
    macd_sig  = inds.get('macd_signal')
    sma20     = inds.get('sma20')
    sma50     = inds.get('sma50')
    sma200    = inds.get('sma200')
    vol       = inds.get('volume')
    avg_vol   = inds.get('avg_volume_20')
    close_pos = inds.get('close_pos')

    rsi_str  = f"{rsi:.1f}"    if rsi      is not None else "—"
    macd_str = f"{macd:+.2f}"  if macd     is not None else "—"
    sig_str  = f"{macd_sig:+.2f}" if macd_sig is not None else "—"

    sma_parts = []
    if sma20:  sma_parts.append(f"20:₹{sma20:.0f}")
    if sma50:  sma_parts.append(f"50:₹{sma50:.0f}")
    if sma200: sma_parts.append(f"200:₹{sma200:.0f}")
    sma_str = " · ".join(sma_parts)

    vol_str = ""
    if vol and avg_vol and avg_vol > 0:
        vol_str = f"Vol: {vol/avg_vol:.1f}× 20D avg"

    pos_str = f"Closed at {close_pos*100:.0f}% of day's range" if close_pos is not None else ""

    detail_bits = " · ".join(p for p in [sma_str, vol_str, pos_str] if p)

    return f"""
    <tr>
      <td style="padding:10px 12px;border-bottom:1px solid #eee;vertical-align:top;">
        <div style="font-weight:600;font-size:15px;">#{i} {sym}</div>
        <div style="color:#666;font-size:12px;">{name}</div>
      </td>
      <td style="padding:10px 12px;border-bottom:1px solid #eee;text-align:right;vertical-align:top;">
        <div style="font-weight:600;">₹{close:.2f}</div>
        <div style="color:{change_color};font-weight:600;font-size:13px;">{change_str}</div>
      </td>
      <td style="padding:10px 12px;border-bottom:1px solid #eee;vertical-align:top;font-size:13px;color:#444;">
        <div>RSI(14): <strong>{rsi_str}</strong></div>
        <div>MACD: <strong>{macd_str}</strong> / Signal: {sig_str}</div>
        <div style="color:#777;margin-top:4px;font-size:12px;">{detail_bits or '—'}</div>
      </td>
    </tr>
    """


def watchlist_card(s: dict, direction: str) -> str:
    """A card for the next-day open watchlist section."""
    inds = s.get('indicators') or {}
    score, reasons = momentum_score(inds, direction)
    label = score_label(score)
    accent = '#16a34a' if direction == 'bull' else '#dc2626'
    sym  = s.get('nsecode', '')
    name = s.get('name', '')

    reasons_html = ''.join(f'<li style="margin:2px 0;">{r}</li>' for r in reasons)

    return f"""
    <div style="background:#fff;border:1px solid #eee;border-left:4px solid {accent};border-radius:6px;padding:12px 16px;margin-bottom:10px;">
      <div style="margin-bottom:6px;">
        <strong style="font-size:15px;">{sym}</strong>
        <span style="color:#666;font-size:12px;margin-left:6px;">{name}</span>
        <span style="float:right;font-size:13px;font-weight:600;">{label} · {score}/100</span>
      </div>
      <ul style="margin:6px 0 0 18px;padding:0;color:#444;font-size:12px;">{reasons_html}</ul>
    </div>
    """


def build_eod_email_html(bullish: list, bearish: list, ts: datetime) -> str:
    bull_rows = ''.join(detail_row(s, i + 1, 'bull') for i, s in enumerate(bullish))
    bear_rows = ''.join(detail_row(s, i + 1, 'bear') for i, s in enumerate(bearish))

    # Sort watchlist cards by score (highest first) so strongest signals are on top
    bull_sorted = sorted(bullish, key=lambda s: momentum_score(s.get('indicators') or {}, 'bull')[0], reverse=True)
    bear_sorted = sorted(bearish, key=lambda s: momentum_score(s.get('indicators') or {}, 'bear')[0], reverse=True)

    bull_watchlist = ''.join(watchlist_card(s, 'bull') for s in bull_sorted)
    bear_watchlist = ''.join(watchlist_card(s, 'bear') for s in bear_sorted)

    return f"""\
<!DOCTYPE html>
<html><body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f6f7f9;padding:20px;color:#222;">
  <div style="max-width:720px;margin:0 auto;">
    <h2 style="margin:0 0 4px;">📊 EOD Analysis &amp; Next-Day Watchlist</h2>
    <p style="color:#666;margin:0 0 24px;">{ts.strftime('%A, %d %b %Y · %I:%M %p IST')} · Market closed at 3:30 PM</p>

    <!-- SECTION A: Full EOD Analysis -->
    <h3 style="color:#16a34a;margin:24px 0 8px;">🟢 Top 5 Bullish — Full EOD Analysis</h3>
    <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #eee;border-radius:8px;overflow:hidden;margin-bottom:24px;">
      <thead><tr style="background:#16a34a;color:#fff;">
        <th style="padding:10px 12px;text-align:left;">Stock</th>
        <th style="padding:10px 12px;text-align:right;">Close</th>
        <th style="padding:10px 12px;text-align:left;">Indicators</th>
      </tr></thead>
      <tbody>{bull_rows}</tbody>
    </table>

    <h3 style="color:#dc2626;margin:24px 0 8px;">🔴 Top 5 Bearish — Full EOD Analysis</h3>
    <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #eee;border-radius:8px;overflow:hidden;margin-bottom:24px;">
      <thead><tr style="background:#dc2626;color:#fff;">
        <th style="padding:10px 12px;text-align:left;">Stock</th>
        <th style="padding:10px 12px;text-align:right;">Close</th>
        <th style="padding:10px 12px;text-align:left;">Indicators</th>
      </tr></thead>
      <tbody>{bear_rows}</tbody>
    </table>

    <!-- SECTION B: Next-Day Open Watchlist -->
    <h3 style="margin:32px 0 8px;">🎯 Next-Day Open Watchlist</h3>
    <p style="color:#666;font-size:13px;margin:0 0 14px;">
      Momentum carry-over scoring (0–100) based on close position in day's range, volume vs 20-day average,
      RSI zone, MACD histogram trend, and breakout/breakdown signals. Sorted strongest first.<br>
      <strong style="color:#b45309;">⚠️ This is NOT a prediction.</strong> Overnight news, global market cues, and gap moves
      frequently override technical signals at the open. Treat as one input among many.
    </p>

    <h4 style="color:#16a34a;margin:16px 0 8px;">🟢 Likely strong open — bullish carry-over</h4>
    {bull_watchlist}

    <h4 style="color:#dc2626;margin:20px 0 8px;">🔴 Likely weak open — bearish carry-over</h4>
    {bear_watchlist}

    <p style="color:#888;font-size:12px;margin-top:32px;border-top:1px solid #eee;padding-top:14px;">
      <strong>Disclaimer:</strong> This is an automated technical analysis using Chartink screener results
      and Yahoo Finance EOD data. Indicator values are end-of-day; intraday values will differ.
      <strong>This is NOT investment or trading advice.</strong> Stock prices are influenced by news, global markets,
      sentiment, liquidity, and many factors no automated system can predict. Always do your own research
      and risk management before any trade. Past patterns do not guarantee future moves.
    </p>
  </div>
</body></html>"""


# ---------------------------------------------------------------------------
# 9. SEND EMAIL VIA GMAIL
# ---------------------------------------------------------------------------
def send_email(subject: str, html_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER_EMAIL, APP_PASSWORD)
        server.send_message(msg)


# ---------------------------------------------------------------------------
# 10. MAIN
# ---------------------------------------------------------------------------
def main() -> int:
    if not SENDER_EMAIL or not APP_PASSWORD:
        print("ERROR: GMAIL_USER and GMAIL_APP_PASSWORD must be set as environment variables.")
        return 1

    now_ist = datetime.now(IST)
    print(f"Current IST time: {now_ist:%Y-%m-%d %H:%M:%S}")

    kind = email_kind_now(now_ist)
    if kind is None:
        print(f"Skipping — {now_ist:%H:%M} IST is not a scheduled email slot.")
        return 0

    print(f"Mode: {kind.upper()}")

    # Fetch screeners (same for both modes)
    print("Fetching bullish screener…")
    bullish_all = fetch_screener(BULLISH_CLAUSE)
    bullish = filter_and_rank(bullish_all, TOP_N, ascending=False)
    print(f"  → {len(bullish_all)} total matches, top {len(bullish)} Nifty 50 by % gain")

    time.sleep(3)  # gentle pause between Chartink calls

    print("Fetching bearish screener…")
    bearish_all = fetch_screener(BEARISH_CLAUSE)
    bearish = filter_and_rank(bearish_all, TOP_N, ascending=True)
    print(f"  → {len(bearish_all)} total matches, top {len(bearish)} Nifty 50 by % loss")

    # Build the right email
    if kind == 'hourly':
        subject = f"Nifty 50 Top 5 · 🟢 Bullish · 🔴 Bearish · {now_ist:%d %b %I:%M %p}"
        html    = build_simple_email_html(bullish, bearish, now_ist)
    else:  # 'eod'
        print("Enriching with yfinance EOD data…")
        bullish = enrich_with_yfinance(bullish)
        bearish = enrich_with_yfinance(bearish)
        subject = f"📊 EOD Analysis & Next-Day Watchlist · Nifty 50 · {now_ist:%d %b %Y}"
        html    = build_eod_email_html(bullish, bearish, now_ist)

    print(f"Sending {kind} email…")
    send_email(subject, html)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
