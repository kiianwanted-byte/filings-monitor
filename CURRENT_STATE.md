# Current state (handover, 18 Nov 2026, about 16:00 SGT)

"Verified" below means seen working in a live run or proven by a test result named here. Everything else is marked unverified.

## Version

No version number. The release is "audit + redesign, 18 Nov 2026": pull requests #1 to #7 merged into `main`, last code merge `fbd9ba28d` (PR #7, 07:32 UTC). `main` moves on every few minutes with the bots' own `state:` / `halts:` / `congress:` commits; those never contain code. Apps Script "Filings monitor" deployment: **Version 10** (15:40 SGT).

## Architecture

GitHub Actions (this public repo, Python 3.11, ubuntu-24.04) + repo as database (`state/`, `data/`) + Google Apps Script clock and status card + Telegram SCOUT channel. See `CLAUDE.md`.

## Features and their status

| Feature | Status | Evidence |
|---|---|---|
| Form 4 insider buys, clusters, watchlist buys | Code verified by tests; live runs green | 291 tests; Filings Monitor runs green after every merge (last checked 07:36 UTC); no buy alert observed live since the redesign |
| EDGAR feed paging | **Verified live** | Diagnostics 18 Nov: `paging ... OK` |
| Form 144 sell notices, 13D/13G watchlist stakes | Tests only | no live alert observed since the changes |
| Fear & Greed level change | Tests only | |
| Trading halts T1/H10 | Live runs green; tests | halts runs 05:38 and 05:49 UTC read the feed; 06:17 UTC was bot-checked twice and reported it (see limitations). Market was closed: no live halt alert since the changes |
| Market-wide circuit breakers | **Unverified** (tests only) | none since March 2020; the feed layout is unknown, the handler does not depend on it |
| Halts history `data/halts.csv` | **Unverified** | created by the first new halt; none yet |
| Congress filings (House) | Live runs green; parser verified on 411 real filings | congress runs 05:51 and 06:21 UTC (kicked by Kick.gs), nothing new to send |
| Congress options labels, `asset_type` column | Verified on real filings (replay); header upgrade verified live | `data/congress_trades.csv` header has `asset_type`, rows byte-identical |
| Trump 278-T | Live run green | manual run 06:32 UTC, nothing new |
| Monday earnings and events digests | **Unverified live** | send-once and week logic tested; first live run Mon 23 Nov 07:00 SGT |
| Heartbeat (12 h / 48 h limits, Telegram check) | **Verified live** | manual run 18 Nov: "10 checks healthy", Telegram check passed |
| Telegram retries and outbox | Tests only | not triggered live |
| Card message layout (Python alerts) | Verified in tests | all 79 test messages compared with the old layout: every value present, valid HTML, no emoji in blocks, block lines ≤ 32. **Not yet seen on the owner's phone** |
| SCOUT STATUS card (Apps Script, card layout) | **Verified live** | Version 10 doGet 15:41 SGT completed; owner confirmed the card arrived |
| Status JSON mode for Builds HQ (`&format=json`, posts nothing) | **Verified live** | owner deployed it on 22 Nov 2026 as a new version of the same deployment and checked the JSON answer; Builds HQ reads it for its SCOUT dots. The status link message was unpinned on 23 Nov 2026 and stays in the channel history |
| Kick.gs: filings, halts (market hours), congress (30 min), CSP, Monday digests | Filings/congress/CSP kicks **verified live** (executions completed every 5 min, congress dispatched 05:51 and 06:21 UTC); Monday digest kicks unverified until 23 Nov | |
| Senate trades | Disabled | the Senate site blocks GitHub |

## Integrations

| Service | Status |
|---|---|
| SEC EDGAR (atom feed, filing index, XML) | working (live runs, Diagnostics) |
| Nasdaq Trader halts RSS | working, with occasional bot-check pages (about 1 in 80 runs) |
| House Clerk index ZIP and PDFs | working (Diagnostics: 473 PTRs in the 2026 index) |
| open-cabinet.org index → OGE PDFs | working (Diagnostics: 20 documents) |
| CNN Fear & Greed | working (Diagnostics) |
| Nasdaq earnings calendar | working (Diagnostics) |
| Forex Factory weekly JSON | this week working; next week 404 until it is published (normal) |
| Telegram Bot API | working (heartbeat check, status card) |
| Finnhub | not configured |

## Database (repo files)

All files in the `CLAUDE.md` table exist except `data/halts.csv` (first new halt) and `state/outbox_*.json` / `state/congress_retry.json` (only when needed). `state/earnings_state.json` and `events_state.json` are still in the pre-audit format (no `sent_week`); the first Monday run writes the new keys.

## Dependencies

`requests==2.34.2`, `pdfplumber==0.11.10`, `pytesseract==0.3.13`, `pdf2image==1.17.0`; dev `pytest==9.2.0`, `pyflakes==4.1.0`. Transitive packages float. Tesseract and poppler are installed by `trump.yml` and `diagnostics.yml`.

## Configuration required

GitHub secrets `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `SEC_USER_AGENT` (set). Apps Script Script Properties `TELEGRAM_TOKEN`, `GH_DISPATCH_TOKEN`, `STATUS_KEY`, `SPREADSHEET_ID` (set; the token was checked identical to the old in-code copy before `Config.gs` was replaced). Triggers: `kickFilings` (5 min) and `scoutStatusDaily` (daily), once each (checked).

## Tests

| Suite | Result |
|---|---|
| `python -m pytest` (291) | Passed 291/291 locally (macOS, Python 3.11.18) and on GitHub (PR #7 Tests run) |
| `pyflakes *.py tests/*.py` | clean |
| Apps Script Node suite (70, owner's local folder) | Passed 70/70, including the options-scanner parity test |
| Apps Script Node suite with the JSON-mode tests (140, owner's local folder, 22 Nov 2026) | Passed 140/140 |

## Deployment

Python: merged and running. Apps Script: Core, Status, Kick, Setup, Config pasted and verified by fingerprint against the tested files; `Halts.gs` deleted; Version 10 deployed. Owner settings: failed-workflow emails on, private commit email on (checked).

## Outstanding bugs

None known.

## Known limitations

- GitHub cron drifts; Kick.gs covers monitor, halts (US market hours only), congress, the digests. Trump and heartbeat rely on cron.
- Nasdaq's bot filter blocks about 1 in 80 halts runs; that halt arrives one run (about 5 minutes) late.
- A Telegram read timeout is retried and can duplicate a message (accepted, D3).
- Congress: scanned or handwritten PTRs cannot be read; they are announced as "OPEN MANUALLY". Options detail (strike, expiry) is not extracted.
- Trump PDFs are OCR'd; ticker resolution covers the names seen so far (aliases in `trump.py`).
- Form 4 holding change is approximate on joint filings.
- 13D/13G alerts are watchlist-only; no market cap floor (no Finnhub key).
- Repo history grows by about 290 commits a day.
- Card width 32 is the OPTIONS bot's choice, not measured on the owner's phone.

## Incomplete features

None in progress. Optional ideas are in `NEXT_STEPS.md`.

## Git state at handover

- Branch `main` on `kiianwanted-byte/filings-monitor`. Last code merge `fbd9ba28d` (PR #7). Later commits on `main` are the bots' state commits.
- Important commits (merges of PRs #1 to #7): `d2679b829` audit and upgrade, `e1186e8df` halts bot-check retry, `72898c3cc` independent-review fixes, `bd52c983f` gentler halts retry, `c34bb10a7` second-review fixes, `197c9efd2` tidier boxes, `fbd9ba28d` card redesign.
- This handover (CLAUDE.md, CURRENT_STATE.md, PROJECT_HANDOVER.md, DECISIONS.md, NEXT_STEPS.md, and edits to README.md, TEST_PLAN.md, DEPLOYMENT.md) is a docs-only change made after `fbd9ba28d`; check `git log` for whether it has been merged.
- No other uncommitted work was left in the owner's local clone.
