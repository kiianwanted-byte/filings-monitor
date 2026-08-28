#!/usr/bin/env python3
"""
trump.py - OGE Form 278-T monitor for Donald J. Trump

The 278-T is the executive branch periodic transaction report. It discloses
every purchase, sale or exchange of a security over $1,000 made on behalf of
the filer, their spouse or a dependent child.

Two things make this worth automating despite the lag:

  1. Trump files fixed income and equity transactions as SEPARATE documents,
     so the bond filing can be ignored wholesale rather than filtered row by
     row.
  2. Filings are large and batched. Aggregating by ticker turns a thousand
     rows into a readable picture of what was accumulated and what was sold.

What it is NOT: a market moving signal. These are trustee managed accounts,
the filings do not indicate the filer directed any trade, and the disclosure
arrives one to four months after the fact. Treat it as portfolio activity
connected to the president, not as his stock picks.

Run modes:
    python trump.py           normal run
    python trump.py test      connectivity and parser check, sends nothing
"""

import io
import os
import re
import csv
import sys
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from monitor import (
    box, log, telegram, money, dmy, days_between,
    load_json, save_json, append_csv, STATE_DIR, DATA_DIR,
)

# ---------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------

USER_AGENT = os.environ.get("SEC_USER_AGENT", "FilingsMonitor")

# Disclosure bands jump from $500,001-$1M straight to $1,000,001-$5M, so a
# $1M floor is a natural cut point rather than an arbitrary one.
MIN_AMOUNT = 1_000_001
MAX_LINES_IN_MESSAGE = 15

SEEN_FILE = STATE_DIR / "trump_seen.json"
TRADES_CSV = DATA_DIR / "trump_trades.csv"

TRADES_HEADER = ["timestamp", "filing_date", "ticker", "asset", "action",
                 "amount_low", "amount_high", "trade_date", "lag_days", "link"]

# OGE publishes through a Lotus Notes application. These are the entry points
# worth trying; the diagnostic reports which of them actually respond.
INDEX_CANDIDATES = [
    "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index?OpenView",
    "https://extapps2.oge.gov/201/Presiden.nsf/PAS%20Index?OpenView",
    "https://extapps2.oge.gov/201/Presiden.nsf/Legacy+Index?OpenView",
]

session = requests.Session()
session.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})


# ---------------------------------------------------------------
# Row parsing
# ---------------------------------------------------------------

# A 278-T row ends with: transaction date, a capital gains Yes/No flag, and
# an amount band. That tail is the only reliably contiguous part, same trick
# that made the House parser work.
ROW_ANCHOR_RE = re.compile(
    r"(?P<tdate>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?:(?P<ndate>\d{1,2}/\d{1,2}/\d{4})\s+)?"
    r"(?:(?P<gains>No|Yes)\s+)?"
    r"\$(?P<low>[\d,]+)\s*[-\u2013\u2014]\s*[^$]{0,60}?\$(?P<high>[\d,]+)")

TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})\)")

ACTION_RE = re.compile(r"\b(purchase|sale|sold|bought|exchange)\b", re.IGNORECASE)
ACTION_CODE_RE = re.compile(r"(?<![A-Za-z])([PSE])(?![A-Za-z])")

# Anything that reads like debt. Belt and braces on top of the separate
# fixed income filing, in case the two documents are ever combined.
NON_EQUITY_RE = re.compile(
    r"\b(bond|bonds|note|notes|treasury|municipal|muni|revenue|debenture|"
    r"certificate of deposit|money market|mortgage|obligation|"
    r"authority|school district|county of|city of|state of|"
    r"L\.?L\.?C|L\.?P\.?|limited partnership|"
    r"preferred|fixed income|corporate credit)\b", re.IGNORECASE)


def norm_action(text):
    m = ACTION_RE.search(text)
    if m:
        w = m.group(1).lower()
        if w in ("purchase", "bought"):
            return "BUY"
        if w in ("sale", "sold"):
            return "SELL"
        return "EXCHANGE"
    m = ACTION_CODE_RE.search(text)
    if m:
        return {"P": "BUY", "S": "SELL", "E": "EXCHANGE"}[m.group(1)]
    return ""


def extract_rows(text):
    """Pull transaction rows out of 278-T text. Wrap tolerant."""
    rows = []
    for m in ROW_ANCHOR_RE.finditer(text):
        low = int(m.group("low").replace(",", ""))
        high = int(m.group("high").replace(",", ""))
        if low < 1000:
            continue

        before = text[max(0, m.start() - 200):m.start()]
        after = text[m.end():m.end() + 60]

        # Stop at the previous row so we never inherit its asset or ticker.
        prev = list(ROW_ANCHOR_RE.finditer(before))
        if prev:
            before = before[prev[-1].end():]

        tick = TICKER_RE.search(before) or TICKER_RE.search(after)
        ticker = tick.group(1) if tick else ""
        if ticker in ("ST", "OP", "PS", "RP", "SP", "JT", "DC", "SR", "IRA"):
            ticker = ""

        action = norm_action(before)

        asset = before.split("\n")[-1] if "\n" in before else before
        asset = re.sub(r"\([A-Z][A-Z0-9.\-]{0,6}\)", "", asset)
        asset = re.sub(r"\b(purchase|sale|sold|bought|exchange)\b", "",
                       asset, flags=re.IGNORECASE)
        asset = re.sub(r"\d{1,2}/\d{1,2}/\d{4}", "", asset)
        asset = re.sub(r"\s+", " ", asset).strip(" .,-|")

        try:
            td = datetime.strptime(m.group("tdate"), "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            td = ""

        rows.append({
            "ticker": ticker, "asset": asset[:60], "action": action or "BUY",
            "low": low, "high": high, "trade_date": td,
        })
    return rows


def is_equity(row):
    if not row["ticker"]:
        return False
    if NON_EQUITY_RE.search(row["asset"]):
        return False
    return True


def aggregate(rows):
    """
    Roll up by ticker and direction. Four NVDA buys in one filing are one
    accumulation event, not four alerts. Direction and concentration are
    what matter here, not individual trade size.
    """
    agg = {}
    for r in rows:
        key = (r["ticker"], r["action"])
        a = agg.setdefault(key, {"ticker": r["ticker"], "action": r["action"],
                                 "count": 0, "low": 0, "high": 0,
                                 "first": "", "last": "", "asset": r["asset"]})
        a["count"] += 1
        a["low"] += r["low"]
        a["high"] += r["high"]
        d = r["trade_date"]
        if d:
            if not a["first"] or d < a["first"]:
                a["first"] = d
            if not a["last"] or d > a["last"]:
                a["last"] = d
    return list(agg.values())


# ---------------------------------------------------------------
# PDF text, with OCR fallback
# ---------------------------------------------------------------

def pdf_text(content, max_pages=200):
    """Try text extraction, fall back to OCR when the pages are scans."""
    text = ""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages[:max_pages]:
                text += (page.extract_text() or "") + "\n"
    except Exception as e:
        log(f"pdfplumber failed :: {e}")

    if len(text.strip()) > 400:
        return text, "text"

    log("little or no embedded text, attempting OCR")
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
        images = convert_from_bytes(content, dpi=200, fmt="png")
        ocr = ""
        for img in images[:max_pages]:
            ocr += pytesseract.image_to_string(img) + "\n"
        return ocr, "ocr"
    except ImportError:
        log("OCR libraries not installed, cannot read scanned filing")
        return text, "none"
    except Exception as e:
        log(f"OCR failed :: {e}")
        return text, "none"


# ---------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------

PDF_LINK_RE = re.compile(r'href="([^"]*\.pdf[^"]*)"', re.IGNORECASE)


def find_filings():
    """Returns a list of {url, label} for Trump 278-T documents."""
    for url in INDEX_CANDIDATES:
        try:
            r = session.get(url, timeout=45)
        except requests.RequestException as e:
            log(f"index unreachable {url} :: {e}")
            continue
        if r.status_code != 200:
            log(f"index HTTP {r.status_code} {url}")
            continue

        found = []
        for href in PDF_LINK_RE.findall(r.text):
            full = href if href.startswith("http") else \
                "https://extapps2.oge.gov" + (href if href.startswith("/") else "/" + href)
            label = requests.utils.unquote(full.split("/")[-1])
            if "trump" not in label.lower():
                continue
            if "278-T" not in label and "278T" not in label.upper():
                continue
            found.append({"url": full, "label": label})

        if found:
            log(f"index ok: {url}, {len(found)} Trump 278-T documents")
            return found

    log("no OGE index responded with Trump 278-T links")
    return []


def label_date(label):
    m = re.search(r"(\d{1,2})[.\-](\d{1,2})[.\-](\d{4})", label)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(1)),
                            int(m.group(2))).strftime("%Y-%m-%d")
        except ValueError:
            pass
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", label)
    return m.group(0) if m else ""


# ---------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------

def send_filing(label, link, filed, rows):
    equities = [r for r in rows if is_equity(r)]
    if not equities:
        log(f"{label}: no equity rows, skipping (likely the fixed income filing)")
        return False

    agg = aggregate(equities)
    big = [a for a in agg if a["low"] >= MIN_AMOUNT]
    big.sort(key=lambda a: -a["low"])

    dates = [r["trade_date"] for r in equities if r["trade_date"]]
    period = (f"{dmy(min(dates))} to {dmy(max(dates))}" if dates else "unknown")
    lags = [days_between(d, filed) for d in dates if filed]
    lags = [l for l in lags if l != ""]
    lag_txt = f"{min(lags)} to {max(lags)} days" if lags else "unknown"

    for r in equities:
        append_csv(TRADES_CSV, [
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            filed, r["ticker"], r["asset"], r["action"],
            r["low"], r["high"], r["trade_date"],
            days_between(r["trade_date"], filed) if filed and r["trade_date"] else "",
            link,
        ])

    if not big:
        log(f"{label}: {len(equities)} equity trades but none above the floor")
        return False

    header = [
        ("PRIORITY", "HIGH"),
        ("PERIOD", period),
        ("FILED", dmy(filed)),
        ("LAG", lag_txt),
        ("TRADES", f"{len(equities)} equity, {len(agg)} tickers"),
        ("SHOWN", f"top {min(len(big), MAX_LINES_IN_MESSAGE)} above {money(MIN_AMOUNT)}"),
    ]

    lines = []
    for a in big[:MAX_LINES_IN_MESSAGE]:
        mark = "\U0001F7E2" if a["action"] == "BUY" else \
               "\U0001F534" if a["action"] == "SELL" else ""
        lines.append((f"{a['action'][:4]} {a['ticker']}",
                      f"{a['count']}x   {money(a['low'])} - {money(a['high'])}"
                      + (f"  {mark}" if mark else "")))
    if len(big) > MAX_LINES_IN_MESSAGE:
        lines.append(("...", f"+{len(big) - MAX_LINES_IN_MESSAGE} more tickers"))

    telegram(box("\U0001F4C4 TRUMP 278-T - EQUITY", header + [("", "")] + lines,
                 link=link,
                 footer="Trustee managed accounts. The filing does not indicate "
                        "the filer directed these trades. Full list in "
                        "trump_trades.csv."))
    return True


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------

def process(filing, seen):
    label, url = filing["label"], filing["url"]
    try:
        r = session.get(url, timeout=120)
    except requests.RequestException as e:
        log(f"download failed {label} :: {e}")
        return
    if r.status_code != 200:
        log(f"download HTTP {r.status_code} {label}")
        return

    text, method = pdf_text(r.content)
    if not text.strip():
        log(f"{label}: unreadable")
        return

    rows = extract_rows(text)
    log(f"{label}: {len(rows)} rows via {method}, "
        f"{sum(1 for x in rows if is_equity(x))} equity")
    send_filing(label, url, label_date(label), rows)
    seen.add(url)


def connectivity_test():
    print("=== TRUMP 278-T CONNECTIVITY TEST ===")
    print("\n[OGE] index candidates")
    filings = find_filings()
    if not filings:
        print("  FAILED. No index URL returned Trump 278-T links.")
        print("  Next step: open extapps2.oge.gov in a browser, find the")
        print("  Trump filings page, and send me that URL.")
        print("\n=== END ===")
        return

    print(f"  OK. {len(filings)} documents found. Newest first:")
    for f in filings[:6]:
        print(f"    {label_date(f['label']) or '????'}  {f['label'][:60]}")

    newest = filings[0]
    print(f"\n[Parse] {newest['label'][:60]}")
    try:
        r = session.get(newest["url"], timeout=120)
        text, method = pdf_text(r.content, max_pages=25)
        print(f"  extraction method: {method}, {len(text)} chars")
        rows = extract_rows(text)
        eq = [x for x in rows if is_equity(x)]
        print(f"  rows parsed: {len(rows)}, equity rows: {len(eq)}")
        for a in sorted(aggregate(eq), key=lambda x: -x["low"])[:8]:
            print(f"    {a['action']:5} {a['ticker']:6} {a['count']}x  "
                  f"{money(a['low'])} - {money(a['high'])}")
        if not eq:
            print("  Sample lines containing '$' for tuning:")
            for ln in [l.strip() for l in text.splitlines() if "$" in l][:6]:
                print(f"    {ln[:110]}")
    except Exception as e:
        print(f"  FAILED :: {e}")

    print("\n=== END ===")


def main():
    STATE_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)
    if not TRADES_CSV.exists():
        with TRADES_CSV.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(TRADES_HEADER)

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        connectivity_test()
        return

    seen = set(load_json(SEEN_FILE, []))
    filings = find_filings()
    new = [f for f in filings if f["url"] not in seen]
    log(f"{len(filings)} documents, {len(new)} new")

    for f in new[:4]:          # OCR is slow, cap the work per run
        process(f, seen)
        time.sleep(1)

    save_json(SEEN_FILE, list(seen)[-500:])
    log("trump done")


if __name__ == "__main__":
    main()
