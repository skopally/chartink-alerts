"""
Chartink Nifty 50 Bullish & Bearish Email Alert
------------------------------------------------
Fetches "Perfect Bullish" and "Perfect Bearish" Nifty 50 stocks from Chartink
and emails them to you. Designed to run hourly.

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

# ---------------------------------------------------------------------------
# 2. SCREENER DEFINITIONS  (Nifty 50 — perfect bullish & perfect bearish)
# ---------------------------------------------------------------------------
# Perfect Bullish: price above 20/50/200 SMA, SMAs stacked up, RSI strong, MACD bullish
BULLISH_CLAUSE = (
    "( {cash} ( "
    "group = \"nifty 50\" "
    "and latest close > latest sma( latest close , 20 ) "
    "and latest sma( latest close , 20 ) > latest sma( latest close , 50 ) "
    "and latest sma( latest close , 50 ) > latest sma( latest close , 200 ) "
    "and latest rsi( 14 ) > 55 "
    "and latest macd line( 26 , 12 , 9 ) > latest macd signal( 26 , 12 , 9 ) "
    ") )"
)

# Perfect Bearish: price below 20/50/200 SMA, SMAs stacked down, RSI weak, MACD bearish
BEARISH_CLAUSE = (
    "( {cash} ( "
    "group = \"nifty 50\" "
    "and latest close < latest sma( latest close , 20 ) "
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
# 3. FETCH FROM CHARTINK
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


# ---------------------------------------------------------------------------
# 4. BUILD THE EMAIL (HTML, looks clean on mobile and desktop)
# ---------------------------------------------------------------------------
def stocks_table(rows: list, accent: str) -> str:
    if not rows:
        return f'<p style="color:#666;font-style:italic;margin:8px 0 20px;">No stocks matched right now.</p>'

    body = ""
    for r in rows:
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
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;font-weight:600;">{symbol}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;color:#444;">{name}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">₹{price}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;color:{change_color};font-weight:600;">{change_str}</td>'
            f'</tr>'
        )

    return (
        f'<table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #eee;border-radius:8px;overflow:hidden;margin-bottom:24px;">'
        f'<thead><tr style="background:{accent};color:#fff;">'
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
    <h2 style="margin:0 0 4px;">Nifty 50 — Bullish & Bearish Alert</h2>
    <p style="color:#666;margin:0 0 20px;">{ts.strftime('%A, %d %b %Y · %I:%M %p IST')}</p>

    <h3 style="color:#16a34a;margin:0 0 8px;">🟢 Perfect Bullish ({len(bullish)})</h3>
    <p style="color:#666;font-size:13px;margin:0 0 8px;">Price &gt; 20/50/200 SMA · SMAs stacked up · RSI &gt; 55 · MACD bullish</p>
    {stocks_table(bullish, "#16a34a")}

    <h3 style="color:#dc2626;margin:0 0 8px;">🔴 Perfect Bearish ({len(bearish)})</h3>
    <p style="color:#666;font-size:13px;margin:0 0 8px;">Price &lt; 20/50/200 SMA · SMAs stacked down · RSI &lt; 45 · MACD bearish</p>
    {stocks_table(bearish, "#dc2626")}

    <p style="color:#888;font-size:12px;margin-top:24px;border-top:1px solid #eee;padding-top:12px;">
      Source: Chartink screener · Automated alert · For information only, not investment advice.
    </p>
  </div>
</body></html>"""


# ---------------------------------------------------------------------------
# 5. SEND EMAIL VIA GMAIL
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
# 6. MAIN
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
    bullish = fetch_screener(BULLISH_CLAUSE)
    print(f"  → {len(bullish)} stocks")

    print("Fetching bearish screener…")
    bearish = fetch_screener(BEARISH_CLAUSE)
    print(f"  → {len(bearish)} stocks")

    subject = f"Nifty 50 Alert · 🟢 {len(bullish)} Bullish · 🔴 {len(bearish)} Bearish · {now_ist:%d %b %I:%M %p}"
    html    = build_email_html(bullish, bearish, now_ist)

    print("Sending email…")
    send_email(subject, html)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
