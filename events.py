#!/usr/bin/env python3
"""
events.py - US economic calendar, weekly digest

Sends one message every Monday morning listing the US economic events for the
coming week: CPI, PPI, PCE, payrolls, FOMC, jobless claims and the rest.

Source is the JSON feed behind the Forex Factory calendar widget rather than
the calendar page itself, which sits behind Cloudflare and would need a
browser to read.

You will not beat anyone to a data release. The value is knowing what is
scheduled so you are not holding a position into a print you forgot about.

Run modes:
    python events.py           send the weekly digest
    python events.py test      connectivity check, sends nothing
"""

import os
import re
import sys
import json
from datetime import datetime, timezone, timedelta

import requests

from monitor import box, log, telegram, load_json, save_json, STATE_DIR

USER_AGENT = os.environ.get("SEC_USER_AGENT", "FilingsMonitor")

# Forex Factory's widget feed. Same data as the calendar page, without the
# Cloudflare challenge.
THIS_WEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEXT_WEEK = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"

COUNTRY = "USD"          # US only, as requested

# "High" alone is roughly 8-12 events a week. Add "Medium" for around 25.
IMPACTS = {"High"}

# Always include these regardless of the impact rating the feed assigns.
ALWAYS = [
    "fomc", "federal funds", "interest rate", "cpi", "core cpi", "ppi",
    "core pce", "non-farm", "nonfarm", "unemployment rate", "gdp",
    "retail sales", "ism ", "powell", "fed chair",
    "jobless claims", "unemployment claims", "employment change",
    "consumer sentiment", "durable goods", "treasury", "beige book",
]

SGT = timezone(timedelta(hours=8))
STATE_FILE = STATE_DIR / "events_state.json"

session = requests.Session()


def fetch_week(url):
    try:
        r = session.get(url, timeout=30, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
        })
    except requests.RequestException as e:
        log(f"events: unreachable :: {e}")
        return None
    if r.status_code != 200:
        log(f"events: HTTP {r.status_code}")
        return None
    try:
        data = r.json()
    except ValueError:
        log("events: non-JSON response")
        return None
    return data if isinstance(data, list) else None


def wanted(ev):
    if (ev.get("country") or "").upper() != COUNTRY:
        return False
    title = (ev.get("title") or "").lower()
    if any(k in title for k in ALWAYS):
        return True
    return (ev.get("impact") or "") in IMPACTS


def to_sgt(ev):
    """Feed dates are ISO with an offset. Returns (datetime_sgt, all_day)."""
    raw = ev.get("date") or ""
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, True
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    local = d.astimezone(SGT)
    # The feed uses midnight for all-day or tentative items.
    all_day = local.hour == 0 and local.minute == 0
    return local, all_day


def clean_title(t):
    t = re.sub(r"\s+", " ", str(t)).strip()
    t = re.sub(r"\s*\(.*?\)\s*$", "", t)        # trailing (MoM), (YoY)
    return t


def build(events):
    """Group by Singapore day, sorted."""
    by_day = {}
    for ev in events:
        when, all_day = to_sgt(ev)
        if when is None:
            continue
        by_day.setdefault(when.date(), []).append((when, all_day, ev))

    rows = []
    for day in sorted(by_day):
        items = sorted(by_day[day], key=lambda x: x[0])
        label = day.strftime("%a %d %b")
        for i, (when, all_day, ev) in enumerate(items[:6]):
            title = clean_title(ev.get("title"))
            time_txt = "" if all_day else when.strftime("%H:%M")
            forecast = str(ev.get("forecast") or "").strip()
            extra = f"  fc {forecast}" if forecast else ""
            rows.append((label if i == 0 else "",
                         f"{time_txt:>5}  {title[:34]}{extra}".strip()))
        if len(items) > 6:
            rows.append(("", f"       +{len(items) - 6} more"))
    return rows


def run_digest():
    now = datetime.now(SGT)
    # Sent Monday morning SGT, so "this week" is the week ahead.
    data = fetch_week(THIS_WEEK)
    label = "this week"
    if data is None:
        data = fetch_week(NEXT_WEEK)
        label = "next week"

    if data is None:
        telegram(box("US EVENTS - no data", [
            ("PRIORITY", "MEDIUM"),
            ("PROBLEM", "Calendar feed returned nothing"),
            ("LIKELY CAUSE", "Feed URL moved or blocked"),
            ("ACTION", "Run the diagnostics workflow"),
        ]))
        return

    events = [e for e in data if wanted(e)]
    rows = build(events)

    if not rows:
        log("events: no US events matched this week")
        telegram(box("WEEK AHEAD - US EVENTS", [
            ("WEEK", now.strftime("%d %b")),
            ("STATUS", "No high impact US events scheduled"),
        ]))
        return

    header = [("WEEK", f"{label}, from {now.strftime('%d %b')}"),
              ("EVENTS", f"{len(events)} US, high impact"),
              ("TIMES", "Singapore (SGT)"),
              ("", "")]

    telegram(box("WEEK AHEAD - US EVENTS", header + rows,
                 footer="You cannot beat a data release. This is so you are "
                        "not holding into one you forgot about."))

    save_json(STATE_FILE, {"sent": datetime.now(timezone.utc)
                           .isoformat(timespec="seconds"),
                           "count": len(events)})
    log(f"events digest sent, {len(events)} events")


def connectivity_test():
    print("=== US EVENTS CONNECTIVITY TEST ===")
    for name, url in (("this week", THIS_WEEK), ("next week", NEXT_WEEK)):
        data = fetch_week(url)
        if data is None:
            print(f"  {name:10} FAILED")
            continue
        us = [e for e in data if (e.get("country") or "").upper() == COUNTRY]
        high = [e for e in us if wanted(e)]
        print(f"  {name:10} OK. {len(data)} total, {len(us)} US, "
              f"{len(high)} matching filter")
        for e in high[:6]:
            when, all_day = to_sgt(e)
            t = "all day" if all_day else (when.strftime("%a %H:%M")
                                           if when else "?")
            print(f"      {t:12} {clean_title(e.get('title'))[:40]:42} "
                  f"impact={e.get('impact')}")
    print("\n=== END ===")


def main():
    STATE_DIR.mkdir(exist_ok=True)
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        connectivity_test()
        return
    run_digest()


if __name__ == "__main__":
    main()
