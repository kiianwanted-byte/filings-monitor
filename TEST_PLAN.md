# Test plan

## How to run

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pyflakes *.py tests/*.py
python -m pytest
```

The **Tests** workflow runs the same three commands on every pull request, on every code change to `main`, and on demand. It reads no secrets and sends nothing.

## How the tests are isolated

Every test runs in a sandbox set up by `tests/conftest.py`:

- `state/` and `data/` are a temporary folder. The repo's real files are never written.
- The network is blocked. Any real request fails the test.
- Telegram is a fake that records what would have been sent, so tests assert on the exact message text.
- Nothing sleeps, so the suite takes about two seconds.

One test reads a real repo file on purpose: the earnings watchlist is checked against the committed `state/sec_companies.json`, so a ticker that stops being listed (a rename) fails the suite.

## What is covered

| File | Tests | Covers |
|---|---|---|
| `test_telegram.py` | 33 | message format, the chat-list header, the exact Telegram payload, retries, rate limits, plain-text fallback, the outbox, the outbox cap and expiry, token scrubbing, the channel check |
| `test_sec.py` | 59 | EDGAR discovery and paging, the retry queue, Form 4 decisions (thresholds, 10b5-1, amendments, watchlist, $10M override, missing ticker, share classes, holding change), clusters (dollar floor, cooldown, milestone), Form 144, 13D/13G, ticker cleaning, name matching |
| `test_health.py` | 39 | heartbeat (limits, grace period, module errors, undelivered alerts, Telegram down), NYSE holidays, market hours, state helpers, CSV pruning, the weekly send-once rule, Fear and Greed |
| `test_modules.py` | 105 | halts (bot-check retry, feed parsing, codes, market-wide halts, grouping, New York and Singapore times, failure stamps, the halts history), congress (row parsing on real filing layouts, page breaks, same-day trades, option tags, equity filter, member watchlist, retries, alert cap, year rollover, trade log upgrade), Trump (ticker resolution, OCR rows, aggregation, discovery fallback) |
| `test_digests.py` | 20 | earnings and events: week selection, send once per week, `--force`, failure announced once, incomplete data, time zones |
| `test_repo.py` | 35 | the rules in the README: workflow pins, timeouts, commit steps that cannot fail a job, branch name passed through env, one writer for the halts history, separate concurrency groups, pinned dependencies, no credentials in the repo, the status contract, one writer per state file |

Total: 291.

## Results, 18 Nov 2026

| Check | Result |
|---|---|
| `pyflakes` on all code and tests | Passed. 0 warnings (19 before the audit) |
| `pytest`, Python 3.11.18, macOS | Passed. 291 of 291 |
| Every module run end to end with the network blocked and Telegram unset, against a copy of the live `state/` and `data/` | Passed. No exceptions. `monitor.py heartbeat` exits 1, as designed, because Telegram is not configured |
| Old and new Telegram payloads compared for 7 message types (first pass, before the redesign) | Passed. Byte-identical |
| Card redesign: all 79 messages the suite produces compared with the previous layout | Passed. Every value present, valid Telegram HTML, no emoji inside a block, no block line over 32 characters |
| Congress parser, old and new, over 411 real House filings (second pass) | Passed. 3,458 trades unchanged, 36 tickers recovered, 11 amounts fixed, 263 previously dropped trades kept, none lost |
| Trump ticker resolution, old and new, over all 835 rows in `data/trump_trades.csv` | Passed. 807 unchanged, 28 changed, every change a correction or a newly resolved name, none lost |
| Cluster rule replayed over `data/buys.csv` | Passed. Replay of the old rule gives 191 alerts in 30 days against 188 actually logged; the new rule gives 106 |
| Tests workflow on GitHub (`ubuntu-24.04`) | Passed on every pull request #1 to #7; 291 of 291 on #7 |
| Live EDGAR, House Clerk, OGE, Nasdaq, CNN, earnings calendar and Telegram | Passed after deploy: Diagnostics and Filings Heartbeat green on 18 Nov; live runs of every module except the Monday digests green. See `CURRENT_STATE.md` |
| A real market-wide circuit breaker in the Nasdaq feed | Blocked. The last one was March 2020, so the feed's layout for it has never been observed. The handler is built not to depend on that layout |
| EDGAR feed paging (`start=`) against the live feed | Passed. Diagnostics on 18 Nov: `paging ... OK` |
| Monday digests live (send once, Kick.gs at 07:00 SGT) | Not executed yet. First run Mon 23 Nov 2026 |

## What is not covered

- The PDF text extraction itself (`pdfplumber`, Tesseract). Tests start from extracted text.
- The diagnostic print modes (`feeds`, `stakes`, `fng`, the `test` modes). They only print.
- The disabled Senate code path.
- GitHub Actions behaviour: cron timing, concurrency queues, the commit step's `git` commands. `test_repo.py` checks the workflow text, not a run.

## Adding a test

Every test runs in the `sandbox` (autouse: a temporary `state/` and `data/`, so no test can touch the real files). Use the `tg` fixture for anything that sends. Read delivered messages with `body_of(tg.texts[0])`, which also asserts the header and the bank emoji are there; `flat()` undoes block wrapping and `rows_of()` returns the block's lines. For a change to a threshold or filter, add a case that pins the new number and replay the committed CSVs before shipping.

## Apps Script

`Kick.gs`, `Status.gs`, `Core.gs`, `Config.gs` and `Setup.gs` are not in this repo. They have their own suite (Node, with stand-ins for the Google services; 70 tests on 18 Nov 2026, 140 with `json-mode.test.js` since 22 Nov 2026), including a parity test that drives the old and new `Kick.gs` through the same schedule and asserts the options scanner's kicks are identical.
