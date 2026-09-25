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

# A purchase this large alerts regardless of holding change. Without this,
# a very large holder adding a big dollar amount fails the 20% test purely
# because their existing position is enormous.
BIG_TRADE_OVERRIDE = 10_000_000
MIN_MARKET_CAP = 300_000_000

# Form 144 sell notices are very common and mostly routine.
# Raised from 5M on 25 Sep 2026. At 5M, 57 notices a week would have buzzed
# (about 8 a day). At 25M it is about 2 a day and still catches the ones that
# matter: Broadcom 225M, CrowdStrike 125M, GitLab 104M, Snowflake 66M.
FORM144_MIN_VALUE = 25_000_000

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

# A cluster alerts once when it forms, then at most once more if it grows to
# CLUSTER_BIG_MILESTONE insiders, then stays quiet for CLUSTER_COOLDOWN_DAYS.
# Before this, every extra buy inside the window re-fired the same cluster.
# Banco Bradesco (BBD) had 20 insiders buy on one day, filed over several
# days, and buzzed 19 times in a week.
CLUSTER_COOLDOWN_DAYS = 7
CLUSTER_BIG_MILESTONE = 5

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
# 5% stake alerts (13D / 13G) are back on, but restricted to the watchlist
# below. The raw feed is dominated by amendments to existing institutional
# positions; filtering to notable filers removes almost all of that noise
# while keeping the filings people actually care about.
STAKES_ENABLED = True
STAKES_NOTABLE_ONLY = True

# Names that always alert, at HIGH, regardless of size or holding change.
#
# Matched as lowercase substrings against the filer or insider name, so
# "buffett" catches "Buffett Warren E" and "berkshire" catches every Berkshire
# entity. Add or remove freely; this is the main dial for who you hear about.
NOTABLE_FILERS = [
    # Berkshire
    "berkshire hathaway", "buffett",
    # Activist and famous funds
    "pershing square", "ackman", "icahn", "elliott investment",
    "elliott management", "third point", "daniel loeb", "starboard value",
    "trian fund", "trian partners", "nelson peltz", "valueact",
    "jana partners", "engine capital", "engaged capital", "sachem head",
    "corvex", "glenview capital", "marcato capital", "legion partners",
    # Well-known managers
    "greenlight capital", "einhorn", "appaloosa", "tepper", "baupost",
    "klarman", "scion asset", "burry", "duquesne", "druckenmiller",
    "soros fund", "tiger global", "coatue", "lone pine capital",
    "viking global", "pointstate", "altimeter capital", "himalaya capital",
    "greenhaven road", "abrams capital",
    # Founders and executives. SEC filings write names surname first.
    "musk elon", "elon musk", "bezos jeffrey", "jeff bezos",
    "zuckerberg mark", "huang jen hsun", "jensen huang", "dell michael",
    "ellison lawrence", "larry ellison", "cook timothy", "tim cook",
    "page lawrence", "brin sergey", "schmidt eric", "nadella satya",
    "pichai sundar", "benioff marc", "chesky brian", "karp alexander",
    "woodman nicholas", "gates william", "bill gates",
    "bill and melinda gates", "walton jim", "walton alice",
    "walton s robson", "walton samuel robson", "koch industries",
    "koch charles", "charles koch", "thiel peter", "peter thiel",
]

# Whole-word matching. The old substring match flagged two of 649 real
# insiders wrongly on 25 Sep 2026: "Edell Michael" contains "dell michael",
# and "Huang Jen-Chau" matched "huang jen" (Jensen Huang files as Huang Jen
# Hsun). Bare surnames like "walton", "koch" and "lone pine" were replaced
# with full names because notable_in_text scans an entire filing page, where
# a bare word can be an address or a passing mention.
_NOTABLE_FILER_RE = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in NOTABLE_FILERS) + r")\b")


def _norm_person(text):
    """Lowercase, '&' to 'and', and punctuation to spaces, so
    'Bill & Melinda Gates' and 'Gates William H. III' compare cleanly."""
    s = str(text or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s)


def is_notable(name):
    """True if this filer or insider is on the watchlist."""
    return bool(_NOTABLE_FILER_RE.search(_norm_person(name)))


def notable_in_text(text):
    """
    Scan a whole filing page for a watchlist name.

    The 'Filed by' parser fails on plenty of layouts, leaving the filer as
    'see filing'. Searching the raw page catches those cases rather than
    silently dropping a filing we care about.
    """
    if not text:
        return ""
    m = _NOTABLE_FILER_RE.search(_norm_person(re.sub(r"<[^>]+>", " ", text)))
    return m.group(1) if m else ""
    low = text.lower()
    for x in NOTABLE_FILERS:
        if x in low:
            return x
    return ""

FORMS = ["4", "144"] + (["SC 13D", "SC 13G"] if STAKES_ENABLED else [])

# EDGAR's getcurrent does prefix matching on `type`, but it chokes on the
# letter after the number: "SC 13D" returns nothing while "SC 13" returns
# both 13D and 13G. So we query the prefix and sort the results out using
# the form name that appears in each entry's title.
#
#   query string  ->  which watched forms it can produce
FEED_QUERIES = [
    ("4", ["4"]),
    ("144", ["144"]),
] + ([("SCHEDULE+13", ["SC 13D", "SC 13G"])] if STAKES_ENABLED else [])

# EDGAR does NOT call these "SC 13D" and "SC 13G". Its atom titles read
# "SCHEDULE 13D/A" and "SCHEDULE 13G/A". Querying the wrong name returns a
# clean empty feed rather than an error, which is why this went unnoticed.
# Confirmed by pulling the untyped feed and counting the form names present:
#   {'4': 88, '3': 4, 'SCHEDULE 13G/A': 2, '144': 2, 'SCHEDULE 13D/A': 4}
#
# Note EDGAR uses BOTH conventions: "SC 13E3" exists alongside "SCHEDULE 13D".
FORM_TITLES = {
    "4":       ["4"],
    "144":     ["144"],
    "SC 13D":  ["SCHEDULE 13D", "SC 13D"],
    "SC 13G":  ["SCHEDULE 13G", "SC 13G"],
}

# If the typed query ever returns nothing, fall back to the untyped feed,
# which is proven to contain these filings mixed in with everything else.
UNTYPED_FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
                "&type=&company=&dateb=&owner=include&count=100&output=atom")


def title_to_form(title, produces):
    """Map an atom title's form name onto whichever watched form it is."""
    actual = title.split(" - ", 1)[0].strip() if " - " in title else ""
    for form in produces:
        for prefix in FORM_TITLES.get(form, [form]):
            if actual.startswith(prefix):
                return form, actual
    return None, actual

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

# Feeds whose source is known-unresolved. Add a form name here to keep it
# being attempted while stopping the heartbeat from reporting it, so a known
# issue does not produce a daily alarm that trains you to ignore alarms.
SUPPRESS_STALE = set()

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
    """
    Keep a CSV to a rolling window so the repo and the reads stay small.

    Written with csv.reader, not DictReader. The alerts file on disk still has
    an older 8-column header while newer rows carry 9 values, because the
    priority column was added to the code without rewriting the file. With
    DictReader the extra value landed under a None key and DictWriter raised
    "dict contains fields not in fieldnames: None", which crashed every run
    before state could be committed.

    Rows are now read positionally, the timestamp is located by name in the
    file's own header, and every row is written back padded or trimmed to the
    canonical header. That also repairs the stale header on disk.
    """
    if not path.exists():
        return
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        return

    file_header, body = rows[0], rows[1:]
    try:
        ts_idx = file_header.index(date_col)
    except ValueError:
        ts_idx = 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept = []
    for r in body:
        if not r:
            continue
        keep = True
        try:
            d = datetime.fromisoformat(r[ts_idx])
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            keep = d >= cutoff
        except (ValueError, IndexError):
            keep = True          # unreadable date, keep rather than lose it
        if keep:
            width = len(header)
            kept.append((r + [""] * width)[:width])

    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
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
            "disable_notification": False,   # everything buzzes, by choice
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

    for query, produces in FEED_QUERIES:
        url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
               f"&type={query}&company=&dateb=&owner=include"
               f"&count={FEED_COUNT}&output=atom")
        text = fetch(url)
        if not text:
            log(f"  feed unreachable: {query}")
            continue

        try:
            entries = find_all(ET.fromstring(text), "entry")
        except ET.ParseError as e:
            log(f"  feed unparseable: {query} :: {e}")
            continue

        if not entries and query != "4":
            # Proven fallback: the untyped feed carries every form type.
            log(f"  feed empty for {query}, trying untyped feed")
            alt = fetch(UNTYPED_FEED)
            if alt:
                try:
                    entries = find_all(ET.fromstring(alt), "entry")
                except ET.ParseError:
                    entries = []

        if not entries:
            log(f"  feed empty: {query}")
            continue

        # The query returned data, so every form it can produce is healthy,
        # even if none of that specific type was filed today.
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for f in produces:
            health[f] = stamp

        for entry in entries:
            link_el = find_one(entry, "link")
            link = link_el.get("href", "") if link_el is not None else ""
            if not link:
                continue
            am = re.search(r"(\d{10}-\d{2}-\d{6})", link)
            if not am:
                continue
            accession = am.group(1)

            title_el = find_one(entry, "title")
            title = (title_el.text or "") if title_el is not None else ""

            # Title looks like: "SCHEDULE 13G/A - GoPro, Inc. (...) (Subject)"
            form, actual = title_to_form(title, produces)
            if form is None:
                continue

            key = f"{form}|{accession}"
            if key in seen or key in known:
                continue

            upd = find_one(entry, "updated")
            filed = (upd.text or "")[:10] if upd is not None else \
                datetime.now(timezone.utc).strftime("%Y-%m-%d")

            rest = title.split(" - ", 1)[1] if " - " in title else title
            cm = re.match(r"^(.*?)\s*\((\d{10})\)", rest)
            company = cm.group(1).strip() if cm else rest.strip()
            cik = cm.group(2) if cm else ""

            queue.append({
                "key": key, "form": form, "accession": accession,
                "company": company, "cik": cik, "link": link,
                "filed": filed, "actual_form": actual,
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

# Filers write these when an issuer has no listed stock. They are not
# tickers. Treating "NONE" as real let unrelated private funds cluster with
# each other, because cluster detection groups buys by ticker.
JUNK_TICKERS = {"NONE", "N/A", "NA", "NULL", "-", "--", "0", "TBD"}


def clean_ticker(t):
    t = (t or "").strip().upper()
    return "" if t in JUNK_TICKERS else t


SEC_COMPANIES_FILE = STATE_DIR / "sec_companies.json"
SEC_COMPANIES_URL = "https://www.sec.gov/files/company_tickers.json"
_sec_rows = None
_cik_index = None
_name_index = None


def load_sec_companies(refresh=True):
    """
    The SEC's own list of every exchange-listed company: CIK, ticker, name.

    Used two ways. Form 144 XML has no ticker field, so ticker_for_cik
    resolves it from the issuer CIK; without that every notice arrived blank
    and was demoted to LOW, so the feature had never fired. Trump filings have
    no tickers either, only names, so name_to_ticker resolves those.

    Only monitor.py refreshes this file. trump.py calls it with
    refresh=False and reads the committed copy. Two workflows writing one
    file would collide on git rebase.
    """
    global _sec_rows
    if _sec_rows is not None:
        return _sec_rows

    cached = load_json(SEC_COMPANIES_FILE, [])
    want = refresh and (not cached or due("sec_companies", 7 * 24 * 60))
    if want:
        text = fetch(SEC_COMPANIES_URL)
        fresh = []
        if text:
            try:
                for row in json.loads(text).values():
                    fresh.append([str(int(row.get("cik_str"))),
                                  str(row.get("ticker", "")).upper(),
                                  str(row.get("title", ""))])
            except (ValueError, TypeError, AttributeError) as e:
                record_error("sec_companies", f"parse failed: {e}")
        if fresh:
            save_json(SEC_COMPANIES_FILE, fresh)
            clear_error("sec_companies")
            cached = fresh
        elif not cached:
            record_error("sec_companies", "download failed, no cache")
    _sec_rows = cached or []
    return _sec_rows


def ticker_for_cik(cik, refresh=True):
    global _cik_index
    try:
        key = str(int(str(cik).strip()))
    except (TypeError, ValueError):
        return ""
    if _cik_index is None:
        idx = {}
        for c, t, _ in load_sec_companies(refresh):
            idx.setdefault(c, t)          # first entry is the primary listing
        _cik_index = idx
    return clean_ticker(_cik_index.get(key, ""))


_NAME_DROP = {"INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY",
              "LTD", "LIMITED", "PLC", "HLDGS", "HLDG", "HOLDINGS", "HOLDING",
              "GROUP", "GRP", "THE", "NEW", "DEL", "CL", "CLASS", "A", "B",
              "COMMON", "STOCK", "SHS", "LLC", "LP", "NV", "SA", "AG", "SE"}


def norm_company(name):
    """Reduce a company name to a comparable core: 'Booking Holdings Inc.'
    and 'BOOKING HLDGS INC' both become 'BOOKING'."""
    s = str(name or "").upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    words = [w for w in s.split() if w not in _NAME_DROP]
    return " ".join(words)


def name_to_ticker(name, refresh=False):
    """
    Exact match on the normalised name only. No prefix or fuzzy matching:
    'BROWN' must never be guessed as Brown-Forman. A wrong ticker is worse
    than showing the company name.
    """
    global _name_index
    if _name_index is None:
        idx, clash = {}, set()
        for c, t, title in load_sec_companies(refresh):
            k = norm_company(title)
            if not k:
                continue
            if k in idx:
                # Same company, second share class (GOOGL and GOOG): keep the
                # first, it is the primary listing. Two DIFFERENT companies
                # sharing a core name is ambiguous, so drop it entirely.
                if idx[k][0] != c:
                    clash.add(k)
                continue
            idx[k] = (c, t)
        _name_index = {k: v[1] for k, v in idx.items() if k not in clash}
    return clean_ticker(_name_index.get(norm_company(name), ""))


CLUSTER_STATE_FILE = STATE_DIR / "cluster_alerts.json"


def cluster_should_alert(ticker, insiders):
    """
    True if this cluster is new, or has grown enough to be worth a second
    alert. Records the alert when it returns True.
    """
    st = load_json(CLUSTER_STATE_FILE, {})
    now = datetime.now(timezone.utc)
    prev = st.get(ticker)
    if prev:
        try:
            age_days = (now - datetime.fromisoformat(prev["at"])).total_seconds() / 86400
        except (KeyError, ValueError):
            age_days = 999
        if age_days < CLUSTER_COOLDOWN_DAYS:
            # Inside the cooldown, the only thing that breaks the silence is
            # the cluster crossing the milestone for the first time.
            crossed = (int(prev.get("insiders", 0)) < CLUSTER_BIG_MILESTONE
                       <= insiders)
            if not crossed:
                return False
    st[ticker] = {"at": now.isoformat(timespec="seconds"), "insiders": insiders}
    # Drop entries far outside the window so the file stays small.
    keep = {}
    for k, v in st.items():
        try:
            if (now - datetime.fromisoformat(v["at"])).days < CLUSTER_COOLDOWN_DAYS * 4:
                keep[k] = v
        except (KeyError, ValueError, TypeError):
            pass
    save_json(CLUSTER_STATE_FILE, keep)
    return True


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
            if clean_ticker(row.get("ticker")) != ticker:
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

    ticker = (clean_ticker(val(root, "issuerTradingSymbol"))
              or ticker_for_cik(val(root, "issuerCik")))
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
    # A pre-scheduled 10b5-1 buy is not a conviction signal, so it must not
    # open a cluster or start the cooldown clock that would silence a real
    # cluster forming later.
    if cluster["fires"] and (planned or
                             not cluster_should_alert(ticker, cluster["insiders"])):
        # Already alerted this cluster recently and it has not grown enough.
        # The buy is still logged to buys.csv above.
        cluster["fires"] = False
    notable = is_notable(insider)
    big = value >= BIG_TRADE_OVERRIDE
    passes = value >= MIN_TRADE_VALUE and pct >= MIN_HOLDING_CHANGE_PCT

    if not (passes or cluster["fires"] or notable or big):
        return False

    if FINNHUB_KEY and ticker:
        cap = market_cap(ticker)
        if cap is not None and cap < MIN_MARKET_CAP:
            return False

    priority = "HIGH" if cluster["fires"] else ("MEDIUM" if passes else "LOW")
    if big:
        priority = "HIGH"
    if REQUIRE_TICKER and not ticker:
        priority = "LOW"
    if planned:
        priority = "LOW"          # scheduled, not a conviction signal
    if notable:
        priority = "HIGH"         # watchlist always wins

    title = (f"INSIDER CLUSTER - {cluster['insiders']} BUYERS"
             if cluster["fires"] else "INSIDER BUY")
    if notable:
        title = "WATCHLIST BUY"
    if amended:
        title = "AMENDED  " + title

    rows = [
        ("PRIORITY", priority + ("  \u2605 WATCHLIST" if notable else "")),
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
    # Form 144 XML carries no ticker, so resolve it from the issuer CIK.
    ticker = (clean_ticker(val(root, "issuerTradingSymbol"))
              or ticker_for_cik(val(root, "issuerCik") or val(root, "cik")))
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
                      box("INSIDER SELL NOTICE - Form 144", rows,
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
    title = "8-K MATERIAL EVENT" + ("  [SEVERE]" if severe else "")

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
                      box("LATE FILING NOTICE", rows, link=item["link"],
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

    # The "Filed by" parser fails on many layouts, so also scan the whole
    # page. A watchlist hit overrides everything below.
    hit = notable_in_text(html)
    notable = bool(hit) or is_notable(who)

    if STAKES_NOTABLE_ONLY and not notable:
        return False          # everything else is CSV only
    if is_institutional(who) and not notable:
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
    amended = "/A" in str(item.get("actual_form", ""))
    priority = "HIGH" if (activist or stake >= 8) else "MEDIUM"
    if amended:
        priority = "MEDIUM"      # updates to an existing stake are routine
    if notable:
        priority = "HIGH"

    rows = [
        ("PRIORITY", priority + ("  \u2605 WATCHLIST" if notable else "")),
        ("COMPANY", item["company"]),
        ("FILER", who or (hit.title() if hit else "see filing")),
        ("FORM", (item.get("actual_form") or item["form"])
                 + ("  (activist)" if activist else "  (passive)")
                 + ("  [AMENDMENT]" if amended else "")),
        ("STAKE", f"{stake:.1f}% of class" if stake else "see filing"),
        ("FILED", dmy(item["filed"])),
    ]

    title = ("WATCHLIST STAKE" if notable
             else ("ACTIVIST STAKE - 13D" if activist
                   else "NEW 5% STAKE - 13G"))

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
    mark = "up" if new_i > old_i else "down"

    rows = [
        ("PRIORITY", "HIGH"),
        ("NOW", f"{now['rating'].upper()}  ({now['score']})"),
        ("WAS", last_rating.upper()),
        ("MOVED", direction),
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
SCHEDULE_FILE = STATE_DIR / "schedule.json"


def stamp(fname, error=None, **extra):
    """
    Record a module's outcome in its own state file.

    Success sets last_ok and clears any error. Failure keeps the previous
    last_ok, so the file still shows when it last genuinely worked, and adds
    last_error plus a consecutive fail_count. The heartbeat reads both.

    Each module writes only its own file. A shared errors file would be
    edited by several workflows at once and collide on git rebase.
    """
    path = STATE_DIR / fname
    st = load_json(path, {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if error is None:
        st["last_ok"] = now
        st.pop("last_error", None)
        st.pop("error_at", None)
        st["fail_count"] = 0
    else:
        st["last_error"] = str(error)[:200]
        st["error_at"] = now
        st["fail_count"] = int(st.get("fail_count", 0)) + 1
    st.update(extra)
    save_json(path, st)


def due(job, every_minutes):
    """
    True if this job has not run in the last `every_minutes`.

    Replaces the old `datetime.now().minute < 5` gate. GitHub throttles the
    5-minute cron down to one run every 20-40 minutes at random minutes, so a
    run landing in minutes 0-4 of the hour was down to chance. Fear and Greed
    went 11 hours between checks because of it. Tracking the last run time
    makes the interval hold regardless of when the runner starts.
    """
    sched = load_json(SCHEDULE_FILE, {})
    now = datetime.now(timezone.utc)
    last = sched.get(job)
    if last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < every_minutes * 60:
                return False
        except ValueError:
            pass
    sched[job] = now.isoformat(timespec="seconds")
    save_json(SCHEDULE_FILE, sched)
    return True


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

# Modules that run in their own workflows. Each writes a state file with a
# last_ok timestamp on every successful run, plus last_error and fail_count
# when a run fails. Before 24 Sep 2026 the heartbeat only watched SEC forms
# and congress, so halts ran unmonitored and sat stalled for about 7 hours
# with nothing reporting it.
#
#   label          state file              limit (hours)
MODULE_CHECKS = [
    ("halts",      "halts_state.json",     3),     # every 5 min
    ("fear-greed", "fear_greed.json",      6),     # hourly
    ("trump",      "trump_state.json",     48),    # daily
    ("earnings",   "earnings_state.json",  192),   # weekly
    ("us-events",  "events_state.json",    192),   # weekly
]

# Older state files used other key names. Read whichever is present.
OK_KEYS = ("last_ok", "at", "last_sent", "sent")


def _age_hours(ts, now):
    try:
        d = datetime.fromisoformat(ts)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (now - d).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def seed_first_seen():
    """
    Record when each watched label first appeared.

    Called from the regular monitor run, which commits state. The heartbeat
    workflow is read-only and never commits, so if the heartbeat seeded this
    itself the ledger would reset on every run and the grace period would
    never expire.
    """
    labels = list(FORMS) + ["congress-house"] + [c[0] for c in MODULE_CHECKS]
    first_seen = load_json(FIRST_SEEN_FILE, {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changed = False
    for lbl in labels:
        if lbl not in first_seen:
            first_seen[lbl] = now
            changed = True
    if changed:
        save_json(FIRST_SEEN_FILE, first_seen)


def heartbeat():
    """
    Silent when healthy. Any message means go and look.

    Covers SEC forms, congress, and every module in MODULE_CHECKS. A feed or
    module added recently gets FEED_GRACE_HOURS before it can be called
    broken, so adding something never triggers an instant false alarm.
    """
    health = load_json(HEALTH_FILE, {})
    now = datetime.now(timezone.utc)
    problems, ok = [], []

    # First-seen ledger drives the grace period. Seeded by the regular
    # monitor run; a label missing here is treated as brand new.
    first_seen = load_json(FIRST_SEEN_FILE, {})

    def in_grace(lbl):
        age = _age_hours(first_seen.get(lbl), now)
        # Not seeded yet means the monitor has not run since this label was
        # added. Give it the benefit of the doubt.
        return age is None or age < FEED_GRACE_HOURS

    # ---- SEC forms and congress ----
    cong = load_json(STATE_DIR / "congress_health.json", {})
    merged = dict(health)
    for k, v in cong.items():
        merged[f"congress-{k}"] = v

    for form in list(FORMS) + ["congress-house"]:
        if form in SUPPRESS_STALE:
            continue
        age = _age_hours(merged.get(form), now)
        if age is None:
            if not in_grace(form):
                problems.append((form, "never reported"))
            continue
        if age > 96:          # covers weekends and long holiday closures
            problems.append((form, f"last ok {round(age)}h ago"))
        else:
            ok.append(form)

    # ---- modules with their own state files ----
    for label, fname, limit in MODULE_CHECKS:
        st = load_json(STATE_DIR / fname, {})
        ts = next((st[k] for k in OK_KEYS if k in st), None)
        age = _age_hours(ts, now)
        err = st.get("last_error")
        if age is None:
            if not in_grace(label):
                problems.append((label, "never reported"
                                 + (f", {err[:34]}" if err else "")))
            continue
        if age > limit:
            detail = f"last ok {round(age)}h ago (limit {limit}h)"
            if err:
                detail += f", {err[:30]}"
            problems.append((label, detail))
        else:
            ok.append(label)

    # ---- modules erroring repeatedly inside monitor.py ----
    errs = load_json(ERROR_FILE, {})
    for mod, e in errs.items():
        if e.get("count", 0) >= 3:
            problems.append((mod, f"x{e['count']} {e.get('last', '')[:34]}"))

    if not problems:
        log(f"heartbeat: {len(ok)} checks healthy, staying quiet")
        return

    rows = [("PRIORITY", "HIGH"), ("", "")]
    rows += [(name[:11], detail) for name, detail in problems[:10]]
    rows += [("", ""),
             ("STILL OK", ", ".join(ok) if ok else "none"),
             ("ACTION", "Actions tab, run Diagnostics"),
             ("THEN", "Send the output to Claude")]
    telegram(box("SCOUT FILINGS - NEEDS ATTENTION", rows), silent=False)


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
        # getcurrent only shows filings EDGAR has received recently. It
        # accepts filings roughly 06:00-22:00 ET, so a probe run overnight
        # will look empty for low volume forms even when nothing is wrong.
        et_hour = (datetime.now(timezone.utc).hour - 4) % 24
        window = "OPEN" if 6 <= et_hour < 22 else "CLOSED (expect thin results)"
        print(f"  approx US Eastern time: {et_hour:02d}:xx   "
              f"EDGAR filing window: {window}")
        print()
        health = load_json(HEALTH_FILE, {})
        def probe(url):
            text = fetch(url)
            if not text:
                return None
            try:
                return find_all(ET.fromstring(text), "entry")
            except ET.ParseError:
                return None

        for query, produces in FEED_QUERIES:
            entries = probe("https://www.sec.gov/cgi-bin/browse-edgar"
                            f"?action=getcurrent&type={query}"
                            "&company=&dateb=&owner=include&count=40"
                            "&output=atom")
            form = query
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

    if len(sys.argv) > 1 and sys.argv[1] == "stakes":
        print("=== STAKE FILING SOURCE PROBE ===")
        et = (datetime.now(timezone.utc).hour - 4) % 24
        print(f"  approx US Eastern: {et:02d}:xx  "
              f"({'window open' if 6 <= et < 22 else 'window CLOSED'})")

        def count_atom(url):
            t = fetch(url)
            if not t:
                return "fetch failed"
            try:
                es = find_all(ET.fromstring(t), "entry")
            except ET.ParseError:
                return "unparseable"
            forms = {}
            for e in es:
                ti = find_one(e, "title")
                f = ((ti.text or "").split(" - ", 1)[0].strip()
                     if ti is not None else "?")
                forms[f] = forms.get(f, 0) + 1
            return f"{len(es)} entries {forms}"

        print("\n[A] getcurrent, owner parameter variants")
        for owner in ("include", "only", "exclude"):
            u = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
                 f"&type=SC+13&company=&dateb=&owner={owner}"
                 "&count=100&output=atom")
            print(f"    owner={owner:8} -> {count_atom(u)}")

        print("\n[B] getcurrent, no type filter (what forms appear at all)")
        u = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
             "&type=&company=&dateb=&owner=include&count=100&output=atom")
        print(f"    {count_atom(u)}")

        print("\n[C] daily index, last 4 completed weekdays")
        today = datetime.now(timezone.utc).date()
        for back in range(1, 6):
            day = today - timedelta(days=back)
            if day.weekday() >= 5:
                continue
            q = (day.month - 1) // 3 + 1
            for kind in ("form", "master"):
                u = (f"https://www.sec.gov/Archives/edgar/daily-index/"
                     f"{day.year}/QTR{q}/{kind}.{day.strftime('%Y%m%d')}.idx")
                t = fetch(u)
                if not t:
                    print(f"    {day} {kind:6} -> unavailable")
                    continue
                d13 = sum(1 for l in t.splitlines()
                          if l.startswith("SC 13D"))
                g13 = sum(1 for l in t.splitlines()
                          if l.startswith("SC 13G"))
                print(f"    {day} {kind:6} -> {len(t.splitlines())} lines, "
                      f"SC 13D={d13}, SC 13G={g13}")
                if d13 or g13:
                    for l in t.splitlines():
                        if l.startswith(("SC 13D", "SC 13G")):
                            print(f"        {l[:100]}")
                            break
                break

        print("\n[D] full text search API")
        for form in ("SC 13D", "SC 13G"):
            u = ("https://efts.sec.gov/LATEST/search-index?q=%22the%22"
                 f"&forms={form.replace(' ', '%20')}")
            t = fetch(u, is_sec=False)
            if not t:
                print(f"    {form} -> fetch failed")
                continue
            try:
                total = json.loads(t).get("hits", {}).get("total", {})
                print(f"    {form} -> hits {total}")
            except ValueError:
                print(f"    {form} -> non-JSON ({t[:60]})")

        print("\n=== END ===")
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
    seed_first_seen()

    # Fear and greed moves slowly. Checking once an hour is plenty.
    if due("fear_greed", 55):
        try:
            check_fear_greed()
        except Exception as e:
            record_error("fear_greed", str(e))

    seen = load_json(SEEN_FILE, [])
    seen_set = set(seen)
    queue = discover(seen_set)
    drain(queue, seen)

    # Housekeeping once an hour.
    if due("prune", 55):
        # Housekeeping must never take a run down with it. A failure here
        # previously crashed the job before the commit step, so nothing was
        # saved and the same crash repeated on every run.
        for p, hdr, days in ((BUYS_CSV, BUYS_HEADER, 180),
                             (ALERTS_CSV, ALERTS_HEADER, 365)):
            try:
                prune_csv(p, hdr, days)
            except Exception as e:
                record_error("prune", f"{p.name}: {e}")
            else:
                clear_error("prune")
        log("pruned rolling windows")

    log("done")


if __name__ == "__main__":
    main()
