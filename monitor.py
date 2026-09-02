#!/usr/bin/env python3
"""
monitor.py - SEC EDGAR filings monitor

Runs on GitHub Actions. Polls EDGAR for new filings, parses them,
sends filtered alerts to Telegram, and keeps its state in the repo.

Watches: Form 4, Form 144, 8-K (selected items), NT 10-K, NT 10-Q, SC 13D
Trading halts are handled separately by the Apps Script project.
"""

import os
import re
import csv
import json
import time
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

# ---------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")

# Prefixed to every outbound message. All bots post to one channel and
# every post shows as "SCOUT", so this is the only source attribution.
SOURCE_TAG = "\U0001F3DB\uFE0F"

# Only alerts at or above this level reach Telegram.
# Everything else is still written to the CSV for later review.
# Options: "HIGH", "MEDIUM", "LOW"
ALERT_MIN_PRIORITY = "MEDIUM"
PRIORITY_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

# Alert thresholds for insider buys
MIN_TRADE_VALUE = 250_000        # was 100k, raised to cut noise
MIN_HOLDING_CHANGE_PCT = 20      # was 10
MIN_MARKET_CAP = 300_000_000

# Form 144 sell notices are very common and mostly routine.
FORM144_MIN_VALUE = 5_000_000

# ---- 13D / 13G stake disclosures ----
# These are the only filings in the system with a real timing gap. A stake
# crossing 5% must be disclosed within days, and small caps routinely sit
# unnoticed for a week or more before anyone writes about them.
#
# The market cap floor does NOT apply here on purpose. The interesting ones
# are micro caps, which the $300M floor would have excluded.
STAKE_MIN_PERCENT = 5.0

# Routine institutional filings. Vanguard crossing 5% in something is not
# news; it is index rebalancing. Anyone NOT on this list is worth a look.
INSTITUTIONAL = [
    "vanguard", "blackrock", "state street", "fmr llc", "fidelity",
    "geode capital", "t. rowe price", "t rowe price", "capital research",
    "capital world", "capital international", "wellington", "invesco",
    "northern trust", "bank of new york", "bny mellon", "jpmorgan",
    "j.p. morgan", "morgan stanley", "goldman sachs", "ubs group",
    "credit suisse", "deutsche bank", "barclays", "hsbc", "citigroup",
    "charles schwab", "dimensional fund", "franklin resources",
    "amvescap", "aberdeen", "janus henderson", "nuveen", "teachers insurance",
    "tiaa", "prudential", "allianz", "axa ", "legal & general",
    "norges bank", "california public employees", "vaneck", "wisdomtree",
    "susquehanna", "citadel advisors", "point72", "millennium management",
    "two sigma", "renaissance technologies", "de shaw", "d. e. shaw",
    "aqr capital", "bridgewater", "man group", "marshall wace",
    "royal bank of canada", "toronto dominion", "bank of montreal",
    "sumitomo", "mitsubishi ufj", "nomura", "mizuho",
]


def is_institutional(name):
    n = (name or "").lower()
    return any(inst in n for inst in INSTITUTIONAL)

# Cluster detection
CLUSTER_WINDOW_DAYS = 14
CLUSTER_MIN_INSIDERS = 2

# 8-K item codes. HIGH ones push to Telegram, LOW ones only hit the CSV.
EIGHTK_ITEMS = {
    "1.01": "Material definitive agreement",
    "1.02": "Termination of material agreement",
    "1.03": "Bankruptcy or receivership",
    "2.04": "Triggering event accelerating debt",
    "3.01": "Delisting notice or listing rule failure",
    "4.01": "Change in certifying accountant",
    "4.02": "NON-RELIANCE ON PRIOR FINANCIALS",
    "5.02": "Departure or election of directors or officers",
}

# These are the distress and structural signals. Rare, and they matter.
EIGHTK_HIGH = {"1.03", "2.04", "3.01", "4.01", "4.02"}
# These fire constantly and are usually routine corporate housekeeping.
EIGHTK_LOW = {"1.01", "1.02", "5.02"}

# Trades only. 8-K, NT and 13D were the bulk of the noise and none of them
# are a buy or a sell. Add them back to this list to re-enable.
FORMS = ["4", "144", "SC 13D", "SC 13G"]

# A filing with no ticker is not actionable, so it is logged but never pushed.
REQUIRE_TICKER = True
FEED_COUNT = 100

MAX_FILINGS_PER_RUN = 120
MAX_ALERTS_PER_RUN = 25
SEEN_MAX = 20_000
SEC_DELAY = 0.15          # SEC allows 10 requests per second

# A newly added feed cannot be "broken" before it has had time to report.
# 13G in particular clusters around quarter ends and can be quiet for days.
FEED_GRACE_HOURS = 72

# Paths inside the repo
ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
DATA_DIR = ROOT / "data"
SEEN_FILE = STATE_DIR / "seen.json"
QUEUE_FILE = STATE_DIR / "queue.json"
HEALTH_FILE = STATE_DIR / "health.json"
FIRST_SEEN_FILE = STATE_DIR / "first_seen.json"
ALERTS_CSV = DATA_DIR / "alerts.csv"
BUYS_CSV = DATA_DIR / "buys.csv"

ALERTS_HEADER = ["timestamp", "type", "ticker", "company", "headline",
                 "value_usd", "lag_days", "link", "priority"]
BUYS_HEADER = ["timestamp", "trade_date", "ticker", "company", "insider",
               "role", "shares", "price", "value_usd", "holding_change_pct",
               "accession"]

session = requests.Session()
alerts_sent = 0


# ---------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def ensure_files():
    STATE_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)
    for path, header in ((ALERTS_CSV, ALERTS_HEADER), (BUYS_CSV, BUYS_HEADER)):
        if not path.exists():
            with path.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(header)
    for path, default in ((SEEN_FILE, []), (QUEUE_FILE, []), (HEALTH_FILE, {})):
        if not path.exists():
            path.write_text(json.dumps(default))


def prune_csv(path, header, days, date_col="timestamp"):
    """Keep the file to a rolling window so the repo and the reads stay small."""
    if not path.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                d = datetime.fromisoformat(row[date_col])
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
            except (ValueError, KeyError, TypeError):
                kept.append(row)      # unparseable date, keep it rather than lose it
                continue
            if d >= cutoff:
                kept.append(row)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(kept)


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, indent=0))


def append_csv(path, row):
    with path.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)


def money(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "n/a"
    if n >= 1e9:
        return f"${n/1e9:.2f}B"
    if n >= 1e6:
        return f"${n/1e6:.2f}M"
    if n >= 1e3:
        return f"${round(n/1e3):,.0f}K"
    return f"${n:.0f}"


def dmy(date_str):
    if not date_str:
        return ""
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
        return f"{d.day} {d.strftime('%b')}"
    except ValueError:
        return str(date_str)


def days_between(a, b):
    try:
        d1 = datetime.strptime(str(a)[:10], "%Y-%m-%d")
        d2 = datetime.strptime(str(b)[:10], "%Y-%m-%d")
        return (d2 - d1).days
    except ValueError:
        return ""


def num(n):
    try:
        return f"{float(str(n).replace(',', '')):,.0f}"
    except (TypeError, ValueError):
        return str(n)


def esc(s):
    """Telegram HTML mode requires these three escaped."""
    return (str(s).replace("&", "&amp;")
                  .replace("<", "&lt;")
                  .replace(">", "&gt;"))


def box(title, rows, link="", footer=""):
    """
    Renders a monospace block so labels line up:

        INSIDER BUY
        TICKER   NVDA
        PERSON   Colette Kress
        ACTION   BUY (open market)
    """
    rows = [(k, v) for k, v in rows if v not in ("", None)]
    width = max((len(k) for k, _ in rows), default=0)
    body = "\n".join(f"{k.ljust(width)}  {v}" for k, v in rows)
    out = f"{esc(title)}\n<pre>{esc(body)}</pre>"
    if footer:
        out += f"\n{esc(footer)}"
    if link:
        out += f"\n{esc(link)}"
    return out


# ---------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------

def fetch(url, tries=3, is_sec=True):
    headers = {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    wait = 1.0
    for attempt in range(1, tries + 1):
        if is_sec:
            time.sleep(SEC_DELAY)
        try:
            r = session.get(url, headers=headers, timeout=30)
        except requests.RequestException as e:
            if attempt == tries:
                log(f"  fetch failed after {tries}: {url} :: {e}")
                return None
            time.sleep(wait)
            wait *= 2
            continue

        if r.status_code == 200:
            return r.text
        if r.status_code == 403:
            log(f"  BLOCKED 403: {url}")
            return None
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == tries:
                log(f"  HTTP {r.status_code} after retries: {url}")
                return None
            retry_after = r.headers.get("Retry-After")
            time.sleep(min(float(retry_after) if retry_after else wait, 20))
            wait *= 2
            continue
        log(f"  HTTP {r.status_code}: {url}")
        return None
    return None


def telegram(text, silent=True):
    """
    Single choke point for every outbound message.

    SOURCE_TAG is prefixed here so no formatter needs to know about it.
    silent=True means the message lands in the channel without buzzing
    the phone. Only genuinely time-sensitive alerts pass silent=False.
    """
    global alerts_sent
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("  telegram not configured, printing instead")
        print(text)
        return False
    text = f"{SOURCE_TAG} {text}"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = session.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }, timeout=20)
        if r.status_code != 200:
            log(f"  telegram {r.status_code}: {r.text[:200]}")
            return False
        time.sleep(0.35)
        return True
    except requests.RequestException as e:
        log(f"  telegram error: {e}")
        return False


def send_alert(kind, ticker, company, headline, value, lag, link, message,
               priority="HIGH"):
    """
    Everything is logged to the CSV. Only alerts at or above
    ALERT_MIN_PRIORITY are pushed to Telegram.
    """
    global alerts_sent
    append_csv(ALERTS_CSV, [
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        kind, ticker, company, headline,
        int(value) if value else "", lag, link, priority,
    ])

    if PRIORITY_RANK.get(priority, 1) < PRIORITY_RANK[ALERT_MIN_PRIORITY]:
        return False

    telegram(message, silent=(priority != "HIGH"))
    alerts_sent += 1
    return True


# ---------------------------------------------------------------
# XML helpers, namespace agnostic
# ---------------------------------------------------------------

def local(tag):
    return tag.split("}")[-1]


def find_all(root, name):
    return [el for el in root.iter() if local(el.tag) == name]


def find_one(root, name):
    for el in root.iter():
        if local(el.tag) == name:
            return el
    return None


def val(root, name):
    """Text of the first descendant with this name, unwrapping <value>."""
    el = find_one(root, name)
    if el is None:
        return ""
    for child in el:
        if local(child.tag) == "value":
            return (child.text or "").strip()
    return (el.text or "").strip()


def is_true(v):
    return str(v).strip().lower() in ("1", "true", "y")


# ---------------------------------------------------------------
# Stage 1: discovery
# ---------------------------------------------------------------

def feed_url(form, count=None):
    """
    EDGAR's browse-edgar CGI does not decode %20 in the type parameter, so a
    form name containing a space ("SC 13D") silently returns an empty feed
    rather than an error. It wants a literal '+' instead.
    """
    return ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
            f"&type={form.replace(' ', '+')}"
            f"&company=&dateb=&owner=include"
            f"&count={count or FEED_COUNT}&output=atom")


def daily_index_entries(form, days_back=2):
    """
    Fallback discovery via EDGAR's daily index.

    getcurrent does not serve every form type, but the daily index lists
    every filing EDGAR received, one line per filing, fixed width:

        SC 13G   GoPro, Inc.   1500435   2026-08-20   edgar/data/...

    Slower to appear than the live feed, but complete and reliable.
    """
    out = []
    today = datetime.now(timezone.utc).date()
    for back in range(days_back + 1):
        day = today - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        qtr = (day.month - 1) // 3 + 1
        url = (f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}"
               f"/QTR{qtr}/form.{day.strftime('%Y%m%d')}.idx")
        text = fetch(url)
        if not text:
            continue

        for line in text.splitlines():
            if not line.startswith(form):
                continue
            # Split on runs of 2+ spaces to survive the fixed-width layout.
            parts = re.split(r"\s{2,}", line.strip())
            if len(parts) < 5:
                continue
            ftype, company, cik, filed, path = parts[0], parts[1], parts[2], parts[3], parts[-1]
            if ftype.strip() != form:
                continue
            am = re.search(r"(\d{10}-\d{2}-\d{6})", path)
            if not am:
                continue
            accession = am.group(1)
            cik_clean = re.sub(r"\D", "", cik)
            link = (f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/"
                    f"{accession.replace('-', '')}/{accession}-index.htm")
            out.append({
                "accession": accession, "company": company.strip(),
                "cik": cik_clean, "link": link, "filed": filed.strip(),
            })
    return out


def discover(seen):
    queue = load_json(QUEUE_FILE, [])
    known = {item["key"] for item in queue}
    health = load_json(HEALTH_FILE, {})
    added = 0

    for form in FORMS:
        text = fetch(feed_url(form))
        entries = []
        if text:
            try:
                entries = find_all(ET.fromstring(text), "entry")
            except ET.ParseError as e:
                log(f"  feed unparseable: {form} :: {e}")
        else:
            log(f"  feed unreachable: {form}")

        # getcurrent does not serve every form type. When it comes back
        # empty, fall back to the daily index rather than going quiet.
        if not entries:
            rows = daily_index_entries(form)
            log(f"  {form}: getcurrent empty, daily index gave {len(rows)}")
            if rows:
                health[form] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for r in rows:
                key = f"{form}|{r['accession']}"
                if key in seen or key in known:
                    continue
                queue.append({
                    "key": key, "form": form, "accession": r["accession"],
                    "company": r["company"], "cik": r["cik"],
                    "link": r["link"], "filed": r["filed"],
                })
                known.add(key)
                added += 1
            continue

        health[form] = datetime.now(timezone.utc).isoformat(timespec="seconds")

        for entry in entries:
            link_el = find_one(entry, "link")
            link = link_el.get("href", "") if link_el is not None else ""
            if not link:
                continue
            m = re.search(r"(\d{10}-\d{2}-\d{6})", link)
            if not m:
                continue
            accession = m.group(1)
            key = f"{form}|{accession}"
            if key in seen or key in known:
                continue

            upd = find_one(entry, "updated")
            filed = (upd.text or "")[:10] if upd is not None else \
                datetime.now(timezone.utc).strftime("%Y-%m-%d")

            title_el = find_one(entry, "title")
            title = (title_el.text or "") if title_el is not None else ""
            rest = title.split(" - ", 1)[1] if " - " in title else title
            cm = re.match(r"^(.*?)\s*\((\d{10})\)", rest)
            company = cm.group(1).strip() if cm else rest.strip()
            cik = cm.group(2) if cm else ""

            queue.append({
                "key": key, "form": form, "accession": accession,
                "company": company, "cik": cik, "link": link, "filed": filed,
            })
            known.add(key)
            added += 1

    # A runaway queue means something upstream broke. Cap it and say so.
    if len(queue) > 2000:
        log(f"WARNING: queue at {len(queue)}, trimming to newest 2000")
        queue = queue[-2000:]

    save_json(QUEUE_FILE, queue)
    save_json(HEALTH_FILE, health)
    log(f"discovery: {added} new filings, queue now {len(queue)}")
    return queue


# ---------------------------------------------------------------
# Filing access
# ---------------------------------------------------------------

def filing_html(link):
    return fetch(link) or ""


def folder_of(link):
    return link[:link.rfind("/")]


def primary_xml(link, form_type):
    folder = folder_of(link)
    text = fetch(f"{folder}/index.json")
    if not text:
        return None
    try:
        items = json.loads(text)["directory"]["item"]
    except (ValueError, KeyError):
        return None

    xmls = [it for it in items
            if it["name"].lower().endswith(".xml")
            and not re.search(r"-index|^R\d|FilingSummary", it["name"], re.I)]

    xmls.sort(key=lambda it: (0 if it.get("type") == form_type else 1,
                              0 if "primary_doc" in it["name"].lower() else 1))

    for it in xmls[:3]:
        body = fetch(f"{folder}/{it['name']}")
        if not body:
            continue
        try:
            return ET.fromstring(body)
        except ET.ParseError:
            continue
    return None


# ---------------------------------------------------------------
# Market cap, optional
# ---------------------------------------------------------------

_cap_cache = {}


def market_cap(ticker):
    if not FINNHUB_KEY or not ticker:
        return None
    if ticker in _cap_cache:
        return _cap_cache[ticker]
    url = (f"https://finnhub.io/api/v1/stock/profile2?symbol={ticker}"
           f"&token={FINNHUB_KEY}")
    text = fetch(url, is_sec=False)
    if not text:
        return None
    try:
        cap = float(json.loads(text).get("marketCapitalization") or 0) * 1e6
    except (ValueError, TypeError):
        return None
    if cap <= 0:
        return None
    _cap_cache[ticker] = cap
    return cap


# ---------------------------------------------------------------
# Form 4
# ---------------------------------------------------------------

def cluster_check(ticker, trade_date):
    """Distinct insiders buying the same ticker inside the window."""
    out = {"fires": False, "insiders": 0, "total": 0.0}
    if not ticker or not BUYS_CSV.exists():
        return out
    try:
        cutoff = datetime.strptime(str(trade_date)[:10], "%Y-%m-%d") - \
            timedelta(days=CLUSTER_WINDOW_DAYS)
    except ValueError:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - \
            timedelta(days=CLUSTER_WINDOW_DAYS)

    names, total = set(), 0.0
    with BUYS_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("ticker") or "").upper() != ticker:
                continue
            try:
                d = datetime.strptime(str(row.get("trade_date"))[:10], "%Y-%m-%d")
            except ValueError:
                continue
            if d < cutoff:
                continue
            names.add(row.get("insider", ""))
            try:
                total += float(row.get("value_usd") or 0)
            except ValueError:
                pass

    out["insiders"] = len(names)
    out["total"] = total
    out["fires"] = len(names) >= CLUSTER_MIN_INSIDERS
    return out


def handle_form4(item):
    root = primary_xml(item["link"], "4")
    if root is None or local(root.tag) != "ownershipDocument":
        return False

    ticker = val(root, "issuerTradingSymbol").upper()
    company = val(root, "issuerName") or item["company"]

    # An amendment corrects an earlier filing. Label it so a changed number
    # never looks like a brand new purchase.
    amended = str(val(root, "documentType")).strip().endswith("/A")

    # A purchase made under a pre-scheduled 10b5-1 plan was decided months
    # ago. It carries far less signal than a discretionary open market buy.
    planned = is_true(val(root, "aff10b5One"))

    owners = []
    for o in find_all(root, "reportingOwner"):
        name = val(o, "rptOwnerName")
        if not name:
            continue
        rel = find_one(o, "reportingOwnerRelationship")
        roles = []
        if rel is not None:
            if is_true(val(rel, "isDirector")):
                roles.append("Director")
            if is_true(val(rel, "isOfficer")):
                roles.append(val(rel, "officerTitle") or "Officer")
            if is_true(val(rel, "isTenPercentOwner")):
                roles.append("10% owner")
        owners.append((name, ", ".join(roles)))

    insider, role = owners[0] if owners else ("Unknown", "")

    shares = value = after = priced = 0.0
    trade_date = ""
    for tx in find_all(root, "nonDerivativeTransaction"):
        if val(tx, "transactionCode") != "P":
            continue
        if val(tx, "transactionAcquiredDisposedCode") != "A":
            continue
        try:
            s = float(val(tx, "transactionShares").replace(",", "") or 0)
            p = float(val(tx, "transactionPricePerShare").replace(",", "") or 0)
            a = float(val(tx, "sharesOwnedFollowingTransaction").replace(",", "") or 0)
        except ValueError:
            continue
        d = val(tx, "transactionDate")
        shares += s
        if p > 0:
            value += s * p
            priced += s
        if a:
            after = a
        if d and (not trade_date or d < trade_date):
            trade_date = d

    if shares <= 0:
        return False

    avg_price = value / priced if priced else 0
    prior = after - shares
    pct = (shares / prior * 100) if prior > 0 else 999
    lag = days_between(trade_date, item["filed"]) if trade_date else ""

    append_csv(BUYS_CSV, [
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        trade_date, ticker, company, insider, role,
        int(shares), f"{avg_price:.2f}" if avg_price else "",
        int(value), "NEW" if pct == 999 else f"{pct:.1f}", item["accession"],
    ])

    cluster = cluster_check(ticker, trade_date)
    passes = value >= MIN_TRADE_VALUE and pct >= MIN_HOLDING_CHANGE_PCT
    if not passes and not cluster["fires"]:
        return False

    if FINNHUB_KEY and ticker:
        cap = market_cap(ticker)
        if cap is not None and cap < MIN_MARKET_CAP:
            return False

    priority = "HIGH" if cluster["fires"] else ("MEDIUM" if passes else "LOW")
    if REQUIRE_TICKER and not ticker:
        priority = "LOW"
    if planned:
        priority = "LOW"          # scheduled, not a conviction signal

    title = (f"\U0001F7E2 INSIDER CLUSTER - {cluster['insiders']} BUYERS"
             if cluster["fires"] else "\U0001F7E2 INSIDER BUY")
    if amended:
        title = "AMENDED  " + title

    rows = [
        ("PRIORITY", priority),
        ("TICKER", ticker or "n/a"),
        ("COMPANY", company),
        ("PERSON", insider),
        ("ROLE", role),
        ("ACTION", "BUY (open market, own money)"
                    + ("  [10b5-1 PLANNED]" if planned else "")),
        ("SHARES", num(shares)),
        ("PRICE", f"${avg_price:.2f}" if avg_price else "not disclosed"),
        ("VALUE", money(value) if value else "n/a"),
        ("HOLDING", "new position" if pct == 999 else f"+{pct:.1f}%"),
        ("TRADE", dmy(trade_date)),
        ("FILED", dmy(item["filed"]) + (f"  (lag {lag}d)" if lag != "" else "")),
    ]
    if cluster["fires"]:
        rows.append(("CLUSTER", f"{cluster['insiders']} insiders / "
                                f"{CLUSTER_WINDOW_DAYS}d / {money(cluster['total'])}"))

    return send_alert(
        "INSIDER_CLUSTER" if cluster["fires"] else "INSIDER_BUY",
        ticker, company, f"{insider} {money(value)}", value, lag,
        item["link"], box(title, rows, link=item["link"]), priority)


# ---------------------------------------------------------------
# Form 144
# ---------------------------------------------------------------

def handle_form144(item):
    root = primary_xml(item["link"], "144")
    if root is None:
        return False

    company = val(root, "issuerName") or item["company"]
    ticker = val(root, "issuerTradingSymbol").upper()
    units = val(root, "unitsToBeSold")
    sale_date = val(root, "approxSaleDate")
    broker = val(root, "brokerName") or val(root, "nameOfBrokerFirm")
    person = (val(root, "personName")
              or val(root, "nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold")
              or val(root, "relationshipToIssuer"))
    try:
        value = float(val(root, "aggregateMarketValue").replace(",", "") or 0)
    except ValueError:
        value = 0

    if not units and not value:
        return False
    priority = "MEDIUM" if value >= FORM144_MIN_VALUE else "LOW"
    if REQUIRE_TICKER and not ticker:
        priority = "LOW"

    rows = [
        ("PRIORITY", priority),
        ("TICKER", ticker or "not in filing"),
        ("COMPANY", company),
        ("PERSON", person),
        ("ACTION", "SELL (proposed, not yet executed)"),
        ("SHARES", num(units) if units else ""),
        ("VALUE", money(value) if value else ""),
        ("SALE DATE", dmy(sale_date)),
        ("BROKER", broker),
        ("FILED", dmy(item["filed"])),
    ]

    return send_alert("FORM_144", ticker, company,
                      f"proposed sale {money(value)}", value, "",
                      item["link"],
                      box("\U0001F534 INSIDER SELL NOTICE - Form 144", rows,
                          link=item["link"],
                          footer="Filed BEFORE the sale happens. "
                                 "Intent to sell, not a completed trade."),
                      priority)


# ---------------------------------------------------------------
# 8-K, NT, 13D
# ---------------------------------------------------------------

def handle_8k(item):
    html = fetch(item["link"])
    if not html:
        return False

    found = set(re.findall(r"Item\s*(\d+\.\d+)", html, re.I))
    hits = sorted(found & set(EIGHTK_ITEMS))
    if not hits:
        return False

    severe = "4.02" in hits or "1.03" in hits
    priority = "HIGH" if any(h in EIGHTK_HIGH for h in hits) else "LOW"
    title = "\U0001F4C4 8-K MATERIAL EVENT" + ("  [SEVERE]" if severe else "")

    rows = [("PRIORITY", priority),
            ("COMPANY", item["company"]),
            ("FILED", dmy(item["filed"]))]
    rows += [(f"ITEM {h}", EIGHTK_ITEMS[h]) for h in hits]

    return send_alert("8K", "", item["company"], f"items {','.join(hits)}",
                      0, "", item["link"], box(title, rows, link=item["link"]),
                      priority)


def handle_nt(item):
    rows = [
        ("PRIORITY", "HIGH"),
        ("COMPANY", item["company"]),
        ("FORM", item["form"]),
        ("MEANING", "Cannot file accounts on time"),
        ("FILED", dmy(item["filed"])),
    ]
    return send_alert("LATE_FILING", "", item["company"], item["form"],
                      0, "", item["link"],
                      box("\U0001F4C4 LATE FILING NOTICE", rows, link=item["link"],
                          footer="Stated reason is in Part III of the filing."))


PERCENT_RE = re.compile(
    r"(?:percent(?:age)?\s+of\s+class|percent\s+of\s+outstanding)"
    r"[^0-9]{0,120}?(\d{1,2}(?:\.\d{1,2})?)\s*%", re.IGNORECASE | re.DOTALL)
PERCENT_FALLBACK_RE = re.compile(r"\b(\d{1,2}\.\d{1,2})\s*%")


def parse_stake(html):
    """Percent of class from a 13D or 13G. Returns 0 if unreadable."""
    if not html:
        return 0.0
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"\s+", " ", text)

    m = PERCENT_RE.search(text)
    if m:
        try:
            v = float(m.group(1))
            if 0 < v <= 100:
                return v
        except ValueError:
            pass

    # Fall back to the largest plausible percentage in the document.
    vals = []
    for x in PERCENT_FALLBACK_RE.findall(text):
        try:
            v = float(x)
            if 0 < v <= 100:
                vals.append(v)
        except ValueError:
            continue
    return max(vals) if vals else 0.0


def filer_name(html, fallback=""):
    if not html:
        return fallback
    idx = html.find("Filed by")
    if idx > -1:
        tail = html[idx:idx + 1500]
        m = re.search(r'class="companyName">\s*([^<(]{3,120})', tail, re.I)
        if m:
            return m.group(1).replace("&amp;", "&").strip()
    return fallback


def handle_stake(item):
    """
    SC 13D and SC 13G. An investor crossing 5% of a company.

    This is the one filing type in the system where the information is often
    genuinely unnoticed for days. Institutional filers are excluded because
    an index fund crossing 5% is mechanical, not a view.
    """
    html = filing_html(item["link"]) or ""
    who = filer_name(html, "")

    if is_institutional(who):
        return False          # routine index and custodian filings

    # Open the actual document for the stake size.
    stake = 0.0
    folder = folder_of(item["link"])
    idx = fetch(f"{folder}/index.json")
    if idx:
        try:
            items = json.loads(idx)["directory"]["item"]
            docs = [it["name"] for it in items
                    if re.search(r"\.(htm|html|txt)$", it["name"], re.I)
                    and not re.search(r"-index|^R\d", it["name"], re.I)]
            for name in docs[:2]:
                body = fetch(f"{folder}/{name}")
                stake = parse_stake(body)
                if stake:
                    break
        except (ValueError, KeyError):
            pass

    if stake and stake < STAKE_MIN_PERCENT:
        return False

    activist = item["form"] == "SC 13D"
    priority = "HIGH" if (activist or stake >= 8) else "MEDIUM"

    rows = [
        ("PRIORITY", priority),
        ("COMPANY", item["company"]),
        ("FILER", who or "see filing"),
        ("FORM", item["form"] + ("  (activist)" if activist else "  (passive)")),
        ("STAKE", f"{stake:.1f}% of class" if stake else "see filing"),
        ("FILED", dmy(item["filed"])),
    ]

    title = ("\U0001F7E2 ACTIVIST STAKE - 13D" if activist
             else "\U0001F7E2 NEW 5% STAKE - 13G")

    return send_alert(
        "STAKE_13D" if activist else "STAKE_13G",
        "", item["company"], f"{who} {stake:.1f}%", 0, "", item["link"],
        box(title, rows, link=item["link"],
            footer="Non-institutional filer. Passive 13G stakes in small caps "
                   "often go unreported for days."),
        priority)


HANDLERS = {
    "4": handle_form4,
    "144": handle_form144,
    "SC 13D": handle_stake,
    "SC 13G": handle_stake,
}


# ---------------------------------------------------------------
# Stage 2: drain
# ---------------------------------------------------------------

def drain(queue, seen):
    processed = 0
    newly_seen = []

    while queue and processed < MAX_FILINGS_PER_RUN and alerts_sent < MAX_ALERTS_PER_RUN:
        item = queue.pop(0)
        processed += 1
        newly_seen.append(item["key"])
        try:
            handler = HANDLERS.get(item["form"])
            if handler:
                handler(item)
        except Exception as e:
            log(f"  error on {item['form']} {item['accession']}: {e}")

    save_json(QUEUE_FILE, queue)

    seen.extend(newly_seen)
    if len(seen) > SEEN_MAX:
        seen = seen[-SEEN_MAX:]
    save_json(SEEN_FILE, seen)

    log(f"drain: processed {processed}, alerts {alerts_sent}, "
        f"queue left {len(queue)}")


# ---------------------------------------------------------------
# CNN Fear and Greed index
# ---------------------------------------------------------------

FNG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
FNG_STATE = STATE_DIR / "fear_greed.json"

FNG_LEVELS = ["extreme fear", "fear", "neutral", "greed", "extreme greed"]


def fng_level(score):
    """CNN's own bands, used when the API omits the rating string."""
    s = float(score)
    if s < 25:
        return "extreme fear"
    if s < 45:
        return "fear"
    if s <= 55:
        return "neutral"
    if s <= 75:
        return "greed"
    return "extreme greed"


def fetch_fng():
    try:
        r = session.get(FNG_URL, timeout=25, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://edition.cnn.com/markets/fear-and-greed",
            "Origin": "https://edition.cnn.com",
        })
    except requests.RequestException as e:
        record_error("fear_greed", f"unreachable: {e}")
        return None
    if r.status_code != 200:
        record_error("fear_greed", f"HTTP {r.status_code}")
        return None
    try:
        d = r.json().get("fear_and_greed") or {}
    except ValueError:
        record_error("fear_greed", "non-JSON response")
        return None

    score = d.get("score")
    if score is None:
        record_error("fear_greed", "no score in payload")
        return None

    rating = str(d.get("rating") or fng_level(score)).strip().lower()
    if rating not in FNG_LEVELS:
        rating = fng_level(score)

    return {
        "score": round(float(score), 1),
        "rating": rating,
        "prev_close": d.get("previous_close"),
        "week_ago": d.get("previous_1_week"),
        "month_ago": d.get("previous_1_month"),
    }


def check_fear_greed():
    """Alert only when the LEVEL changes, not on every score wobble."""
    now = fetch_fng()
    if not now:
        return

    prev = load_json(FNG_STATE, {})
    last_rating = prev.get("rating")

    save_json(FNG_STATE, {"rating": now["rating"], "score": now["score"],
                          "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    clear_error("fear_greed")

    if not last_rating:
        log(f"fear & greed baseline set: {now['rating']} ({now['score']})")
        return
    if last_rating == now["rating"]:
        log(f"fear & greed unchanged: {now['rating']} ({now['score']})")
        return

    old_i = FNG_LEVELS.index(last_rating) if last_rating in FNG_LEVELS else -1
    new_i = FNG_LEVELS.index(now["rating"])
    direction = "toward greed" if new_i > old_i else "toward fear"
    mark = "\U0001F7E2" if new_i > old_i else "\U0001F534"

    rows = [
        ("PRIORITY", "HIGH"),
        ("NOW", f"{now['rating'].upper()}  ({now['score']})"),
        ("WAS", last_rating.upper()),
        ("MOVED", f"{direction}  {mark}"),
        ("PREV CLOSE", str(round(float(now["prev_close"]), 1))
                       if now.get("prev_close") is not None else ""),
        ("1 WEEK AGO", str(round(float(now["week_ago"]), 1))
                       if now.get("week_ago") is not None else ""),
        ("1 MONTH AGO", str(round(float(now["month_ago"]), 1))
                        if now.get("month_ago") is not None else ""),
    ]

    telegram(box("FEAR & GREED - LEVEL CHANGE", rows,
                 link="https://edition.cnn.com/markets/fear-and-greed",
                 footer="Sentiment gauge, not a trade signal. Extremes are "
                        "read as contrarian more often than as confirmation."))
    log(f"fear & greed: {last_rating} -> {now['rating']}")


# ---------------------------------------------------------------
# Self healing
# ---------------------------------------------------------------

ERROR_FILE = STATE_DIR / "errors.json"


def record_error(module, message):
    """
    Remember what broke and how many runs in a row it has been broken.
    A single failure is noise; a persistent one is a real problem.
    """
    errs = load_json(ERROR_FILE, {})
    e = errs.get(module, {"count": 0})
    e["count"] = e.get("count", 0) + 1
    e["last"] = str(message)[:200]
    e["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    errs[module] = e
    save_json(ERROR_FILE, errs)
    log(f"  recorded error [{module} x{e['count']}]: {message}")


def clear_error(module):
    """Recovered. Forget it so the heartbeat stays quiet."""
    errs = load_json(ERROR_FILE, {})
    if module in errs:
        del errs[module]
        save_json(ERROR_FILE, errs)
        log(f"  {module} recovered")


def self_heal():
    """
    Repairs what can actually be repaired automatically: corrupt or
    nonsensical state files. A parser that no longer matches its source
    cannot be fixed by retrying, so that case is reported instead.
    """
    fixed = []

    for path, default in ((SEEN_FILE, []), (QUEUE_FILE, []), (HEALTH_FILE, {})):
        try:
            raw = path.read_text() if path.exists() else None
            if raw is not None:
                json.loads(raw)
        except Exception:
            save_json(path, default)
            fixed.append(f"reset corrupt {path.name}")

    # A queue that never drains means something upstream is wrong; clearing
    # it lets discovery start clean rather than churning the same bad rows.
    q = load_json(QUEUE_FILE, [])
    if len(q) > 5000:
        save_json(QUEUE_FILE, q[-500:])
        fixed.append(f"trimmed runaway queue ({len(q)} entries)")

    if fixed:
        for f in fixed:
            log(f"self-heal: {f}")
    return fixed


# ---------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------

def heartbeat():
    health = load_json(HEALTH_FILE, {})
    now = datetime.now(timezone.utc)
    stale, ok = [], []

    # Congress runs in its own workflow but reports health the same way.
    cong = load_json(STATE_DIR / "congress_health.json", {})
    # Only watch congress modules that have actually reported at least once.
    # Senate is off by default, and a disabled module is not a broken one.
    watched = list(FORMS) + ["congress-house"]
    if "senate" in cong:
        watched.append("congress-senate")
    merged = dict(health)
    for k, v in cong.items():
        merged[f"congress-{k}"] = v

    # A feed added to the watch list five minutes ago has not had a chance
    # to report yet. Record when each feed was first watched and give it a
    # grace period before it can be called broken.
    first_seen = load_json(FIRST_SEEN_FILE, {})
    changed = False
    for form in watched:
        if form not in first_seen:
            first_seen[form] = now.isoformat(timespec="seconds")
            changed = True
    if changed:
        save_json(FIRST_SEEN_FILE, first_seen)

    for form in watched:
        ts = merged.get(form)
        if not ts:
            try:
                watched_for = (now - datetime.fromisoformat(
                    first_seen[form])).total_seconds() / 3600
            except (ValueError, KeyError):
                watched_for = 0
            if watched_for < FEED_GRACE_HOURS:
                log(f"heartbeat: {form} still in grace period "
                    f"({watched_for:.0f}h of {FEED_GRACE_HOURS}h)")
                continue
            if form.startswith("congress-"):
                stale.append(f"{form} (never ran)")
            else:
                stale.append(f"{form} (never)")
            continue
        try:
            age = (now - datetime.fromisoformat(ts)).total_seconds() / 3600
        except ValueError:
            stale.append(f"{form} (bad ts)")
            continue
        if age > 96:      # covers weekends and long holiday closures
            stale.append(f"{form} ({round(age)}h)")
        else:
            ok.append(form)

    today = week = 0
    if ALERTS_CSV.exists():
        with ALERTS_CSV.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    d = datetime.fromisoformat(row["timestamp"])
                except (ValueError, KeyError):
                    continue
                age_h = (now - d).total_seconds() / 3600
                if age_h < 24:
                    today += 1
                if age_h < 168:
                    week += 1

    queue = load_json(QUEUE_FILE, [])

    # Silent when healthy. One weekly liveness ping on Mondays so that
    # total silence never becomes ambiguous.
    is_monday = now.weekday() == 0

    if not stale and not is_monday:
        log("heartbeat: all feeds healthy, staying quiet")
        return

    errs = load_json(ERROR_FILE, {})
    persistent = {k: v for k, v in errs.items() if v.get("count", 0) >= 3}

    if stale or persistent:
        rows = [
            ("PRIORITY", "HIGH"),
            ("PROBLEM", ", ".join(stale) if stale else "module errors"),
            ("MEANING", "No new filings seen from these feeds"),
            ("LIKELY CAUSE", "EDGAR changed a URL or feed format"),
            ("ACTION", "Open the repo Actions tab, read the newest run log"),
            ("THEN", "Send the error to Claude to patch monitor.py"),
            ("STILL OK", ", ".join(ok) if ok else "none"),
            ("BACKLOG", str(len(queue))),
        ]
        for mod, e in list(persistent.items())[:3]:
            rows.append((mod.upper()[:11], f"x{e['count']}  {e.get('last','')[:40]}"))
        telegram(box("EDGAR MONITOR IS BROKEN", rows), silent=False)
        return

    rows = [
        ("STATUS", "All feeds healthy"),
        ("FEEDS", ", ".join(ok)),
        ("ALERTS 7D", str(week)),
        ("BACKLOG", str(len(queue))),
    ]
    telegram(box("WEEKLY CHECK - EDGAR OK", rows))


# ---------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------

def main():
    if not SEC_USER_AGENT or "example.com" in SEC_USER_AGENT:
        log("SEC_USER_AGENT is not set. Add it as a repository secret.")
        sys.exit(1)

    ensure_files()

    if len(sys.argv) > 1 and sys.argv[1] == "heartbeat":
        heartbeat()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "feeds":
        print("=== EDGAR FEED PROBE ===")
        health = load_json(HEALTH_FILE, {})
        def probe(url):
            text = fetch(url)
            if not text:
                return None
            try:
                return find_all(ET.fromstring(text), "entry")
            except ET.ParseError:
                return None

        for form in FORMS:
            entries = probe(feed_url(form, 20))
            last = health.get(form, "never")
            if entries is None:
                print(f"  {form:8} FETCH FAILED")
                continue
            print(f"  {form:8} {len(entries):3} entries   last healthy: {last}")
            for e in entries[:2]:
                t = find_one(e, "title")
                print(f"           {(t.text or '')[:72]}")

            if len(entries) == 0:
                print(f"           trying variants for {form}:")
                variants = [form.replace(" ", "+"), requests.utils.quote(form),
                            form.replace(" ", ""), form.split()[-1],
                            form.replace(" ", "+")[:5]]
                for v in dict.fromkeys(variants):
                    got = probe("https://www.sec.gov/cgi-bin/browse-edgar"
                                f"?action=getcurrent&type={v}"
                                "&company=&dateb=&owner=include&count=20"
                                "&output=atom")
                    print(f"             type={v!r:12} -> "
                          f"{len(got) if got is not None else 'error'}")
                rows = daily_index_entries(form)
                print(f"             daily index    -> {len(rows)}")
                for r in rows[:2]:
                    print(f"               {r['filed']}  {r['company'][:44]}")
        print("=== END ===")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "fng":
        d = fetch_fng()
        if not d:
            print("FEAR & GREED: FAILED. CNN blocked the request or changed "
                  "its payload. Check state/errors.json.")
        else:
            print(f"FEAR & GREED: OK. score={d['score']} rating={d['rating']}")
            print(f"  prev close={d.get('prev_close')} "
                  f"1w={d.get('week_ago')} 1m={d.get('month_ago')}")
            prev = load_json(FNG_STATE, {})
            print(f"  stored level: {prev.get('rating') or 'none yet'}")
        return

    self_heal()

    # Fear and greed moves slowly. Checking once an hour is plenty.
    if datetime.now(timezone.utc).minute < 5:
        try:
            check_fear_greed()
        except Exception as e:
            record_error("fear_greed", str(e))

    seen = load_json(SEEN_FILE, [])
    seen_set = set(seen)
    queue = discover(seen_set)
    drain(queue, seen)

    # Housekeeping once an hour, based on the clock, so it is cheap.
    if datetime.now(timezone.utc).minute < 5:
        prune_csv(BUYS_CSV, BUYS_HEADER, 180)
        prune_csv(ALERTS_CSV, ALERTS_HEADER, 365)
        log("pruned rolling windows")

    log("done")


if __name__ == "__main__":
    main()
