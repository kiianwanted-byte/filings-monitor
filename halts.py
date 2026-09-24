#!/usr/bin/env python3
"""
halts.py - Nasdaq trading halt monitor

Moved off Google Apps Script on 17 Sep 2026. Apps Script gives no way to set
a request timeout, so when the Nasdaq feed stalled under load UrlFetchApp
blocked until Google killed the execution at the 6 minute ceiling. The
MAX_ALERTS_PER_RUN and SOFT_DEADLINE_MS caps never helped because the time
was burned before the item loop started. Three failures in one session, all
during US market hours.

Python sets timeout=15 and a stalled connection dies cleanly.

The trade-off accepted in the move: GitHub cron drifts 5 to 20 minutes, so a
T1 halt alert can arrive late. This is the only real-time signal in the
system, so that cost is real.

Run modes:
    python halts.py           normal run
    python halts.py test      connectivity check, sends nothing
"""

import os
import sys
import json
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import requests

from monitor import (box, log, telegram, load_json, save_json, stamp,
                     find_all, find_one, STATE_DIR)

FEED_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"

SEEN_FILE = STATE_DIR / "halts_seen.json"
STATE_FILE = STATE_DIR / "halts_state.json"

SEEN_MAX = 4000
MAX_ALERTS_PER_RUN = 10

# Every reason code the feed uses, kept for labelling.
HALT_CODES = {
    "T1":  "News pending",
    "T2":  "News released",
    "T12": "Additional information requested",
    "H4":  "Non-compliance with listing requirements",
    "H9":  "Not current in required filings",
    "H10": "SEC trading suspension",
    "H11": "Regulatory concern",
    "D":   "Security deletion or delisting",
    "M":   "Volatility trading pause, market wide",
}

# Only these produce an alert. Trimmed 18 Sep 2026 because volume was too
# high: one run had 25 alertable items out of 68.
#
# T2 was the biggest offender. It means news released, which is the
# resumption of a T1, so every halt was arriving twice for no extra
# information. D fires on routine delistings most days. T12, H4, H9 and H11
# are compliance and filing issues that show up steadily and rarely matter.
#
# Add a code back to this set to widen it again.
ALERT_ON = {
    "T1",    # news pending, the only one that says something is coming
    "H10",   # SEC trading suspension, rare and serious
    "M",     # market wide volatility halt
}

# LUDP and LUDS, ordinary volatility pauses, were never included. On a
# volatile day those alone produce hundreds.

# Of the alerting codes, these buzz the phone rather than landing silently.
HIGH_CODES = {"T1", "H10", "M"}

# Referenced by fetch_feed but was never defined in the deployed file, so
# every run from the morning of 24 Sep 2026 died with a NameError before
# fetching anything. That is what stalled halts for most of a day.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": ("application/rss+xml, application/xml;q=0.9, "
               "text/xml;q=0.9, */*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nasdaqtrader.com/trader.aspx?id=tradehalts",
}

session = requests.Session()


def fetch_feed(tries=2):
    """A stalled connection dies at 15 seconds instead of eating the run."""
    for attempt in range(1, tries + 1):
        try:
            r = session.get(FEED_URL, timeout=15, headers=BROWSER_HEADERS)
        except requests.RequestException as e:
            log(f"halts: request failed ({attempt}/{tries}) :: {e}")
            continue
        ctype = r.headers.get("Content-Type", "")
        if r.status_code != 200:
            log(f"halts: HTTP {r.status_code} ({ctype})")
            continue
        log(f"halts: HTTP 200, {len(r.text)} chars, content-type: {ctype}")
        return r.text
    return None


def txt(el, name):
    """The feed namespaces its fields as ndm:*, so match on local name."""
    found = find_one(el, name)
    return (found.text or "").strip() if found is not None else ""


def parse(xml_text):
    # Accept bytes or str. Bytes are preferred: ElementTree then honours the
    # encoding declared in the document itself.
    if isinstance(xml_text, bytes):
        xml_text = xml_text.lstrip(b"\xef\xbb\xbf")      # strip UTF-8 BOM
    else:
        # A BOM that was mis-decoded arrives as three literal characters, so
        # lstrip of \ufeff alone will not clear it. Cut anything before the
        # first "<" instead, which handles every variant.
        cut = xml_text.find("<")
        if 0 < cut <= 8:
            xml_text = xml_text[cut:]
        xml_text = xml_text.lstrip("\ufeff").lstrip()

    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError) as e:
        # A 200 that is not XML means the server sent something else, usually
        # a block page or a redirect. Show the head of the body so the cause
        # is visible instead of guessed at.
        raw = (xml_text.decode("utf-8", "replace")
               if isinstance(xml_text, bytes) else (xml_text or ""))
        head = raw[:300].replace("\n", " ").strip()
        log(f"halts: unparseable feed :: {e}")
        log(f"halts: body starts with: {head}")
        return None

    out = []
    for item in find_all(root, "item"):
        symbol = txt(item, "IssueSymbol")
        if not symbol:
            continue
        out.append({
            "symbol": symbol,
            "name": txt(item, "IssueName"),
            "code": txt(item, "ReasonCode").upper(),
            "halt_date": txt(item, "HaltDate"),
            "halt_time": txt(item, "HaltTime"),
            "market": txt(item, "Market"),
            "resume_date": txt(item, "ResumptionDate"),
            "resume_time": txt(item, "ResumptionTradeTime"),
            "threshold": txt(item, "PauseThresholdPrice"),
        })
    return out


def key_of(h):
    return f"{h['symbol']}|{h['halt_date']}|{h['halt_time']}|{h['code']}"


def send_one(h):
    code = h["code"]
    priority = "HIGH" if code in HIGH_CODES else "MEDIUM"
    resume = " ".join(x for x in (h["resume_date"], h["resume_time"]) if x)

    rows = [
        ("PRIORITY", priority),
        ("TICKER", h["symbol"]),
        ("COMPANY", h["name"][:38]),
        ("EVENT", f"{code} - {HALT_CODES.get(code, 'Halt')}"),
        ("HALTED", " ".join(x for x in (h["halt_time"], h["halt_date"]) if x)),
        ("MARKET", h["market"]),
        ("THRESHOLD", h["threshold"]),
        ("RESUME", resume or "not yet announced"),
    ]

    telegram(box("TRADING HALT", rows,
                 link="https://www.nasdaqtrader.com/trader.aspx?id=tradehalts",
                 footer=("T1 means news is pending. Watch for the release."
                         if code == "T1" else "")),
             silent=(priority != "HIGH"))


def send_batch(halts):
    """Several at once usually means one issuer with multiple securities."""
    rows = [(h["symbol"], f"{h['code']}  {h['name'][:26]}")
            for h in halts[:20]]
    if len(halts) > 20:
        rows.append(("...", f"+{len(halts) - 20} more"))

    telegram(box(f"{len(halts)} TRADING HALTS", rows,
                 link="https://www.nasdaqtrader.com/trader.aspx?id=tradehalts",
                 footer="Grouped because several halted at once. Multiple "
                        "tickers of one issuer halt together."),
             silent=not any(h["code"] in HIGH_CODES for h in halts))


def run():
    xml_text = fetch_feed()
    if xml_text is None:
        log("halts: feed unreachable this run")
        stamp("halts_state.json", error="feed unreachable")
        return

    halts = parse(xml_text)
    if halts is None:
        stamp("halts_state.json", error="feed not parseable as XML")
        return

    stamp("halts_state.json", items_in_feed=len(halts))

    seen = load_json(SEEN_FILE, [])
    seen_set = set(seen)

    fresh, new_keys = [], []
    for h in halts:
        k = key_of(h)
        if k in seen_set:
            continue
        seen_set.add(k)
        new_keys.append(k)
        if h["code"] in ALERT_ON:
            fresh.append(h)

    if fresh:
        if len(fresh) <= 3:
            for h in fresh[:MAX_ALERTS_PER_RUN]:
                send_one(h)
        else:
            send_batch(fresh[:MAX_ALERTS_PER_RUN * 5])

    seen.extend(new_keys)
    save_json(SEEN_FILE, seen[-SEEN_MAX:])

    log(f"halts: {len(halts)} in feed, {len(new_keys)} new, "
        f"{len(fresh)} alertable")


def connectivity_test():
    print("=== NASDAQ HALTS CONNECTIVITY TEST ===")
    et = (datetime.now(timezone.utc).hour - 4) % 24
    print(f"  approx US Eastern: {et:02d}:xx  "
          f"({'market hours' if 9 <= et < 16 else 'outside market hours'})")

    print("\n[variants] what each URL and header set returns")
    variants = [
        ("default headers", FEED_URL, {}),
        ("browser UA", FEED_URL, {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"),
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nasdaqtrader.com/trader.aspx?id=tradehalts",
        }),
        ("http not https", FEED_URL.replace("https://", "http://"), {}),
        ("no www", FEED_URL.replace("www.", ""), {}),
        ("rss.aspx alt feed", "https://www.nasdaqtrader.com/rss.aspx"
                              "?feed=currenthalts", {}),
    ]
    for label, url, hdrs in variants:
        base = {"User-Agent": os.environ.get("SEC_USER_AGENT",
                                             "FilingsMonitor")}
        base.update(hdrs)
        try:
            r = session.get(url, timeout=15, headers=base,
                            allow_redirects=True)
        except requests.RequestException as e:
            print(f"    {label:20} ERROR {str(e)[:60]}")
            continue
        body = r.text or ""
        looks_xml = body.lstrip().startswith("<?xml") or \
            body.lstrip().startswith("<rss")
        print(f"    {label:20} HTTP {r.status_code}  {len(body):7} chars  "
              f"xml={looks_xml}  ct={r.headers.get('Content-Type','')[:28]}")
        if not looks_xml and body:
            print(f"        starts: {body[:110].replace(chr(10), ' ').strip()}")
        if r.history:
            print(f"        redirected via {len(r.history)} hop(s) to {r.url}")

    xml_text = fetch_feed()
    if xml_text is None:
        print("\n  FAILED. Feed unreachable from this runner.")
        print("\n=== END ===")
        return

    halts = parse(xml_text)
    if halts is None:
        print("  FAILED to parse.")
        print("\n=== END ===")
        return
    print(f"  OK. {len(halts)} items in feed ({len(xml_text)} bytes)")

    codes = {}
    for h in halts:
        codes[h["code"]] = codes.get(h["code"], 0) + 1
    print(f"  reason codes: {codes}")

    alertable = [h for h in halts if h["code"] in ALERT_ON]
    print(f"  would alert on {len(alertable)} of them")
    for h in alertable[:5]:
        print(f"      {h['symbol']:8} {h['code']:4} "
              f"{h['halt_time']:10} {h['name'][:34]}")

    state = load_json(STATE_FILE, {})
    print(f"  last successful run: {state.get('last_ok', 'never')}")
    print(f"  seen ledger: {len(load_json(SEEN_FILE, []))} keys")
    print("\n=== END ===")


def main():
    STATE_DIR.mkdir(exist_ok=True)
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        connectivity_test()
        return
    run()


if __name__ == "__main__":
    main()
