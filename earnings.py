#!/usr/bin/env python3
"""
earnings.py - Weekly earnings calendar digest

Sends one message every Monday morning listing which large cap companies
report during the coming week, plus the scheduled macro events.

You will not beat anyone to an earnings release. The value here is never
being caught holding a position into one you did not know about.

Run modes:
    python earnings.py            send the weekly digest
    python earnings.py test       connectivity check, sends nothing
"""

import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta

import requests

from monitor import box, log, telegram, load_json, save_json, STATE_DIR

USER_AGENT = os.environ.get("SEC_USER_AGENT", "FilingsMonitor")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")

# Companies big enough that their results move the index, not just the stock.
# Edit freely. Anything not on this list is ignored by the digest.
WATCHLIST = {
    # Mega cap tech
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA", "AVGO",
    "ORCL", "CRM", "AMD", "ADBE", "INTC", "CSCO", "QCOM", "TXN", "MU", "AMAT",
    "LRCX", "KLAC", "ARM", "PLTR", "SNOW", "NOW", "PANW", "CRWD", "DDOG",
    "MDB", "NET", "SHOP", "UBER", "ABNB", "COIN", "SQ", "PYPL", "SMCI",
    # Financials
    "BRK.B", "JPM", "BAC", "WFC", "GS", "MS", "C", "SCHW", "BLK", "AXP",
    "V", "MA", "SPGI", "CB", "PGR",
    # Healthcare
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR", "AMGN",
    "BMY", "GILD", "VRTX", "REGN", "ISRG", "MDT", "CVS",
    # Consumer and industrial
    "WMT", "COST", "HD", "PG", "KO", "PEP", "MCD", "NKE", "SBUX", "TGT",
    "LOW", "DIS", "NFLX", "CMCSA", "T", "VZ", "TMUS",
    "CAT", "DE", "BA", "GE", "HON", "UNP", "UPS", "FDX", "LMT", "RTX", "NOC",
    # Energy and materials
    "XOM", "CVX", "COP", "SLB", "EOG", "PSX", "MPC", "OXY", "LIN", "FCX",
}

STATE_FILE = STATE_DIR / "earnings_state.json"

NASDAQ_CAL = "https://api.nasdaq.com/api/calendar/earnings?date={date}"
FINNHUB_CAL = ("https://finnhub.io/api/v1/calendar/earnings"
               "?from={start}&to={end}&token={key}")

# Recurring macro events. Dates that matter regardless of what you hold.
MACRO_NOTES = [
    "CPI and PPI land mid month, PCE at month end",
    "Non-farm payrolls the first Friday of each month",
    "Jobless claims every Thursday",
]

session = requests.Session()


# ---------------------------------------------------------------
# Sources
# ---------------------------------------------------------------

def fetch_nasdaq(day, debug=False):
    """Nasdaq's public calendar. No key, but it is picky about headers."""
    url = NASDAQ_CAL.format(date=day.strftime("%Y-%m-%d"))
    try:
        r = session.get(url, timeout=30, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nasdaq.com/market-activity/earnings",
            "Origin": "https://www.nasdaq.com",
        })
    except requests.RequestException as e:
        log(f"nasdaq calendar unreachable :: {e}")
        return None
    if r.status_code != 200:
        log(f"nasdaq calendar HTTP {r.status_code}")
        return None
    try:
        rows = (r.json().get("data") or {}).get("rows") or []
    except ValueError:
        return None

    if debug:
        allsyms = [(r.get("symbol") or "").upper() for r in rows]
        print(f"    total companies reporting: {len(rows)}")
        print(f"    sample: {', '.join(allsyms[:12])}")

    out = []
    for row in rows:
        sym = (row.get("symbol") or "").upper().strip()
        if sym in WATCHLIST:
            out.append({
                "date": day.strftime("%Y-%m-%d"),
                "symbol": sym,
                "name": (row.get("name") or "")[:28],
                "when": row.get("time") or "",
            })
    return out


def fetch_finnhub(start, end):
    if not FINNHUB_KEY:
        return None
    url = FINNHUB_CAL.format(start=start.strftime("%Y-%m-%d"),
                             end=end.strftime("%Y-%m-%d"), key=FINNHUB_KEY)
    try:
        r = session.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    except requests.RequestException as e:
        log(f"finnhub calendar unreachable :: {e}")
        return None
    if r.status_code != 200:
        log(f"finnhub calendar HTTP {r.status_code}")
        return None
    try:
        rows = r.json().get("earningsCalendar") or []
    except ValueError:
        return None

    if debug:
        allsyms = [(r.get("symbol") or "").upper() for r in rows]
        print(f"    total companies reporting: {len(rows)}")
        print(f"    sample: {', '.join(allsyms[:12])}")

    out = []
    for row in rows:
        sym = (row.get("symbol") or "").upper().strip()
        if sym in WATCHLIST:
            out.append({
                "date": row.get("date", ""),
                "symbol": sym,
                "name": "",
                "when": {"bmo": "pre", "amc": "post"}.get(
                    (row.get("hour") or "").lower(), ""),
            })
    return out


def collect_week(start, end):
    """Nasdaq first, Finnhub as fallback. Returns [] if both fail."""
    found = []
    day = start
    nasdaq_ok = False
    while day <= end:
        if day.weekday() < 5:               # weekdays only
            rows = fetch_nasdaq(day)
            if rows is not None:
                nasdaq_ok = True
                found.extend(rows)
            time.sleep(0.6)
        day += timedelta(days=1)

    if nasdaq_ok:
        return found

    log("nasdaq unavailable, trying finnhub")
    rows = fetch_finnhub(start, end)
    return rows if rows is not None else []


# ---------------------------------------------------------------
# Digest
# ---------------------------------------------------------------

def when_label(w):
    w = str(w).lower()
    if "pre" in w or "bmo" in w or "before" in w:
        return "pre"
    if "post" in w or "amc" in w or "after" in w:
        return "post"
    return ""


def build_digest(start, end, rows):
    by_day = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)

    out = []
    day = start
    while day <= end:
        key = day.strftime("%Y-%m-%d")
        if day.weekday() < 5:
            names = sorted({r["symbol"] for r in by_day.get(key, [])})
            if names:
                label = day.strftime("%a %d %b")
                shown = ", ".join(names[:9])
                if len(names) > 9:
                    shown += f"  +{len(names) - 9}"
                out.append((label, shown))
        day += timedelta(days=1)
    return out


def run_digest():
    today = datetime.now(timezone.utc).date()
    # Monday of the coming week
    start = today + timedelta(days=(7 - today.weekday()) % 7 or 0)
    if today.weekday() == 0:
        start = today
    end = start + timedelta(days=4)

    rows = collect_week(start, end)
    digest = build_digest(start, end, rows)

    if not digest:
        telegram(box("WEEK AHEAD - no calendar data", [
            ("PRIORITY", "MEDIUM"),
            ("WEEK", f"{start.strftime('%d %b')} to {end.strftime('%d %b')}"),
            ("PROBLEM", "Earnings calendar returned nothing"),
            ("LIKELY CAUSE", "Nasdaq blocked the request or changed format"),
            ("ACTION", "Run the earnings workflow manually with the test arg"),
        ]))
        return

    body = [("WEEK", f"{start.strftime('%d %b')} to {end.strftime('%d %b')}")]
    body += digest
    body.append(("WATCHING", f"{len(WATCHLIST)} large caps"))

    telegram(box("WEEK AHEAD - EARNINGS", body,
                 footer="You cannot beat the market to a release. "
                        "This is so you are not holding into one blind."))

    save_json(STATE_FILE, {"last_sent": datetime.now(timezone.utc)
                           .isoformat(timespec="seconds"),
                           "count": len(rows)})
    log(f"earnings digest sent, {len(rows)} entries")


def connectivity_test():
    print("=== EARNINGS CONNECTIVITY TEST ===")
    day = datetime.now(timezone.utc).date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)

    print(f"\n[Nasdaq] calendar for {day}")
    rows = fetch_nasdaq(day, debug=True)
    if rows is None:
        print("  FAILED. Nasdaq blocked the request or changed its format.")
        print("  Fallback: add a free FINNHUB_KEY secret.")
    else:
        print(f"  OK. {len(rows)} watchlist companies reporting that day.")
        for r in rows[:5]:
            print(f"    {r['symbol']:6} {r['name']}")

    print("\n[Nasdaq] full week probe")
    d = datetime.now(timezone.utc).date()
    d += timedelta(days=(7 - d.weekday()) % 7 or 0)
    total = 0
    for i in range(5):
        day_i = d + timedelta(days=i)
        got = fetch_nasdaq(day_i)
        n = len(got) if got is not None else 0
        total += n
        print(f"    {day_i.strftime('%a %d %b')}: {n} watchlist companies")
        time.sleep(0.6)
    print(f"    week total: {total}")

    print(f"\n[Finnhub] key present: {'yes' if FINNHUB_KEY else 'no'}")
    if FINNHUB_KEY:
        fh = fetch_finnhub(day, day + timedelta(days=5))
        print("  FAILED." if fh is None else f"  OK. {len(fh)} entries.")

    print("\n=== END ===")


def main():
    STATE_DIR.mkdir(exist_ok=True)
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        connectivity_test()
        return
    run_digest()


if __name__ == "__main__":
    main()
