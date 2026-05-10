"""
Chartink Nifty 50 Bullish & Bearish Email Alert
------------------------------------------------
Fetches "Perfect Bullish" and "Perfect Bearish" Nifty 50 stocks from Chartink
and emails the TOP 5 of each, ranked by today's % change.

You don't need to edit the code — all your details go into "secrets"
(environment variables). See SETUP_GUIDE.md for step-by-step instructions.
"""

import os
import sys
import smtplib
import requests
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# 1. CONFIGURATION  (read from environment variables — set these in GitHub)
# ---------------------------------------------------------------------------
SENDER_EMAIL    = os.environ.get("GMAIL_USER", "").strip()
APP_PASSWORD    = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", SENDER_EMAIL).strip()

# Set to "true" if you want emails ONLY during Indian market hours (Mon–Fri, 9 AM–4 PM IST)
MARKET_HOURS_ONLY = os.environ.get("MARKET_HOURS_ONLY", "true").lower() == "true"

# How many top stocks to show in each list (bullish / bearish)
TOP_N = 5

# ---------------------------------------------------------------------------
# 2. NIFTY 50 STOCK LIST  (used to strictly filter out indices and non-Nifty stocks)
# ---------------------------------------------------------------------------
# If the Nifty 50 list changes (rebalancing), you can update this list anytime.
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
# Perfect Bullish: price above 20/50/200 SMA, SMAs stacked up, RSI strong, MACD bullish
BULLISH_CLAUSE = (
    "( {cash} ( "
    "latest close > latest sma( latest close , 20 ) "
    "and latest sma( latest close , 20 ) > latest sma( latest close , 50 ) "
    "and latest sma( latest close , 50 ) > latest sma( latest close , 200 ) "
    "and latest rsi( 14 ) > 55 "
    "and latest macd line( 26 , 12 , 9 ) > latest macd signal( 26 , 12 , 9 ) "
    ") )"
)

# Perfect Bearish: price below 20/50/200 SMA, SMAs stacked down, RSI weak, MACD bearish
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
# 4. FETCH FROM CHARTINK
# ---------------------------------------------------------------------------
def fetch_screener(scan_clause: str) -> list:
    """Run a Chartink scan and return the list of matching stocks."""
    with requests.Session() as session:
        # Get the CSRF token (Chartink requires this on every request)
        home = session.get(CHARTINK_HOME, timeout=30)
        soup = BeautifulSoup(home.text, "html.parser")
        csrf_tag = soup.select_one("[name='csrf-token']")
        if not csrf_tag:
            raise RuntimeError("Could not find Chartink CSRF token — site may have changed.")

        session.headers.update({
            "x-csrf-token": csrf_tag["content"],
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": CHARTINK_HOME,
        })

        resp = session.post(CHARTINK_PROCESS, data={"scan_clause": scan_clause}, timeout=30)
        resp.raise_for_status()
        return resp.json().get("data", []) or []


def filter_and_rank(rows: list, top_n: int, ascending: bool) -> list:
    """Keep only Nifty 50 stocks, then sort by % change and return top N."""
    # Step 1: Keep only Nifty 50 stocks (excludes indices like CNXENERGY, CNXMNC, etc.)
    nifty_only = [r for r in rows if (r.get("nsecode") or "").upper() in NIFTY_50]

    # Step 2: Sort by today's % change. Ascending=True for bearish (most negative first).
    def pct(r):
        try:
            return float(r.get("per_chg", 0))
        except (TypeError, ValueError):
            return 0.0

    nifty_only.sort(key=pct, reverse=not ascending)
    return nifty_only[:top_n]


# ---------------------------------------------------------------------------
# 5. BUILD THE EMAIL (HTML, looks clean on mobile and desktop)
# ---------------------------------------------------------------------------
def stocks_table(rows: list, accent: str) -> str:
    if not rows:
        return f'<p style="color:#666;font-style:italic;margin:8px 0 20px;">No stocks matched right now.</p>'

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


def build_email_html(bullish: list, bearish: list, ts: datetime) -> str:
    return f"""\
<!DOCTYPE html>
<html><body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f6f7f9;padding:20px;color:#222;">
  <div style="max-width:680px;margin:0 auto;">
    <h2 style="margin:0 0 4px;">Nifty 50 — Top 5 Bullish & Bearish</h2>
    <p style="color:#666;margin:0 0 20px;">{ts.strftime('%A, %d %b %Y · %I:%M %p IST')}</p>

    <h3 style="color:#16a34a;margin:0 0 8px;">🟢 Top 5 Perfect Bullish</h3>
    <p style="color:#666;font-size:13px;margin:0 0 8px;">Price &gt; 20/50/200 SMA · SMAs stacked up · RSI &gt; 55 · MACD bullish · Ranked by today's % gain</p>
    {stocks_table(bullish, "#16a34a")}

    <h3 style="color:#dc2626;margin:0 0 8px;">🔴 Top 5 Perfect Bearish</h3>
    <p style="color:#666;font-size:13px;margin:0 0 8px;">Price &lt; 20/50/200 SMA · SMAs stacked down · RSI &lt; 45 · MACD bearish · Ranked by today's % loss</p>
    {stocks_table(bearish, "#dc2626")}

    <p style="color:#888;font-size:12px;margin-top:24px;border-top:1px solid #eee;padding-top:12px;">
      Source: Chartink screener · Filtered to Nifty 50 stocks only · Automated alert · For information only, not investment advice.
    </p>
  </div>
</body></html>"""


# ---------------------------------------------------------------------------
# 6. SEND EMAIL VIA GMAIL
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
# 7. MAIN
# ---------------------------------------------------------------------------
def main() -> int:
    if not SENDER_EMAIL or not APP_PASSWORD:
        print("ERROR: GMAIL_USER and GMAIL_APP_PASSWORD must be set as environment variables.")
        return 1

    now_ist = datetime.now(IST)

    if MARKET_HOURS_ONLY:
        # Indian market: Mon-Fri, ~9:15 AM – 3:30 PM. We allow 9 AM – 4 PM as a buffer.
        if now_ist.weekday() >= 5:
            print(f"Skipping — weekend ({now_ist:%A}).")
            return 0
        if not (9 <= now_ist.hour < 16):
            print(f"Skipping — outside market hours ({now_ist:%H:%M} IST).")
            return 0

    print("Fetching bullish screener…")
    bullish_all = fetch_screener(BULLISH_CLAUSE)
    bullish = filter_and_rank(bullish_all, TOP_N, ascending=False)
    print(f"  → {len(bullish_all)} matches, top {len(bullish)} Nifty 50 by % gain")

    print("Fetching bearish screener…")
    bearish_all = fetch_screener(BEARISH_CLAUSE)
    bearish = filter_and_rank(bearish_all, TOP_N, ascending=True)
    print(f"  → {len(bearish_all)} matches, top {len(bearish)} Nifty 50 by % loss")

    subject = f"Nifty 50 Top 5 · 🟢 Bullish · 🔴 Bearish · {now_ist:%d %b %I:%M %p}"
    html    = build_email_html(bullish, bearish, now_ist)

    print("Sending email…")
    send_email(subject, html)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
