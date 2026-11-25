# CLAUDE.md: SCOUT Filings Monitor ("CONGRESS" bot)

Project memory for Claude Code. Concise on purpose: details live in the files listed under **Documents**.

## What this is

A free, unattended, one-way Telegram alert bot for one person (Kiian, in Singapore). It reads public disclosure sources and posts filtered alerts to the private **SCOUT** channel, which it shares with other bots (OPTIONS, SNKRS, TCG). It never trades, has no commands and reads no Telegram messages. It is an awareness queue, not a trading signal.

Owner: not a developer. He deploys by merging pull requests on GitHub and by pasting whole files into the Google Apps Script editor. Give him full files, numbered steps, and a check he can see for each step. Plain language. Do not ask him technical questions you can answer from the code.

## Architecture

```
Apps Script "Filings monitor" (Google)          GitHub Actions (this repo, public)
  Kick.gs   every 5 min ── workflow_dispatch ──►  monitor.yml (always), halts.yml (US market hours),
            also kicks congress.yml every 30 min,   congress.yml, earnings.yml / events.yml (Mon 07:00 SGT)
            the Monday digests, and csp-scanner     every workflow also has a cron as a backup
  Status.gs SCOUT STATUS card (status link,       each run commits state/*.json and data/*.csv to main
            daily 09:00 if stale) + stamp endpoint + JSON mode for Builds HQ
        ▲ reads state/*.json from this repo                 │
        └──────────── Telegram SCOUT channel ◄───────────────┘
```

- **The repository is the database.** `state/` = JSON (seen lists, queues, health stamps, caches, outboxes). `data/` = CSV history. Each workflow commits only its own files (one writer per file).
- **GitHub cron drifts 5 to 60+ minutes**, so the Apps Script clock (`Kick.gs`) starts workflows on time; crons stay as backup.
- The Apps Script code is **not in this repo** (`Config.gs` holds the private chat id). Live project: "Filings monitor" in the kiianwanted Google account. Last known-good copies: owner's local folder `~/Desktop/Claude Audits/CONGRESS/apps-script/new/` (with a Node test suite in `apps-script/tests/`).

## Stack

Python 3.11 on `ubuntu-24.04` GitHub runners. `requests`, `pdfplumber`, `pytesseract` + `pdf2image` (OCR for Trump PDFs), exact pins in `requirements.txt`. Tests: `pytest`, `pyflakes` (`requirements-dev.txt`). Apps Script (V8). Telegram Bot API `sendMessage` (HTML mode).

## Files

| File | Responsibility |
|---|---|
| `monitor.py` | SEC EDGAR: Form 4 insider buys and clusters, Form 144, 13D/13G stakes; CNN Fear & Greed; the heartbeat. **Also the shared library**: `card()` message layout, `telegram()` delivery with retries and per-module outbox, state helpers (`load_json`, `save_json`, `Ledger`, `stamp`, `due`), `dmy()`, `money()`, NYSE calendar |
| `halts.py` | Nasdaq Trader halts RSS: T1, H10 and market-wide circuit breakers (MWC0-3, MWCQ); `data/halts.csv` history |
| `congress.py` | House Clerk PTR index ZIP + PDFs (pdfplumber): one message per filing; `data/congress_trades.csv` |
| `trump.py` | OGE Form 278-T PDFs found via open-cabinet.org, downloaded only from `*.oge.gov`, OCR; `data/trump_trades.csv` |
| `earnings.py` | Monday digest: which of 107 large caps report this week (Nasdaq calendar, Finnhub fallback if a key is set) |
| `events.py` | Monday digest: high-impact US economic events in Singapore time (Forex Factory JSON) |
| `tests/` | 291 pytest tests, network blocked, fake Telegram, every test sandboxed to a temp `state/` and `data/` (autouse fixture) |
| `.github/workflows/` | monitor, halts, congress, trump, earnings, events, heartbeat, diagnostics, tests |

## Data (repo-as-database)

| File | Writer | Holds |
|---|---|---|
| `state/seen.json`, `queue.json`, `first_seen.json`, `cluster_alerts.json`, `sec_companies.json`, `fear_greed.json`, `schedule.json`, `health.json` | monitor.yml | SEC seen keys (cap 20,000), retry queue, feed grace, cluster cooldowns, ticker map, F&G level, job timers, health stamps |
| `state/halts_seen.json`, `halts_state.json` | halts.yml | seen halt keys (cap 4,000; market-wide = one `MW|code|date` key), health stamp read by Status.gs |
| `state/congress_seen.json`, `congress_health.json`, `congress_retry.json` | congress.yml | seen filings, health, PDF retry counts |
| `state/trump_seen.json`, `trump_state.json` | trump.yml | seen documents, health |
| `state/earnings_state.json`, `events_state.json` | earnings.yml / events.yml | `sent_week` / `sent_at` (send once per week), failure flags |
| `state/outbox_<module>.json` | that module | undelivered messages, max 50, dropped after 48 h. Absent when empty |
| `data/alerts.csv`, `buys.csv` | monitor.yml | every SEC alert (365 days), every insider buy (180 days) |
| `data/congress_trades.csv` | congress.yml | every parsed trade, last column `asset_type` (`ST`, `OP`, `GS`...) |
| `data/trump_trades.csv` | trump.yml | every equity row |
| `data/halts.csv` | halts.yml | new halts except LUDP/LUDS, rolling year (created by the first new halt) |

## Configuration (names only, never values)

GitHub repository secrets: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `SEC_USER_AGENT` (required); `FINNHUB_KEY` (optional, not set: market cap floor and earnings fallback off). Env flag `SENATE_ENABLED` (off: the Senate site blocks GitHub). Apps Script Script Properties: `TELEGRAM_TOKEN`, `GH_DISPATCH_TOKEN` (fine-grained, Actions on filings-monitor and csp-scanner, no expiry by owner's choice), `STATUS_KEY`, `SPREADSHEET_ID`. Thresholds are named constants at the top of each module with a comment saying why (see README "dials").

## Messages

Every alert is built with `monitor.card()`, the same shape as the OPTIONS bot:
`<b>TITLE · TICKER</b> · PRIORITY` / plain subtitle / plaintext number block (32 wide, 11-char label column, wraps under its column) / optional `<b>Do:</b>` / `<i>note</i>` / short tappable link. `telegram()` adds the `CONGRESS` chat-list header and the 🏛️ tag. Apps Script messages use the `WARNINGS` header and 🩺.

## Conventions

- Dated comment next to every non-obvious change saying why ("Fixed 18 Nov 2026: ..."). Constants, not magic numbers.
- Fail visibly: a parse failure is reported, an unknown ticker stays blank (no guessing, no fuzzy tickers).
- Mark a filing/PDF/halt seen only after it was handled; a failed download goes back on the queue.
- Delivery is at-least-once: retries + outbox; a rare duplicate beats a lost alert.
- Messages: all times Singapore; dates `20 Nov`; no emoji inside a block; titles without emoji.
- Commits by Claude use the GitHub no-reply address. Never commit `state/` or `data/` (the workflows own them).

## Testing

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q                      # 291 tests, ~2 s, no network
python -m pyflakes *.py tests/*.py       # prints nothing when clean
```
Read a sent message in a test with `body_of(tg.texts[0])`; `flat()` undoes block wrapping; `rows_of()` gives block lines. For a threshold or parser change, replay the committed CSVs (or real filings) old vs new before shipping. The Tests workflow runs pytest + pyflakes on every pull request. Apps Script: `cd apps-script && node --test tests/*.test.js` in the owner's local folder (70 tests incl. an options-scanner parity test).

## Deployment

- Python: pull request → green **Tests** → merge. The `gh` login on the owner's Mac is **kiianlow** (read-only here): push a branch to the fork `kiianlow/filings-monitor` (its Actions are off) and open a PR. Merging needs the owner's account (kiianwanted-byte), e.g. the desktop app's browser pane when he has signed in there and asked you to merge.
- Apps Script: owner pastes whole files (put one on his clipboard with `LANG=en_US.UTF-8 pbcopy < file`, then verify the saved copy by fingerprint). After any `Status.gs` change: Deploy > Manage deployments > pencil > **New version**. Never "New deployment" (new URL, SNKRS/TCG/CSP go stale silently). Never type code into the Apps Script editor (it auto-inserts characters). Then run `testStatusJson` (prints the JSON mode, posts nothing). Every new `Status.gs` keeps the JSON mode: Builds HQ reads it (`DEPLOYMENT.md`, "Status.gs JSON mode").
- After a Python deploy: watch the next Filings Monitor run; run **Diagnostics** and **Filings Heartbeat** by hand.
- Full procedure, checks and rollback: `DEPLOYMENT.md`.

## Constraints (each has broken production before)

1. Free only; the repo stays **public** (Actions minutes). No paid APIs.
2. One writer per state/data file; separate concurrency groups; checkout uses `ref: ${{ github.ref_name }}` so a queued run starts from the newest state.
3. A failed state commit never fails a job (`continue-on-error`, rebase retries, `exit 0`).
4. Keep `ubuntu-24.04`, Python 3.11, `actions/checkout@v4`, `actions/setup-python@v5`, and the workflow **file names** (Kick.gs dispatches by name).
5. Status contract: `halts.py` writes `state/halts_state.json`, `monitor.py` writes ISO stamps to `state/health.json`; `Status.gs` reads both.
6. Heartbeat silent when healthy; a red heartbeat run means Telegram refused the bot (that red run is what emails the owner).
7. Every message keeps the CONGRESS header, 🏛️ tag, plaintext block, and buzzes (`disable_notification: false`).
8. No token or key in code, docs or logs (`scrub()` redacts log lines). The chat id is private too.
9. `Kick.gs` is shared with the OPTIONS bot (`csp-scanner`): its CSP block must stay byte-identical (parity test).
10. Never run or recreate the removed `setup()` / `deleteTriggers()`; use `checkTriggers` / `repairTriggers`.
11. Audit reports (incl. a security report) stay out of this public repo; they are in the owner's local `audit/` folder.

## Documents

| File | Read it for |
|---|---|
| `CURRENT_STATE.md` | what is deployed and verified right now |
| `PROJECT_HANDOVER.md` | history: original design, what the Nov 2026 audit found and changed |
| `DECISIONS.md` | why things are the way they are, and which decisions are open |
| `NEXT_STEPS.md` | what to do next, and what awaits the owner |
| `README.md` | overview, dials, how to run |
| `DEPLOYMENT.md` | deploy, verify, roll back, recover |
| `TEST_PLAN.md` | what is and is not tested |
| `CHANGELOG.md` | every change, newest first |

## INSTRUCTIONS FOR FUTURE CLAUDE CODE SESSIONS

Before making changes to this project:

1. Read CLAUDE.md.
2. Read CURRENT_STATE.md.
3. Read PROJECT_HANDOVER.md.
4. Review DECISIONS.md.
5. Check NEXT_STEPS.md.
6. Inspect the current Git status.
7. Verify the actual implementation before relying on documentation.

Treat the source code as the primary source of truth. If documentation conflicts with the implementation, identify the discrepancy and recommend an update.

Do not repeat completed work without a reason. Do not reverse previous architectural decisions without evaluating the original reasoning (DECISIONS.md).

Update the relevant documentation (this file, CURRENT_STATE.md, CHANGELOG.md, and the others where affected) whenever future development changes the project.
