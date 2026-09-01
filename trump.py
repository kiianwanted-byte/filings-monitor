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
from datetime import datetime, timezone, timedelta
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

# Same reasoning as congress: absorb the historical backlog silently.
MAX_FILING_AGE_DAYS = 45

SEEN_FILE = STATE_DIR / "trump_seen.json"
TRADES_CSV = DATA_DIR / "trump_trades.csv"

TRADES_HEADER = ["timestamp", "filing_date", "ticker", "asset", "action",
                 "amount_low", "amount_high", "trade_date", "lag_days", "link"]

# OGE publishes through a Lotus Notes application. These are the entry points
# worth trying; the diagnostic reports which of them actually respond.
# OGE runs a Lotus Domino application. Domino views expose a structured feed
# via ?ReadViewEntries, which is far more reliable than scraping the rendered
# HTML, where the links are document handles rather than files.
VIEW_BASE = "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index"

INDEX_CANDIDATES = [
    VIEW_BASE + "?ReadViewEntries&Count=1000",
    VIEW_BASE + "?ReadViewEntries&Count=1000&OutputFormat=JSON",
    VIEW_BASE + "?OpenView&Count=1000",
    VIEW_BASE + "?OpenView",
    "https://www.oge.gov/web/OGE.nsf/Officials%20Individual%20Disclosures%20Search%20Collection",
    "https://open-cabinet.org/officials/trump-donald-j",
]

# Known filings, used only if discovery fails entirely. Lets the parser and
# alerting run end to end rather than the whole module sitting dead.
SEED_FILINGS = [
    "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index/2BF91F890F718ACB85258E5B002DE16B/$FILE/Donald-J-Trump-08.12.2026-278T.pdf",
    "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index/F9CA13B970439E8F85258E27002DDF15/$FILE/Donald-J-Trump-06.25.2026-278T%20(2).pdf",
    "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index/405E4EC4E27BE8D185258DF7002DD1C0/$FILE/Trump,%20Donald%20J.-05.08.2026-278T(2).pdf",
    "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index/5326D3AF5BE7C25385258DF7002DD1B7/$FILE/Trump,%20Donald%20J.-05.08.2026-278T.pdf",
    "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index/CD75555856A7D2E485258DE4002DD4A0/$FILE/Donald-J-Trump-4.20.2026-278T.pdf",
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


TICKER_MAP = {
    "3m": "MMM", "abbott laboratories": "ABT", "abbvie": "ABBV",
    "accenture": "ACN", "adobe": "ADBE", "advanced micro devices": "AMD",
    "aflac": "AFL", "agilent technologies": "A", "air products": "APD",
    "airbnb": "ABNB", "alexandria real estate": "ARE", "allstate": "ALL",
    "alphabet": "GOOGL", "altria": "MO", "amazon": "AMZN", "amazon.com": "AMZN",
    "amcor": "AMCR", "amerisourcebergen": "COR", "ameren": "AEE",
    "american electric power": "AEP", "american express": "AXP",
    "american tower": "AMT", "american water works": "AWK", "ametek": "AME",
    "amgen": "AMGN", "amphenol": "APH", "analog devices": "ADI",
    "aon": "AON", "apple": "AAPL", "applied materials": "AMAT",
    "aptiv": "APTV", "archer daniels midland": "ADM", "arista networks": "ANET",
    "arthur j gallagher": "AJG", "assurant": "AIZ", "at&t": "T",
    "atmos energy": "ATO", "autodesk": "ADSK", "automatic data processing": "ADP",
    "autozone": "AZO", "avalonbay": "AVB", "avery dennison": "AVY",
    "baker hughes": "BKR", "ball": "BALL", "bank of america": "BAC",
    "bank of new york mellon": "BK", "baxter international": "BAX",
    "becton dickinson": "BDX", "berkshire hathaway": "BRK.B",
    "best buy": "BBY", "biogen": "BIIB", "blackrock": "BLK",
    "blackstone": "BX", "boeing": "BA", "booking holdings": "BKNG",
    "boston scientific": "BSX", "bristol myers squibb": "BMY",
    "broadcom": "AVGO", "broadridge": "BR", "brown forman": "BF.B",
    "cadence design systems": "CDNS", "campbell soup": "CPB",
    "capital one": "COF", "cardinal health": "CAH", "carrier global": "CARR",
    "caterpillar": "CAT", "cboe global markets": "CBOE", "cbre group": "CBRE",
    "cencora": "COR", "centene": "CNC", "centerpoint energy": "CNP",
    "charles schwab": "SCHW", "charter communications": "CHTR",
    "chevron": "CVX", "chipotle": "CMG", "chubb": "CB", "cigna": "CI",
    "cincinnati financial": "CINF", "cintas": "CTAS", "cisco systems": "CSCO",
    "citigroup": "C", "citizens financial": "CFG", "clorox": "CLX",
    "cme group": "CME", "coca cola": "KO", "cognizant": "CTSH",
    "colgate palmolive": "CL", "comcast": "CMCSA", "conagra brands": "CAG",
    "conocophillips": "COP", "consolidated edison": "ED", "constellation brands": "STZ",
    "constellation energy": "CEG", "copart": "CPRT", "corning": "GLW",
    "corteva": "CTVA", "costco": "COST", "coterra energy": "CTRA",
    "crowdstrike": "CRWD", "csx": "CSX", "cummins": "CMI", "cvs health": "CVS",
    "danaher": "DHR", "darden restaurants": "DRI", "deere": "DE",
    "delta air lines": "DAL", "devon energy": "DVN", "dexcom": "DXCM",
    "diamondback energy": "FANG", "digital realty": "DLR", "discover financial": "DFS",
    "dollar general": "DG", "dollar tree": "DLTR", "dominion energy": "D",
    "dover": "DOV", "dow": "DOW", "dte energy": "DTE", "duke energy": "DUK",
    "dupont": "DD", "eaton": "ETN", "ebay": "EBAY", "ecolab": "ECL",
    "edison international": "EIX", "edwards lifesciences": "EW",
    "electronic arts": "EA", "elevance health": "ELV", "eli lilly": "LLY",
    "emerson electric": "EMR", "enphase energy": "ENPH", "entergy": "ETR",
    "eog resources": "EOG", "equifax": "EFX", "equinix": "EQIX",
    "equity residential": "EQR", "essex property": "ESS", "estee lauder": "EL",
    "everest": "EG", "evergy": "EVRG", "eversource energy": "ES",
    "exelon": "EXC", "expedia": "EXPE", "exxon mobil": "XOM",
    "fastenal": "FAST", "federal realty": "FRT", "fedex": "FDX",
    "fifth third bancorp": "FITB", "first solar": "FSLR", "fiserv": "FI",
    "ford motor": "F", "fortinet": "FTNT", "fortive": "FTV",
    "fox": "FOXA", "franklin resources": "BEN", "freeport mcmoran": "FCX",
    "gartner": "IT", "ge aerospace": "GE", "ge healthcare": "GEHC",
    "general dynamics": "GD", "general mills": "GIS", "general motors": "GM",
    "genuine parts": "GPC", "gilead sciences": "GILD", "goldman sachs": "GS",
    "goldman sachs group": "GS", "halliburton": "HAL", "hartford": "HIG",
    "hasbro": "HAS", "hca healthcare": "HCA", "henry schein": "HSIC",
    "hershey": "HSY", "hess": "HES", "hewlett packard enterprise": "HPE",
    "hilton worldwide": "HLT", "home depot": "HD", "honeywell": "HON",
    "hormel foods": "HRL", "host hotels": "HST", "howmet aerospace": "HWM",
    "hp": "HPQ", "humana": "HUM", "huntington bancshares": "HBAN",
    "ibm": "IBM", "idex": "IEX", "idexx laboratories": "IDXX",
    "illinois tool works": "ITW", "illumina": "ILMN", "incyte": "INCY",
    "ingersoll rand": "IR", "intel": "INTC", "intercontinental exchange": "ICE",
    "international flavors & fragrances": "IFF", "international flavors and fragrances": "IFF",
    "international paper": "IP", "interpublic": "IPG", "intuit": "INTU",
    "intuitive surgical": "ISRG", "invesco": "IVZ", "iqvia": "IQV",
    "iron mountain": "IRM", "j m smucker": "SJM", "jabil": "JBL",
    "jacobs solutions": "J", "johnson & johnson": "JNJ", "johnson controls": "JCI",
    "jpmorgan chase": "JPM", "juniper networks": "JNPR", "kellanova": "K",
    "kenvue": "KVUE", "keurig dr pepper": "KDP", "keycorp": "KEY",
    "keysight technologies": "KEYS", "kimberly clark": "KMB", "kinder morgan": "KMI",
    "kla": "KLAC", "kraft heinz": "KHC", "kroger": "KR", "l3harris": "LHX",
    "labcorp": "LH", "lam research": "LRCX", "lennar": "LEN",
    "lincoln electric": "LECO", "linde": "LIN", "live nation": "LYV",
    "lockheed martin": "LMT", "loews": "L", "lowes": "LOW",
    "lyondellbasell": "LYB", "marathon petroleum": "MPC", "marriott": "MAR",
    "marsh & mclennan": "MMC", "martin marietta": "MLM", "masco": "MAS",
    "mastercard": "MA", "match group": "MTCH", "mcdonalds": "MCD",
    "mckesson": "MCK", "medtronic": "MDT", "merck": "MRK", "meta platforms": "META",
    "metlife": "MET", "mettler toledo": "MTD", "microchip technology": "MCHP",
    "micron technology": "MU", "microsoft": "MSFT", "mid america apartment": "MAA",
    "moderna": "MRNA", "mohawk industries": "MHK", "molson coors": "TAP",
    "mondelez": "MDLZ", "monster beverage": "MNST", "moodys": "MCO",
    "morgan stanley": "MS", "motorola solutions": "MSI", "nasdaq": "NDAQ",
    "netapp": "NTAP", "netflix": "NFLX", "newmont": "NEM", "news": "NWSA",
    "nextera energy": "NEE", "nike": "NKE", "nisource": "NI",
    "nordson": "NDSN", "norfolk southern": "NSC", "northern trust": "NTRS",
    "northrop grumman": "NOC", "norwegian cruise": "NCLH", "nrg energy": "NRG",
    "nucor": "NUE", "nvidia": "NVDA", "nxp semiconductors": "NXPI",
    "old dominion freight": "ODFL", "omnicom": "OMC", "oneok": "OKE",
    "oracle": "ORCL", "oreilly automotive": "ORLY", "otis worldwide": "OTIS",
    "paccar": "PCAR", "packaging corp": "PKG", "palantir": "PLTR",
    "palo alto networks": "PANW", "paramount": "PARA", "parker hannifin": "PH",
    "paychex": "PAYX", "paycom": "PAYC", "paypal": "PYPL", "pepsico": "PEP",
    "pfizer": "PFE", "pg&e": "PCG", "philip morris": "PM", "phillips 66": "PSX",
    "pinterest": "PINS", "pnc financial": "PNC", "pool": "POOL",
    "ppg industries": "PPG", "ppl": "PPL", "principal financial": "PFG",
    "procter & gamble": "PG", "progressive": "PGR", "prologis": "PLD",
    "prudential financial": "PRU", "public service enterprise": "PEG",
    "public storage": "PSA", "pultegroup": "PHM", "qualcomm": "QCOM",
    "quanta services": "PWR", "quest diagnostics": "DGX", "ralph lauren": "RL",
    "raymond james": "RJF", "realty income": "O", "regency centers": "REG",
    "regeneron": "REGN", "regions financial": "RF", "republic services": "RSG",
    "resmed": "RMD", "revvity": "RVTY", "rockwell automation": "ROK",
    "rollins": "ROL", "roper technologies": "ROP", "ross stores": "ROST",
    "royal caribbean": "RCL", "rtx": "RTX", "s&p global": "SPGI",
    "salesforce": "CRM", "sba communications": "SBAC", "schlumberger": "SLB",
    "seagate": "STX", "sempra": "SRE", "servicenow": "NOW", "sherwin williams": "SHW",
    "simon property": "SPG", "skyworks solutions": "SWKS", "smurfit westrock": "SW",
    "snap on": "SNA", "solventum": "SOLV", "southern": "SO",
    "southwest airlines": "LUV", "stanley black & decker": "SWK",
    "starbucks": "SBUX", "state street": "STT", "steel dynamics": "STLD",
    "steris": "STE", "stryker": "SYK", "synchrony financial": "SYF",
    "synopsys": "SNPS", "sysco": "SYY", "t rowe price": "TROW",
    "take two interactive": "TTWO", "tapestry": "TPR", "targa resources": "TRGP",
    "target": "TGT", "td synnex": "SNX", "te connectivity": "TEL",
    "teledyne": "TDY", "teleflex": "TFX", "teradyne": "TER", "tesla": "TSLA",
    "texas instruments": "TXN", "textron": "TXT", "thermo fisher": "TMO",
    "tjx": "TJX", "tmobile": "TMUS", "tractor supply": "TSCO",
    "trane technologies": "TT", "transdigm": "TDG", "travelers": "TRV",
    "trimble": "TRMB", "truist financial": "TFC", "tyler technologies": "TYL",
    "tyson foods": "TSN", "uber technologies": "UBER", "udr": "UDR",
    "ulta beauty": "ULTA", "union pacific": "UNP", "united airlines": "UAL",
    "united parcel service": "UPS", "united rentals": "URI",
    "unitedhealth": "UNH", "universal health": "UHS", "us bancorp": "USB",
    "valero energy": "VLO", "ventas": "VTR", "verisign": "VRSN",
    "verisk analytics": "VRSK", "verizon": "VZ", "vertex pharmaceuticals": "VRTX",
    "viatris": "VTRS", "vici properties": "VICI", "visa": "V",
    "vulcan materials": "VMC", "wabtec": "WAB", "walgreens boots": "WBA",
    "walmart": "WMT", "walt disney": "DIS", "warner bros discovery": "WBD",
    "waste management": "WM", "waters": "WAT", "wec energy": "WEC",
    "wells fargo": "WFC", "welltower": "WELL", "west pharmaceutical": "WST",
    "western digital": "WDC", "weyerhaeuser": "WY", "williams": "WMB",
    "willis towers watson": "WTW", "workday": "WDAY", "wynn resorts": "WYNN",
    "xcel energy": "XEL", "xylem": "XYL", "yum brands": "YUM",
    "zebra technologies": "ZBRA", "zimmer biomet": "ZBH", "zoetis": "ZTS",
}

SUFFIX_RE = re.compile(
    r"\b(inc|corp|corporation|co|company|ltd|limited|plc|the|class [ab]|"
    r"common stock|holdings|group|new|nv|sa|ag)\b\.?", re.IGNORECASE)


def resolve_ticker(name):
    """Company name to ticker. The filing gives us no ticker of its own."""
    key = SUFFIX_RE.sub(" ", name.lower())
    key = re.sub(r"[^a-z0-9& ]", " ", key)
    key = re.sub(r"\s+", " ", key).strip()
    if not key:
        return ""
    if key in TICKER_MAP:
        return TICKER_MAP[key]
    for k, v in TICKER_MAP.items():          # prefix match for truncated names
        if key.startswith(k) or k.startswith(key):
            if abs(len(k) - len(key)) <= 6:
                return v
    return ""


# The 278-T carries NO tickers. Rows are company names in plain text, and
# OCR mangles them further: "wurchase" for purchase, stray pipes, and amounts
# like $15,002 where the disclosure band is $15,001. So we anchor on the
# transaction word, tolerate a mangled first letter, and snap amounts to the
# nearest real disclosure band.

ACTION_WORD_RE = re.compile(
    r"\b(?:[a-z]{0,2}urchase|sale|sold|[a-z]?xchange|exchange)\b", re.IGNORECASE)

AMOUNT_LOOSE_RE = re.compile(
    r"\$?\s?(\d[\d,]{2,11})\s*[-\u2013\u2014~]\s*\$?\s?(\d[\d,]{2,11})")

DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")

# Real disclosure bands. OCR drift gets snapped back to these.
BANDS = [1_001, 15_001, 50_001, 100_001, 250_001, 500_001,
         1_000_001, 5_000_001, 25_000_001, 50_000_001]
BAND_TOPS = [15_000, 50_000, 100_000, 250_000, 500_000, 1_000_000,
             5_000_000, 25_000_000, 50_000_000, 100_000_000]


def snap(value, table):
    """Pull an OCR-drifted figure back to the nearest disclosure band."""
    best = min(table, key=lambda b: abs(b - value))
    return best if abs(best - value) <= max(50, best * 0.01) else value


NON_EQUITY_RE = re.compile(
    r"\b(bond|bonds|note|notes|treasury|municipal|muni|revenue|debenture|"
    r"certificate of deposit|money market|mortgage|obligation|"
    r"authority|school district|county of|city of|state of|university of|"
    r"L\.?L\.?C|L\.?P\.?|limited partnership|"
    r"preferred|fixed income|corporate credit|sr sec|senior notes|"
    r"due 20\d\d|\d\.\d{2,3}%)\b", re.IGNORECASE)

# A company rather than a bond issue or a fund.
COMPANY_HINT_RE = re.compile(
    r"\b(inc|corp|corporation|co|company|ltd|limited|plc|group|holdings|"
    r"technologies|systems|industries|international|nv|sa|ag|etf|trust)\b\.?$",
    re.IGNORECASE)


def clean_name(s):
    s = s.replace("\\", " ")
    # OCR prefixes every row with the line number, sometimes misread as a
    # letter ("s Howmet"), which otherwise blocks the ticker lookup.
    s = re.sub(r"^[\s\d|\[\]{}()<>.,;:_/-]+", "", s)
    s = re.sub(r"^[a-zA-Z]\s+(?=[A-Z])", "", s)
    s = re.sub(r"[|\[\]{}<>]", " ", s)
    s = re.sub(r"\b(sp|jt|dc|st|na|no|yes)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .,-|")


def norm_action(word):
    w = word.lower()
    if "urchase" in w:
        return "BUY"
    if w in ("sale", "sold"):
        return "SELL"
    return "EXCHANGE"


def extract_rows(text):
    """
    Line oriented, because each 278-T transaction occupies one line and the
    span approach used for House PTRs would let OCR noise bleed between rows.
    """
    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) < 12 or "$" not in line and "-" not in line:
            continue

        am = ACTION_WORD_RE.search(line)
        if not am:
            continue

        rest = line[am.end():]
        mo = AMOUNT_LOOSE_RE.search(rest)
        if not mo:
            continue

        try:
            low = snap(int(mo.group(1).replace(",", "")), BANDS)
            high = snap(int(mo.group(2).replace(",", "")), BAND_TOPS)
        except ValueError:
            continue
        if low < 1_000 or high <= low:
            continue

        td = ""
        dm = DATE_RE.search(rest[:mo.start()]) or DATE_RE.search(line)
        if dm:
            try:
                td = datetime(int(dm.group(3)), int(dm.group(1)),
                              int(dm.group(2))).strftime("%Y-%m-%d")
            except ValueError:
                td = ""

        asset = clean_name(line[:am.start()])
        if len(asset) < 3:
            continue

        rows.append({
            "ticker": resolve_ticker(asset),
            "asset": asset[:60],
            "action": norm_action(am.group(0)),
            "low": low, "high": high, "trade_date": td,
        })
    return rows


def is_equity(row):
    """
    No tickers exist in the source document, so a ticker cannot be the test.
    Instead: reject anything that reads like debt, and accept anything that
    reads like a company or that we could resolve to a ticker.
    """
    name = row["asset"]
    if NON_EQUITY_RE.search(name):
        return False
    if row["ticker"]:
        return True
    return bool(COMPANY_HINT_RE.search(name))


def aggregate(rows):
    """
    Roll up by ticker and direction. Four NVDA buys in one filing are one
    accumulation event, not four alerts. Direction and concentration are
    what matter here, not individual trade size.
    """
    agg = {}
    for r in rows:
        ident = r["ticker"] or re.sub(r"\s+", " ", r["asset"]).strip().lower()[:28]
        key = (ident, r["action"])
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

# Domino renders attachment links in a few shapes depending on the view.
PDF_LINK_RE = re.compile(r'href="([^"]*\.pdf[^"]*)"', re.IGNORECASE)
ATTACH_RE = re.compile(r'([A-F0-9]{32})[^"\'<>]*?\$FILE/([^"\'<>]+?\.pdf)',
                       re.IGNORECASE)
UNID_RE = re.compile(r'unid="([A-F0-9]{32})"', re.IGNORECASE)


def absolutise(href):
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return "https://extapps2.oge.gov" + href
    return "https://extapps2.oge.gov/" + href


def is_trump_278t(label):
    low = label.lower()
    if "trump" not in low:
        return False
    flat = label.upper().replace("-", "").replace(" ", "")
    return "278T" in flat


def harvest(html):
    """Pull every plausible Trump 278-T attachment URL out of a page."""
    out, seen = [], set()

    for href in PDF_LINK_RE.findall(html):
        full = absolutise(href)
        label = requests.utils.unquote(full.split("/")[-1])
        if is_trump_278t(label) and full not in seen:
            seen.add(full)
            out.append({"url": full, "label": label})

    for unid, fname in ATTACH_RE.findall(html):
        label = requests.utils.unquote(fname)
        if not is_trump_278t(label):
            continue
        full = f"{VIEW_BASE}/{unid}/$FILE/{fname}"
        if full not in seen:
            seen.add(full)
            out.append({"url": full, "label": label})

    return out


def find_filings(verbose=False):
    """Returns a list of {url, label} for Trump 278-T documents."""
    for url in INDEX_CANDIDATES:
        try:
            r = session.get(url, timeout=60)
        except requests.RequestException as e:
            log(f"index unreachable {url} :: {e}")
            continue
        if r.status_code != 200:
            log(f"index HTTP {r.status_code} {url}")
            continue

        found = harvest(r.text)
        if verbose:
            print(f"    {url}")
            print(f"      HTTP 200, {len(r.text)} chars, "
                  f"{len(PDF_LINK_RE.findall(r.text))} pdf hrefs, "
                  f"{len(ATTACH_RE.findall(r.text))} attachment refs, "
                  f"{len(UNID_RE.findall(r.text))} view entries, "
                  f"{len(found)} Trump 278-T")
            if not found:
                sample = re.findall(r'href="([^"]{10,110})"', r.text)[:5]
                for s in sample:
                    print(f"        sample href: {s}")

        if found:
            log(f"index ok: {url}, {len(found)} Trump 278-T documents")
            found.sort(key=lambda f: label_date(f["label"]), reverse=True)
            return found

    log("discovery failed, falling back to seed list")
    seeds = [{"url": u, "label": requests.utils.unquote(u.split("/")[-1])}
             for u in SEED_FILINGS]
    seeds.sort(key=lambda f: label_date(f["label"]), reverse=True)
    return seeds


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
        label = a["ticker"] or a["asset"][:16]
        lines.append((f"{a['action'][:4]} {label}",
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
    filings = find_filings(verbose=True)
    if not filings:
        print("  FAILED. No index responded and no seeds configured.")
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
            print(f"    {a['action']:5} {(a['ticker'] or a['asset'][:16]):16} "
                  f"{a['count']}x  {money(a['low'])} - {money(a['high'])}")
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

    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_FILING_AGE_DAYS))
    fresh, absorbed = [], 0
    for f in new:
        d = label_date(f["label"])
        try:
            fd = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc) if d else None
        except ValueError:
            fd = None
        if fd and fd < cutoff:
            seen.add(f["url"])
            absorbed += 1
        else:
            fresh.append(f)
    new = fresh
    log(f"{len(filings)} documents, {len(new)} recent, "
        f"{absorbed} older than {MAX_FILING_AGE_DAYS}d absorbed silently")

    for f in new[:4]:          # OCR is slow, cap the work per run
        process(f, seen)
        time.sleep(1)

    save_json(SEEN_FILE, list(seen)[-500:])
    log("trump done")


if __name__ == "__main__":
    main()
