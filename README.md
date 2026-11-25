# SCOUT Filings Monitor

Scheduled monitors that read public disclosure sources and post filtered alerts to one private Telegram channel. Everything it reads is public. It has no broker connection, places no trades, and does not read incoming Telegram messages: there are no commands, only outgoing alerts.

It is an awareness and research queue, not a trading signal. The aim is to know about a filing the moment it becomes public, and to never be caught by a scheduled event.

## What it watches

| Module | Source | Alerts on | Runs |
|---|---|---|---|
| `monitor.py` Form 4 | SEC EDGAR | open-market insider buys, insider clusters, watchlist names | every 5 min |
| `monitor.py` Form 144 | SEC EDGAR | proposed insider sales of $25M or more | every 5 min |
| `monitor.py` 13D / 13G | SEC EDGAR | 5% stakes by watchlist filers | every 5 min |
| `monitor.py` Fear and Greed | CNN | a change of level (for example fear to neutral) | hourly |
| `halts.py` | Nasdaq Trader | halts with code T1 (news pending), H10 (SEC suspension), market-wide circuit breakers; history in `data/halts.csv` | every 5 min in US market hours |
| `congress.py` | House Clerk | congressional stock trades, one message per filing | every 30 min |
| `trump.py` | OGE Form 278-T | Trump account equity trades of $1M or more, aggregated | daily |
| `earnings.py` | Nasdaq calendar | Monday digest: which of 107 large caps report this week | weekly |
| `events.py` | Forex Factory feed | Monday digest: high-impact US economic events, Singapore time | weekly |
| `monitor.py heartbeat` | this repo's state files | problems only, silent when healthy | every 6 h |

`monitor.py` is also the shared library. Every other module imports its message formatting, Telegram delivery and state helpers from it.

## How it runs

```
Apps Script "Filings monitor"                GitHub Actions (this repo)
  Kick.gs    every 5 min  ── dispatch ──►    monitor.yml, halts.yml, congress.yml,
  Status.gs  daily + status link + JSON        earnings.yml, events.yml
        │                                    cron backups on every workflow
        │ reads state/*.json                 each run commits state/ and data/ back
        ▼                                            │
  Telegram SCOUT channel  ◄──────────────────────────┘
```

- **The repository is the database.** `state/` holds JSON (what has been seen, health stamps, caches) and `data/` holds CSV history. Each workflow commits its own files back to `main`.
- **GitHub cron is unreliable**, so `Kick.gs` (Google Apps Script, not in this repo) starts the workflows on a dependable clock. The crons stay as a backup.
- **Free only.** The repo must stay public: private repos get 2,000 Actions minutes a month and this uses far more.

## Configuration

Secrets live in **Settings > Secrets and variables > Actions**. Nothing secret is ever in code. See `.env.example` for the names.

| Secret | Required | Used for |
|---|---|---|
| `TELEGRAM_TOKEN` | yes | the bot that posts |
| `TELEGRAM_CHAT_ID` | yes | the channel to post to |
| `SEC_USER_AGENT` | yes | your name and email, which the SEC requires on every request |
| `FINNHUB_KEY` | no | market cap floor for insider buys, and an earnings fallback. Not set today |

The dials are plain constants at the top of each file, each with a comment saying why it has its value:

| Dial | File | Now | Meaning |
|---|---|---|---|
| `MIN_TRADE_VALUE`, `MIN_HOLDING_CHANGE_PCT` | monitor.py | $250K, 20% | a single insider buy must clear both |
| `CLUSTER_MIN_TOTAL` | monitor.py | $250K | combined buying before a cluster alerts. `0` turns the floor off |
| `FORM144_MIN_VALUE` | monitor.py | $25M | proposed sale size |
| `NOTABLE_FILERS` | monitor.py | list | names that always alert at HIGH |
| `STAKES_NOTABLE_ONLY` | monitor.py | `True` | 13D/13G alerts for the watchlist only |
| `ALERT_ON` | halts.py | T1, H10, MWC* | halt codes that alert |
| `NOTABLE_MEMBERS`, `PUSH_MIN_AMOUNT` | congress.py | list, $50,001 | who and what size |
| `MIN_AMOUNT` | trump.py | $1,000,001 | aggregate size per ticker |
| `WATCHLIST` | earnings.py | 107 tickers | companies in the Monday digest |

## Messages

Every alert is a card in the same shape as the OPTIONS bot's, which shares the SCOUT channel (`monitor.card()`):

```
INSIDER BUY · ACME · MEDIUM          bold title: what, ticker, priority
Acme Corp · open-market buy          plain subtitle
Person     Doe Jane                  aligned block, plaintext, 32 wide
Shares     10,000 at $30.00
Value      $300K
Do: ...                              only when there is something to do
Bought on the open market...         note, in italics
Open SEC filing                      short tappable link
```

All times are Singapore time. There is no emoji in a title or inside the block; the 🏛️ that starts every message is the bot's tag.

## Delivery

Every message goes through `monitor.telegram()`, which adds the `CONGRESS` chat-list header and the bank emoji, retries on a network error, a Telegram server error or a rate limit, and falls back to plain text if Telegram rejects the formatting. A message that still cannot be delivered is saved to `state/outbox_<module>.json` and sent by that module's next run with a "Delayed delivery" line. Outbox entries older than 48 hours are dropped.

## Run it yourself

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
export SEC_USER_AGENT="Your Name you@example.com"

python -m pytest              # the test suite: no network, sends nothing
python halts.py test          # connectivity check, sends nothing
python monitor.py feeds       # EDGAR probe, sends nothing
```

Without `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` set, a normal run prints its messages instead of sending them. A normal run writes to `state/` and `data/`, so do not commit those from your machine: the workflows own them.

Each module has a `test` mode that prints what it can reach and what it would do. The **Diagnostics** workflow runs all of them on GitHub.

## Tests

`python -m pytest` runs 291 tests in about two seconds. They use a temporary folder for state, a fake Telegram, and block the network. The **Tests** workflow runs them, plus `pyflakes`, whenever code changes. See `TEST_PLAN.md`.

## Rules that must not be broken

Each of these has taken the live bot down at least once. `tests/test_repo.py` checks most of them.

1. The repo stays public and free. No paid APIs.
2. One writer per state file. Two workflows writing the same file collide on `git push`.
3. A failed state commit never fails a job (`continue-on-error`, retries, `exit 0`).
4. No guessing. An unknown ticker stays blank; a parse failure is reported, not papered over.
5. Code commits never touch `state/` or `data/`.
6. `ubuntu-24.04`, Python 3.11 and the workflow file names stay as they are. `Kick.gs` starts workflows by file name.
7. `halts.py` keeps writing `state/halts_state.json` and `monitor.py` keeps writing ISO timestamps to `state/health.json`. `Status.gs` reads both.
8. Every message keeps the `CONGRESS` header, the bank emoji, the plaintext code block, and buzzes.
9. The heartbeat stays silent when healthy.
10. No token or key in code, docs or logs.

## Documents

| File | What it is for |
|---|---|
| `CLAUDE.md` | project memory and rules for Claude Code sessions (start here) |
| `CURRENT_STATE.md` | what is deployed, what is verified and what is not |
| `PROJECT_HANDOVER.md` | history: original design, the November 2026 audit, what changed |
| `DECISIONS.md` | why things are as they are; which decisions are open |
| `NEXT_STEPS.md` | what to watch, optional improvements, owner decisions |
| `DEPLOYMENT.md` | how to deploy, verify and roll back |
| `TEST_PLAN.md` | what is tested and what is not |
| `CHANGELOG.md` | what changed and why, newest first |
