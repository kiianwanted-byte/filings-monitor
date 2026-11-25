# Changelog

Newest first. One entry per deploy. Each entry says what changed, why, how it was tested, and anything decided on the owner's behalf so it can be reversed.

## 2026-11-22 — Status.gs JSON mode for Builds HQ (Apps Script only)

The status link with `&format=json` added now answers with data instead of a card, for the green and red health dots on Kiian's private Builds HQ site. No Python file, alert rule, threshold, schedule, state file or CSV changed.

- The JSON mode returns the six rows (HALTS, SNKRS, TCG, FILINGS, CSP, KICK). Each row has `name`, `stale`, `error`, `age_ms`, `limit_hours` and `detail` (the advice, only when stale). It posts nothing to SCOUT. The key check still comes first.
- Unchanged: the plain link still posts the SCOUT STATUS card, `scoutStatusDaily` still posts at 09:00 when something is stale, and stamp mode is the same.
- New editor function `testStatusJson`: prints the JSON and posts nothing. Run it after any `Status.gs` change.
- Builds HQ calls only the JSON mode: when one of its pages is open, or when Kiian's iPhone widget refreshes, at most once a minute per Builds HQ server. After a failed call it waits 5 minutes (20 seconds when a newly started server got no answer at all). It maps SCOUT SNKRS to the SNKRS row, SCOUT TCG to TCG, SCOUT OPTIONS to CSP, and SCOUT CONGRESS to HALTS, FILINGS and KICK (red if any of the three is). The link lives only in Builds HQ's `SCOUT_STATUS_URL` setting on Vercel.
- Deployed by the owner on 22 Nov 2026 as a new version of the existing deployment, so the web address did not change. `Status.gs` went from 542 to 613 lines. He checked the JSON answer.
- 23 Nov 2026: the owner unpinned the status link message in SCOUT, since he now checks bot health on Builds HQ. The message is still in the channel history.

Tested: 140 of 140 Apps Script tests pass, with the new `json-mode.test.js`, run against both the new file and the 18 Nov files.

Reverse: first ask the owner to set Builds HQ's `SCOUT_STATUS_JSON` to `0`. The 18 Nov file has no JSON mode, so each Builds HQ call would get the plain link's behaviour and post a SCOUT STATUS card, up to one every 5 minutes per Builds HQ server, and its four SCOUT dots would show "Can't check". Then paste back the 18 Nov `Status.gs` (Drive, CONGRESS folder, `apps-script/Status.gs (18 Nov 2026, before JSON mode).txt`) and redeploy as a new version.

## 2026-11-18 — message redesign: cards in the OPTIONS bot's shape

Every CONGRESS message now has the same shape as the OPTIONS bot's, since they share SCOUT. Cosmetic: no alert rule, threshold, schedule, state file or CSV changed.

| Part | What it looks like |
|---|---|
| Title | one bold line: `INSIDER BUY · ACME · MEDIUM`, `TRADING HALT · ABC · HIGH`, `CONGRESS FILING · HIGH · ★ WATCHLIST`. The priority is the tag after the title instead of a PRIORITY row; the ticker is in the title instead of a TICKER row |
| Subtitle | what it is, in plain words: `Acme Corp · open-market buy`, `Nancy Pelosi · House (CA11)` |
| Number block | title-case labels in an 11-character column, no line over 32 characters (the OPTIONS bot's width), long values wrap under their own column. Still marked plaintext so Telegram does not colour it |
| Do line | **Do:** one instruction, only where there is something to do (heartbeat, delayed digests, unreadable congress PDF) |
| Note | the context sentence in italics |
| Link | a short tappable link (`Open SEC filing`, `Open Nasdaq halts`, `Open the filing (PDF)`) instead of the long raw address. If Telegram ever rejects the HTML, the plain-text copy keeps the address |

Kept on purpose: the CONGRESS chat-list header, the 🏛️ tag that starts every message (no second emoji: the coloured circles stay removed), Singapore times, every message buzzing.

Per message:
- Halt times put Singapore first, New York under it: `Halted     22:41 SGT 17 Nov` / `09:41 New York 17 Nov`.
- Market-wide halts read `Event      MWC1 · level 1` (the title already says market-wide) and `Feed rows  40`.
- Congress and Trump trade lines read `SELL DIS   $1M-$5M · 30 Dec`: the same disclosure bands without the `.00`, so a line fits one row.
- Clusters show `Cluster    3 insiders in 14 days` and `Together   $270K`.
- The heartbeat no longer lists a module under "Still ok" when it is also listed as a problem (it did when a fresh module kept erroring).
- Events digest: one entry per event, day and date, then time and event, as asked; a long event name continues under its own column instead of running off the screen.
- Apps Script (`Status.gs`, `Kick.gs`, to paste): the SCOUT STATUS card and the FILINGS KICK FAILING warning in the same shape. The OPTIONS KICK FAILED warning is left exactly as it was: it sits inside the options scanner's code in `Kick.gs`, which stays byte-identical.

Tested: 291 of 291 Python tests and 70 of 70 Apps Script tests pass. 35 tests that matched the old layout now check the same facts in the new one (none removed or weakened; every "must not appear" check kept); 4 new tests for the card (shape, wrapping, valid Telegram HTML with no emoji in the block, plain-text fallback keeping the link). All 79 messages the suite produces were compared with the live layout: every value present, valid HTML, no emoji inside a block, no block line over 32 characters.

Reverse: revert the pull request; paste back the Status.gs and Kick.gs from `handback/apps-script-paste/`.

## 2026-11-18 — tidier messages (cosmetic)

Wording and layout only. No alert rule, threshold, schedule, state file or CSV changed. The house rules stay: CONGRESS chat-list header, the bank emoji, plain-text code blocks, no emoji inside boxes, plain-text titles, Singapore times, everything buzzes.

| Change | Why |
|---|---|
| Long values in a box wrap onto an indented line under their own column (`BOX_WIDTH = 38` in `monitor.py`), breaking after a comma or before a bracket when that costs no extra line, otherwise between words | Telegram wrapped lines that were too long back to the left edge, which broke the columns and could split a number (filer names, the heartbeat's "still ok" list, circuit-breaker titles, insider roles). Every line of 38 characters or fewer is exactly as before, including the halt time line |
| The events digest keeps one line per event (`wrap=False`) | The layout Kiian asked for: day, date, time, event on one line |
| Congress trade lines read `$1.00M-$5.00M  30 Dec` | Without the inner spaces a trade line fits on one row |
| Titles are consistent capitals: `US EVENTS - NO DATA`, `CONGRESS FILING - OPEN MANUALLY`, `INSIDER SELL NOTICE - FORM 144`, `AMENDED INSIDER BUY` (single space) | Some titles were half lower case, one had a double space |
| Form 144 sale dates read `20 Nov` | They were shown as `11/20/2026` beside every other date's `20 Nov`; `dmy()` now also reads the US form |
| The heartbeat names checks in words: `FORM 4`, `Form 144`, `13D`, `Congress`, `Fear & Greed`, `US events` (`HEALTH_NAMES`) | A stale Form 4 feed showed as a bare `4`, and the healthy list used internal names like `congress-house` |
| Market-wide halts say `ROWS 40 listed under this code` | Shorter, so it fits one line |

Tested: 287 of 287 tests pass, `pyflakes` clean. Every message the test suite produces (79) was captured before and after: no number lost, no box line wider than 38, and the only word changes are the renames above. 7 tests that looked for a long line on one row now check the same text with the wrapping undone (`flat()` in `tests/conftest.py`).

Reverse: revert the pull request, or set `BOX_WIDTH` to a large number to turn the wrapping off.

## 2026-11-18 — second independent review: three small fixes

A second, separate review of the fixes above found nothing high or medium. Three low items, all fixed:

| Change | Why |
|---|---|
| A forced events digest also accepts the coming week | Run by hand with `force` on a Sunday afternoon or evening in Singapore (after the feed rolls to the new week), it found no feed for the week just ending and sent a false "no data" alert. Scheduled runs are Monday only and were never affected |
| The filings job limit is 20 minutes instead of 15 | `monitor.py` stops starting new work at 10 minutes, but a filing already in progress makes several fetches and could still run past 15 on a very slow SEC day. Normal runs take under a minute |
| A market-wide halt row in `data/halts.csv` is labelled `MARKET`, "All listed securities (N rows)"; a halt that started and ended between two runs is announced as "MARKET-WIDE HALT, ALREADY ENDED" | The row carried whichever stock the feed listed first, which would mislead the price-move analysis; and the message said trading was paused when it had already resumed |

Tested: 281 of 281 tests pass, `pyflakes` clean.

## 2026-11-18 — fixes from an independent review of the new code

A separate review of every change made on 18 Nov, after it went live. Five fixes; one finding kept as a deliberate choice.

| Change | Why |
|---|---|
| Every workflow that saves state checks out the branch as it is when the job starts (`ref: ${{ github.ref_name }}`), and a failed `git pull --rebase` is aborted before the next try | A run queued behind another started from the commit that was current when it was queued, so it did not see the other run's saved state and could send its alerts again. Rare for the 5-minute jobs (2 of the last 300 halts runs were queued), but the Monday digests are started by Apps Script and by cron at the same minute, 23:00 UTC, so a second copy of a digest was a weekly risk |
| A market-wide circuit breaker is recorded once per level per day (`MW|MWC1|date`), however many rows the feed lists, and rows without a symbol are kept for those codes | Keyed per row, a halt of the whole market (thousands of rows) would overflow the 4,000-key seen list and alert again on every run, pushing out real halt keys as it went. A symbol-less layout would never have alerted |
| Congress stops looking for a trade's ticker and type code at the start of the next trade's line | It could take the next trade's ticker and `[XX]` code. 4 of 3,768 real trades had the wrong code (school bonds coded `[GS]` read as `[OT]`); all four corrected, nothing else changed |
| `monitor.py` stops starting new work after 10 of its 15 minutes, `congress.py` after 14 of its 20 | A job killed at its limit saves nothing, so every alert it had sent went out again on the next run. Unfinished filings stay queued or unseen for the next run |
| The events digest only accepts a feed that holds its week | When this week's feed failed it used next week's, which on a Monday is the following week: sent as "this week" and recorded as sent, so the backup runs could not correct it. Now it counts as a failed fetch and the backups retry |
| Kept: a Telegram read timeout is still retried | Telegram may have posted the message and answered slowly, or not posted it; the two cannot be told apart. A rare duplicate is the better failure than a lost alert. Now said in a comment in `_send_once` |

Tested: 277 of 277 tests pass, `pyflakes` clean. New tests cover each fix, including a 5,000-row market-wide halt over three runs, the wrong-week fallback, and both time limits. The congress change was replayed over the 411 real filings. The events change was checked against the live feed (its week runs from Sunday 15 Nov).

## 2026-11-18 — halts: retry when Nasdaq's bot filter answers

| Change | Why |
|---|---|
| When the halts feed answers with a bot-check page instead of the feed, the request is tried once more after 8 seconds, keeping the filter's cookies. (First shipped as two quick retries with cookies dropped; tuned the same day after a live run got a harder block on the third try. The next run, 5 minutes later from another machine, remains the real backstop.) | Nasdaq's filter returns a 200 and a small challenge page now and then: 18 of 1,500 runs since late October, one of them minutes after the upgrade went live. The run then ended with "feed not parseable" and the halt waited for the next run, about 5 minutes later. Nothing was ever lost |

Tested: 262 of 262 tests pass, including a challenge page followed by the feed, and a challenge page on every try (still reported as before).

## 2026-11-18 — second pass: congress parser, halts history, workflow hardening

### Alerts that were wrong or missing

| Change | Why |
|---|---|
| Congress keeps every trade in a filing | Rows sharing an action, a date and the first 18 letters of a name were merged, on the belief that one trade could be read twice. Checked against 411 real filings: it never is. The merge only dropped real trades, 263 of 3,768 (the same stock bought in two accounts on one day, or two Liberty tracking stocks), 11 of them large enough to alert |
| Congress ignores the column heading the form repeats on every page | When a page break fell inside a trade, the heading's "$200?" was read as the top of the amount band. 11 logged trades showed "$50,001 - $200" and the like; the same break also hid 36 tickers |
| Congress option trades say so: `BUY GOOGL  $250K - $500K   30 Dec  calls` | The filer's asset code (`[OP]`) was thrown away, so bought call options were shown as plain stock buys. 15 option trades in the 411 filings checked, every one now labelled, with calls or puts from the filer's description |
| Trades the filer codes `[GS]` (government securities) or `[CS]` (corporate bonds) never alert as stocks | They are debt whatever the name says |

### New

| Change | Why |
|---|---|
| `data/halts.csv`: every new halt except ordinary volatility pauses, with whether it alerted. Rolling year. Written only by the halts job | So the planned analysis (price move after each alert type) has halts to work with. `alerts.csv` stays `monitor.py`'s alone |
| `data/congress_trades.csv` gains an `asset_type` column at the end | Option, stock and bond trades can be told apart in the history. Only the header line of the existing file is rewritten, once; every row is kept byte for byte, and older rows read as empty in the new column |

### Workflows

| Change | Why |
|---|---|
| Commit steps take the branch name from `env:` and quote it | Security finding S13: it was pasted into the shell line unquoted |
| `cache: pip` on every Python setup | Saves the dependency download on most of the ~290 runs a day |
| The test sandbox now applies to every test automatically | A new test that forgot to ask for it wrote to the real `data/congress_trades.csv` on my machine during development. Caught at once and restored from git; nothing was committed. Now no test can reach the real files |

### Tested

259 of 259 tests pass, `pyflakes` clean. The congress parser was replayed, old against new, over 411 real House filings downloaded from the Clerk's public site: every trade the old parser found is kept (3,458) or corrected (36 tickers recovered, 11 amounts fixed), none lost or worsened, and 263 previously dropped trades now appear. The header upgrade was run on a copy of the live trade log: every row byte-identical.

### Decided on the owner's behalf

| Decision | Reverse with |
|---|---|
| Halts history skips `LUDP` and `LUDS` | empty `HALTS_CSV_SKIP` in `halts.py` |
| `[GS]` and `[CS]` trades are not treated as stocks | empty `NON_EQUITY_TAGS` in `congress.py` |

## 2026-11-18 — audit and upgrade

Full audit of the six Python modules, the eight workflows and the Apps Script side. The architecture stays as it is: GitHub Actions for the work, the repo for state, Apps Script for the clock and the status card. The changes are fixes inside that design.

### Alerts that were being lost

| Change | Why |
|---|---|
| Telegram sends are retried, and a message that still cannot be delivered is saved to `state/outbox_<module>.json` and sent by the next run with a "Delayed delivery" line | A send was tried once. If Telegram was slow or rate limited the alert was gone, because the filing or halt was already marked seen. Nothing reported it |
| A filing whose documents fail to download goes back on the queue, up to 3 tries | One SEC timeout marked the filing seen for good |
| Discovery reads further pages of EDGAR's feed when a whole page is new, up to 4 | The feed shows only the newest 100 filings per form. At the US close 60 to 70 arrive every five minutes, and the longest gap between runs in 45 days was 81 minutes, so older filings had scrolled away unread |
| A congress PDF that fails to download is retried on later runs, up to 6 | It was reported at once as "open manually" and the trades in it were never read |
| When congress reaches its 15-alert cap it stops and leaves the rest for the next run | It carried on, marking the remaining filings seen with their alerts silently skipped |
| In January the previous year's House index is read too | Filings from the last days of December were missed, and until the new year's file existed every run failed |

### Alerts that were wrong

| Change | Why |
|---|---|
| Halt code `M` no longer alerts. `MWC0` to `MWC3` and `MWCQ` now do, as one message | `M` was labelled "market wide". Nasdaq defines it as a volatility pause in one exchange-listed stock, the same event as the `LUDP` pauses that were excluded as noise. It was 389 of the 601 alertable halts on record, about 7 a day. The real market-wide circuit breaker codes were not watched at all |
| Trump ticker resolution no longer accepts a near prefix | `SNAP INC CLASS A` was reported as SNA (Snap-on) and `IDEXX LABS INC` as IEX (IDEX Corp) |
| Events digest decides "all day" in the feed's own time zone | An event at 11:00 New York in winter is 00:00 Singapore and was printed as "all day" with its time dropped |
| Congress no longer filters out listed partnerships | "L.P." and "Partners" in the name blocked 13 real trades: Brookfield Renewable (BEP), Energy Transfer (ET), Enterprise Products (EPD), Perella Weinberg (PWP) and others |
| Two share classes in one ticker field use the first (`HEI, HEI.A` becomes `HEI`) | The whole string was treated as a ticker, so those buys never clustered and the alert showed both |
| A Form 4 that does not say what is held after the trade shows `HOLDING not disclosed` | It showed "new position" |
| Earnings watchlist: `SQ` replaced by `XYZ` | Block Inc changed ticker in January 2025, so its results were never flagged |
| Halt times read `09:41 ET 17 Nov (22:41 SGT)` | The feed's New York time was shown bare, which reads as Singapore time |

### Noise

| Change | Why |
|---|---|
| Insider clusters need $250,000 of combined buying (`CLUSTER_MIN_TOTAL`) | Clusters had no dollar floor and were the largest source of pings: 188 in 30 days, all HIGH, a quarter of them totalling under $62,000 |
| Monday digests are sent once per week however often the job runs | They are now started by `Kick.gs` at 07:00 Singapore as well as by cron, with two backup cron times |
| A digest source failure is announced once, then retried quietly | Each backup run would have repeated the failure message |

### Problems that went unreported

| Change | Why |
|---|---|
| SEC feed staleness limit 96 hours to 12; congress 96 to 48 | EDGAR's feed answers around the clock, weekends included, so 96 hours meant a dead filings monitor went unreported for four days |
| The heartbeat asks Telegram whether the bot can still see the channel, and its run goes red if not, or if its warning could not be delivered | A revoked token or a removed bot was invisible: every send failed quietly and every run stayed green |
| The heartbeat reports alerts stuck in an outbox for more than an hour | New with the outbox |
| Trump: falling back to the built-in seed list is stamped as a failure | It was stamped as a success, so if the one working index went away, new filings would stop and nothing would say so |
| Repeatedly unreadable SEC filings are reported after three runs in a row | New with the retry queue |

### Security

| Change | Why |
|---|---|
| Credentials are removed from every log line before printing | A network error message contains the request URL, and the Telegram URL contains the bot token. This repo's logs are public. GitHub's own masking was the only protection |
| The Finnhub key is sent in a header, not the URL | Same reason. Not exploitable today, the key is not set |
| `trump.py` downloads a filing only from `oge.gov`; `monitor.py` follows a filing link only to `www.sec.gov` | The Trump filing list comes from a third-party page. Whatever it pointed at would have been downloaded, parsed and reported as a Trump filing |
| Dependencies pinned to exact versions | Every run installed whatever was newest. The pins are the versions the live runs were already using |

### Housekeeping

- State files are written to a temporary file and swapped in, so a killed run cannot leave a truncated file.
- `congress.py` and `trump.py` keep their seen lists in order, so trimming drops the oldest entries and not random ones.
- NYSE holidays are computed from the exchange's rules. The typed list ended in 2027.
- The weekly SEC company list refresh restarts its clock only after a download that worked.
- First-time 13D and 13G filings by non-institutional holders who are not on the watchlist are written to `alerts.csv` at LOW. They are still not pushed. This gives real numbers for deciding whether to widen stake alerts.
- Removed: the 8-K and late-filing handlers (unused since mid September), unused imports and variables. `pyflakes` reports nothing (19 warnings before).
- Diagnostics show real US Eastern time. They assumed UTC-4 all year.
- `heartbeat.yml` and `diagnostics.yml` have timeouts. `events.yml` uses the same five-try state push as the others.
- Added: `tests/` (246 tests), the Tests workflow, `README.md`, `DEPLOYMENT.md`, `TEST_PLAN.md`, `.env.example`, `.gitignore`.

### Tested

`pyflakes` clean. 246 of 246 tests pass on Python 3.11. Every module run end to end with the network blocked. Telegram payloads byte-identical to the previous version for 7 message types. Trump tickers compared old and new over all 835 logged rows: 28 changed, all corrections or newly resolved. Cluster rule replayed over `data/buys.csv`: 191 alerts in 30 days become 106. Details in `TEST_PLAN.md`.

Not tested here: anything needing the live SEC, House, OGE or Telegram. Run **Diagnostics** after deploying.

### Decided on the owner's behalf, and how to reverse each

| Decision | To reverse |
|---|---|
| Cluster floor of $250,000 | `CLUSTER_MIN_TOTAL = 0` in `monitor.py` |
| Halt code `M` stops alerting | add `"M"` to `ALERT_ON` in `halts.py` |
| Stake alerts stay watchlist-only | `STAKES_NOTABLE_ONLY = False` in `monitor.py` |
| No market cap filter (no Finnhub key) | add a `FINNHUB_KEY` secret. Note it would hide insider buys in companies under $300M |
| SEC staleness limit of 12 hours | `FORMS_STALE_HOURS` in `monitor.py` |

### Not changed

`state/`, `data/`, the message layout, the `CONGRESS` header, the watchlists (apart from `SQ`), every alert threshold other than the cluster floor, the workflow file names, `ubuntu-24.04`, Python 3.11, the action versions.

## Earlier

Before this entry the repo had no changelog. Changes from August to 11 November 2026 are recorded by date in the project's technical reference.
