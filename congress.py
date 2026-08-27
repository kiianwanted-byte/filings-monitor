#!/usr/bin/env python3
"""
congress.py - Congressional stock trade monitor

Watches periodic transaction reports (PTRs) filed under the STOCK Act.

  House  - disclosures-clerk.house.gov publishes an annual ZIP containing an
           XML index of every filing. Trade details live in linked PDFs.
  Senate - efdsearch.senate.gov requires accepting a terms page to obtain a
           session cookie before it will return any search results.

Members must file within 45 days of a trade, so every alert here is weeks
old by definition. The filing becoming public IS the event. This puts you
level with everyone else watching, not ahead of them.

Run modes:
    python congress.py            normal run
    python congress.py test       connectivity check, sends nothing
"""

import io
import os
import re
import csv
import sys
import json
import time
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

from monitor import (
    box, esc, log, telegram, money, dmy, days_between,
    load_json, save_json, append_csv, STATE_DIR, DATA_DIR,
)

# ---------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------

USER_AGENT = os.environ.get("SEC_USER_AGENT", "FilingsMonitor")

# Disclosure bands. Members report ranges, never exact figures.
PUSH_MIN_AMOUNT = 50_001       # below this, log only
HIGH_MIN_AMOUNT = 500_001      # at or above this, always push

MAX_FILINGS_PER_RUN = 25
MAX_ALERTS_PER_RUN = 15

SEEN_FILE = STATE_DIR / "congress_seen.json"
HEALTH_FILE = STATE_DIR / "congress_health.json"
TRADES_CSV = DATA_DIR / "congress_trades.csv"

TRADES_HEADER = ["timestamp", "chamber", "member", "ticker", "asset",
                 "action", "amount_low", "amount_high", "trade_date",
                 "filed_date", "lag_days", "priority", "link"]

HOUSE_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.ZIP"
HOUSE_PDF = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
SENATE_HOME = "https://efdsearch.senate.gov/search/home/"
SENATE_SEARCH = "https://efdsearch.senate.gov/search/report/data/"

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

alerts_sent = 0


# ---------------------------------------------------------------
# Amount parsing
# ---------------------------------------------------------------

AMOUNT_RE = re.compile(r"\$([\d,]+)\s*(?:-|–|—|to)\s*\$([\d,]+)")


def parse_amount(text):
    """'$1,001 - $15,000' -> (1001, 15000). Returns (0, 0) if unreadable."""
    if not text:
        return 0, 0
    m = AMOUNT_RE.search(str(text))
    if not m:
        single = re.search(r"\$([\d,]+)", str(text))
        if single:
            v = int(single.group(1).replace(",", ""))
            return v, v
        return 0, 0
    return (int(m.group(1).replace(",", "")),
            int(m.group(2).replace(",", "")))


def amount_label(low, high):
    if not low and not high:
        return "not disclosed"
    if low == high:
        return money(low)
    return f"{money(low)} - {money(high)}"


def classify(low):
    if low >= HIGH_MIN_AMOUNT:
        return "HIGH"
    if low >= PUSH_MIN_AMOUNT:
        return "MEDIUM"
    return "LOW"


def action_label(code):
    """PTR transaction codes."""
    c = str(code).strip().upper()
    return {
        "P": "BUY",
        "S": "SELL",
        "S (PARTIAL)": "SELL (partial)",
        "E": "EXCHANGE",
    }.get(c, c or "unknown")


# ---------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------

def send(chamber, member, party_state, ticker, asset, action,
         low, high, trade_date, filed_date, link, priority):
    global alerts_sent

    lag = days_between(trade_date, filed_date) if trade_date and filed_date else ""

    append_csv(TRADES_CSV, [
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        chamber, member, ticker, asset, action, low, high,
        trade_date, filed_date, lag, priority, link,
    ])

    if priority == "LOW":
        return False
    if alerts_sent >= MAX_ALERTS_PER_RUN:
        return False

    rows = [
        ("PRIORITY", priority),
        ("MEMBER", member),
        ("CHAMBER", chamber + (f"  ({party_state})" if party_state else "")),
        ("TICKER", ticker or "not listed"),
        ("ASSET", asset[:44] if asset else ""),
        ("ACTION", action),
        ("AMOUNT", amount_label(low, high)),
        ("TRADE", dmy(trade_date)),
        ("FILED", dmy(filed_date)),
        ("LAG", f"{lag} days" if lag != "" else "unknown"),
    ]

    telegram(box("CONGRESS TRADE", rows, link=link,
                 footer="Disclosed under the STOCK Act. The trade already "
                        "happened weeks ago."))
    alerts_sent += 1
    return True


def send_parse_failure(chamber, member, filed_date, link):
    """PDF unreadable. Still tell them a filing landed."""
    global alerts_sent
    if alerts_sent >= MAX_ALERTS_PER_RUN:
        return
    rows = [
        ("PRIORITY", "MEDIUM"),
        ("MEMBER", member),
        ("CHAMBER", chamber),
        ("FILED", dmy(filed_date)),
        ("NOTE", "Trade details could not be read automatically"),
    ]
    telegram(box("CONGRESS FILING - open manually", rows, link=link,
                 footer="Usually a scanned or handwritten PTR."))
    alerts_sent += 1


# ---------------------------------------------------------------
# House
# ---------------------------------------------------------------

def fetch_house_index(year):
    """Returns a list of filing dicts from the Clerk's annual ZIP."""
    url = HOUSE_ZIP.format(year=year)
    try:
        r = session.get(url, timeout=90)
    except requests.RequestException as e:
        log(f"house: zip unreachable :: {e}")
        return None
    if r.status_code != 200:
        log(f"house: zip HTTP {r.status_code}")
        return None

    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        xml_name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
        root = ET.fromstring(zf.read(xml_name))
    except Exception as e:
        log(f"house: zip unreadable :: {e}")
        return None

    out = []
    for m in root.findall(".//Member"):
        def g(tag):
            el = m.find(tag)
            return (el.text or "").strip() if el is not None else ""

        # Only periodic transaction reports. Annual reports are a different beast.
        if g("FilingType") != "P":
            continue

        doc_id = g("DocID")
        if not doc_id:
            continue

        name = " ".join(x for x in [g("First"), g("Last")] if x).strip()
        if g("Suffix"):
            name += " " + g("Suffix")

        out.append({
            "doc_id": doc_id,
            "member": name or g("Last"),
            "state": g("StateDst"),
            "filed": g("FilingDate"),
            "year": g("Year") or str(year),
        })
    return out


# A House PTR row looks like:
#   Bloom Energy Corporation (BE) [ST] P 07/24/2026 08/19/2026 $1,000,001 - $5,000,000
# The [ST] asset-type tag sits between the ticker and the transaction code,
# and is sometimes absent. Case sensitive on purpose: a lowercase "p" inside
# a company name must not be mistaken for a purchase code.
HOUSE_ROW_RE = re.compile(
    r"(?P<asset>[A-Za-z0-9][^\n]{2,80}?)\s*"
    r"(?:\((?P<ticker>[A-Z][A-Z0-9.\-]{0,6})\)\s*)?"
    r"(?:\[[A-Za-z]{1,3}\]\s*)?"
    r"(?P<action>S \(partial\)|[PSE])\s+"
    r"(?P<tdate>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<ndate>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<amount>\$[\d,]+\s*[-\u2013\u2014]\s*\$[\d,]+)")


def parse_house_pdf(doc_id, year):
    """Extract trade rows from a House PTR PDF. Returns [] if unreadable."""
    try:
        import pdfplumber
    except ImportError:
        log("house: pdfplumber not installed")
        return []

    url = HOUSE_PDF.format(year=year, doc=doc_id)
    try:
        r = session.get(url, timeout=60)
    except requests.RequestException as e:
        log(f"house: pdf unreachable {doc_id} :: {e}")
        return []
    if r.status_code != 200:
        return []

    text = ""
    try:
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            for page in pdf.pages[:12]:
                text += (page.extract_text() or "") + "\n"
    except Exception as e:
        log(f"house: pdf parse failed {doc_id} :: {e}")
        return []

    if not text.strip():
        return []      # scanned document, would need OCR

    rows = []
    for m in HOUSE_ROW_RE.finditer(text):
        low, high = parse_amount(m.group("amount"))
        asset = re.sub(r"\s+", " ", m.group("asset")).strip(" .-")
        try:
            td = datetime.strptime(m.group("tdate"), "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            td = ""
        rows.append({
            "asset": asset,
            "ticker": (m.group("ticker") or "").upper(),
            "action": action_label(m.group("action")),
            "low": low, "high": high, "trade_date": td,
        })
    return rows


def run_house(seen, health):
    year = datetime.now(timezone.utc).year
    index = fetch_house_index(year)
    if index is None:
        return
    health["house"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    new = [f for f in index if f"H|{f['doc_id']}" not in seen]
    log(f"house: {len(index)} PTRs in index, {len(new)} new")

    # Newest first so a backlog does not bury today's filings.
    new.sort(key=lambda f: f.get("filed", ""), reverse=True)

    for f in new[:MAX_FILINGS_PER_RUN]:
        seen.add(f"H|{f['doc_id']}")
        link = HOUSE_PDF.format(year=f["year"], doc=f["doc_id"])

        try:
            filed = datetime.strptime(f["filed"], "%m/%d/%Y").strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            filed = f.get("filed", "")

        trades = parse_house_pdf(f["doc_id"], f["year"])
        time.sleep(0.4)

        if not trades:
            send_parse_failure("House", f["member"], filed, link)
            continue

        for t in trades:
            send("House", f["member"], f["state"], t["ticker"], t["asset"],
                 t["action"], t["low"], t["high"], t["trade_date"], filed,
                 link, classify(t["low"]))


# ---------------------------------------------------------------
# Senate
# ---------------------------------------------------------------

def senate_session():
    """eFD makes you accept a terms page before it returns anything."""
    try:
        r = session.get(SENATE_HOME, timeout=30)
        if r.status_code != 200:
            log(f"senate: home HTTP {r.status_code}")
            return None
        token = session.cookies.get("csrftoken")
        if not token:
            m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', r.text)
            token = m.group(1) if m else None
        if not token:
            log("senate: no csrf token found")
            return None

        r2 = session.post(SENATE_HOME, data={
            "prohibition_agreement": "1",
            "csrfmiddlewaretoken": token,
        }, headers={"Referer": SENATE_HOME}, timeout=30)
        if r2.status_code not in (200, 302):
            log(f"senate: agreement HTTP {r2.status_code}")
            return None
        return session.cookies.get("csrftoken") or token
    except requests.RequestException as e:
        log(f"senate: session failed :: {e}")
        return None


def run_senate(seen, health):
    token = senate_session()
    if not token:
        return

    start = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%m/%d/%Y")
    try:
        r = session.post(SENATE_SEARCH, data={
            "start": "0",
            "length": "100",
            "report_types": "[11]",           # periodic transaction reports
            "filer_types": "[]",
            "submitted_start_date": start,
            "submitted_end_date": "",
            "candidate_state": "",
            "senator_state": "",
            "office_id": "",
            "first_name": "",
            "last_name": "",
            "csrfmiddlewaretoken": token,
        }, headers={"Referer": SENATE_HOME,
                    "X-CSRFToken": token,
                    "X-Requested-With": "XMLHttpRequest"}, timeout=45)
    except requests.RequestException as e:
        log(f"senate: search failed :: {e}")
        return

    if r.status_code != 200:
        log(f"senate: search HTTP {r.status_code}")
        return

    try:
        rows = r.json().get("data", [])
    except ValueError:
        log("senate: search returned non-JSON")
        return

    health["senate"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log(f"senate: {len(rows)} PTRs in window")

    count = 0
    for row in rows:
        if count >= MAX_FILINGS_PER_RUN:
            break
        # Columns: first, last, office, report link (HTML anchor), date
        try:
            first, last, office, anchor, filed_raw = row[:5]
        except (ValueError, TypeError):
            continue

        m = re.search(r'href="([^"]+)"', str(anchor))
        href = m.group(1) if m else ""
        if not href:
            continue
        link = "https://efdsearch.senate.gov" + href
        key = "S|" + href
        if key in seen:
            continue
        seen.add(key)
        count += 1

        member = re.sub(r"<[^>]+>", "", f"{first} {last}").strip()
        try:
            filed = datetime.strptime(filed_raw.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            filed = str(filed_raw)

        # eFD renders trades as an HTML table on the report page.
        try:
            pr = session.get(link, timeout=30)
            html = pr.text if pr.status_code == 200 else ""
        except requests.RequestException:
            html = ""

        trades = parse_senate_html(html)
        time.sleep(0.4)

        if not trades:
            send_parse_failure("Senate", member, filed, link)
            continue

        for t in trades:
            send("Senate", member, re.sub(r"<[^>]+>", "", str(office))[:24],
                 t["ticker"], t["asset"], t["action"], t["low"], t["high"],
                 t["trade_date"], filed, link, classify(t["low"]))


SEN_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
SEN_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)


def parse_senate_html(html):
    if not html:
        return []
    out = []
    for row_html in SEN_ROW_RE.findall(html):
        cells = [re.sub(r"<[^>]+>", " ", c) for c in SEN_CELL_RE.findall(row_html)]
        cells = [re.sub(r"\s+", " ", c).strip() for c in cells]
        if len(cells) < 6:
            continue
        joined = " ".join(cells)
        if "$" not in joined:
            continue

        low, high = parse_amount(joined)
        if not low:
            continue

        ticker = ""
        tm = re.search(r"\b([A-Z]{1,5})\b(?=\s|$)", cells[3] if len(cells) > 3 else "")
        for c in cells:
            tm2 = re.fullmatch(r"[A-Z]{1,5}", c.strip())
            if tm2:
                ticker = c.strip()
                break

        action = ""
        for c in cells:
            cl = c.strip().lower()
            if cl in ("purchase", "sale", "sale (partial)", "sale (full)", "exchange"):
                action = ("BUY" if cl == "purchase"
                          else "EXCHANGE" if cl == "exchange" else "SELL")
                break
        if not action:
            continue

        trade_date = ""
        dm = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", joined)
        if dm:
            try:
                trade_date = datetime.strptime(dm.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
            except ValueError:
                pass

        asset = ""
        for c in cells:
            if len(c) > 8 and "$" not in c and "/" not in c:
                asset = c
                break

        out.append({"asset": asset, "ticker": ticker, "action": action,
                    "low": low, "high": high, "trade_date": trade_date})
    return out


# ---------------------------------------------------------------
# Connectivity test
# ---------------------------------------------------------------

def connectivity_test():
    print("=== CONGRESS CONNECTIVITY TEST ===")
    year = datetime.now(timezone.utc).year

    print(f"\n[House] annual index for {year}")
    idx = fetch_house_index(year)
    if idx is None:
        print("  FAILED. The ZIP URL or format may have changed.")
    else:
        print(f"  OK. {len(idx)} periodic transaction reports in the index.")
        if idx:
            newest = sorted(idx, key=lambda f: f.get("filed", ""))[-1]
            print(f"  Newest: {newest['member']} filed {newest['filed']} "
                  f"doc {newest['doc_id']}")
            print("  Trying to read that PDF...")
            rows = parse_house_pdf(newest["doc_id"], newest["year"])
            print(f"  Parsed {len(rows)} trade rows.")
            for r in rows[:3]:
                print(f"    {r['ticker'] or '-':6} {r['action']:6} "
                      f"{amount_label(r['low'], r['high'])}  {r['trade_date']}")

    print("\n[Senate] eFD session")
    tok = senate_session()
    print("  OK, session established." if tok else "  FAILED to establish session.")

    print("\n=== END ===")


# ---------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------

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
    health = load_json(HEALTH_FILE, {})

    try:
        run_house(seen, health)
    except Exception as e:
        log(f"house run failed :: {e}")

    try:
        run_senate(seen, health)
    except Exception as e:
        log(f"senate run failed :: {e}")

    seen_list = list(seen)[-30_000:]
    save_json(SEEN_FILE, seen_list)
    save_json(HEALTH_FILE, health)
    log(f"congress done, alerts sent {alerts_sent}")


if __name__ == "__main__":
    main()
