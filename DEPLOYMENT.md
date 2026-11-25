# Deployment

How a change gets from a file to the live bot, how to check it worked, and how to undo it.

## Where things run

| Part | Lives in | Goes live when |
|---|---|---|
| Python modules, workflows | this repo, branch `main` | the change lands on `main`. The next run uses it (within 5 minutes for `monitor.py` and `halts.py`) |
| `Kick.gs`, `Status.gs`, `Core.gs`, `Config.gs`, `Setup.gs` | Google Apps Script project "Filings monitor" | saved in the editor. `Status.gs` also needs a redeploy, see below |
| Secrets | GitHub repo secrets, Apps Script Script Properties | saved |

There is no build step and no server. Deploying is "put the file on `main`".

## Before deploying anything

1. `python -m pyflakes *.py tests/*.py` prints nothing.
2. `python -m pytest` passes.
3. Any threshold or filter change has been replayed over `data/*.csv` and the old and new alert counts compared.

The **Tests** workflow does 1 and 2 on every pull request and on every code change to `main`.

## Deploying Python and workflow changes

**Route A, pull request (preferred).** Open the pull request, check that the Tests run is green, press **Merge pull request**. A merge commit is fine. The monitors keep committing `state/` while this happens; that is expected and does not conflict, because code changes never touch `state/` or `data/`.

**Route B, upload.** On GitHub open the repo, **Add file > Upload files**, drag the changed files in (keeping folders: `tests/` and `.github/workflows/` must land in the same places), commit to `main`.

Never upload or edit anything under `state/` or `data/`.

## Checking a Python deploy

1. **Actions > Tests**: the run for the merge is green.
2. **Actions > Filings Monitor**: the next run is green and ends with `done`. A new commit `state: ...` appears.
3. **Actions > Diagnostics > Run workflow**. Read each step:
   - EDGAR feed probe: a `paging` line reading `OK` (page 2 differs from page 1). `NOT HONOURED` is harmless, it only means discovery cannot reach past the first 100 filings.
   - Trading halts: `OK. N items in feed` and a list of reason codes.
   - Congress, Trump, events, earnings: each prints `OK`.
4. **Actions > Filings Heartbeat > Run workflow**: green and the log says `checks healthy, staying quiet`. A red heartbeat means Telegram refused the bot. Read the log line `TELEGRAM CHECK FAILED`.
5. Watch SCOUT for the next real alert. It must start with `CONGRESS` in the chat list and the bank emoji inside.

## Deploying Apps Script changes

The Apps Script project is shared with the options scanner (`Kick.gs` starts its jobs too) and with SNKRS, TCG and CSP (they report to `Status.gs`). Rules:

1. Paste whole files. Select all in the editor, paste, save. Copy the file from TextEdit (or, from a terminal, `LANG=en_US.UTF-8 pbcopy < file`): a copy that is not UTF-8 turns characters like `·` into `¬∑`. Never type code into the editor: it inserts its own characters. If someone is helping, have them check the saved file against the source afterwards.
2. **Never run anything named `setup` or `deleteTriggers`.** The current `Setup.gs` no longer has them. `checkTriggers` is the safe way to look.
3. After changing `Status.gs`: **Deploy > Manage deployments > pencil > Version: New version > Deploy**. Never **New deployment**: it changes the web address, and SNKRS, TCG and CSP keep reporting to the old one and all go stale with no error.
   Any new `Status.gs` must keep the JSON mode (see "Status.gs JSON mode" below). Without it, each Builds HQ call gets the plain link's behaviour and posts a SCOUT STATUS card, up to one every 5 minutes per Builds HQ server, and its four SCOUT dots show "Can't check". If the JSON mode has to go, ask the owner to set `SCOUT_STATUS_JSON` to `0` on Builds HQ first.
4. Deploy the Python side first when both change. `Kick.gs` starts the Monday digests, and only the current `earnings.py` and `events.py` refuse to send a digest twice.

## Checking an Apps Script deploy

1. Run `checkTriggers`. The log must say `kickFilings` and `scoutStatusDaily` are both present, once each.
2. Run `testKick`. Both lines must say `204`.
3. Run `testKickExtras`. `congress.yml -> 204`.
4. Run `testKickCsp`. Both lines `204`. (Outside their windows the options jobs exit without sending.)
5. Open the STATUS CHECK link: the message in SCOUT, no longer pinned since 23 Nov 2026, so search the channel for it. A card arrives with `WARNINGS` in the chat list and six rows: HALTS, SNKRS, TCG, FILINGS, CSP, KICK.
6. Run `testStatusJson`. It prints the same six rows as JSON and posts nothing. This is the mode Builds HQ reads.

## Status.gs JSON mode (Builds HQ)

Since 22 Nov 2026. The status link with `&format=json` added answers the six rows as JSON: `name`, `stale`, `error`, `age_ms`, `limit_hours` and `detail` (the advice, only when stale). It posts nothing to SCOUT. The key check comes first, as for the card. The plain link still posts the card, and the daily card and stamp mode are unchanged.

Kiian's private Builds HQ site calls only this mode: when one of its pages is open, or when his iPhone widget refreshes, at most once a minute per Builds HQ server. After a failed call it waits 5 minutes (20 seconds when a newly started server got no answer at all). It shows a green or red dot for SCOUT SNKRS (SNKRS row), SCOUT TCG (TCG), SCOUT OPTIONS (CSP) and SCOUT CONGRESS (HALTS, FILINGS and KICK, red if any of the three is). The link lives only in Builds HQ's `SCOUT_STATUS_URL` setting on Vercel, never in this repo. Rollback copy of the 18 Nov file: Drive, CONGRESS folder.

## Rolling back

**Python.** On GitHub open the commit that introduced the change, **Revert** (for a pull request: the **Revert** button on the merged pull request), merge the revert. Or upload the previous version of the files. State files written by the newer code are safe for older code to read: every new field is ignored by old code, and new files (`state/outbox_*.json`, `state/congress_retry.json`) are simply not looked at.

**Apps Script.** Paste the previous file back and save. For `Status.gs`, redeploy as a new version again. Old versions are also under **Deploy > Manage deployments**, in the version list.

**One thing that does not roll back by itself:** if the Telegram token was moved into Script Properties and `Config.gs` was replaced, an old `Core.gs` or `Status.gs` will look for the token in `Config.gs` and not find it. Either keep the new `Core.gs` and `Status.gs`, or put the token line back in `Config.gs`.

## Secrets

| Secret | Where | To rotate |
|---|---|---|
| `TELEGRAM_TOKEN` | GitHub secret, and Apps Script Script Property `TELEGRAM_TOKEN` | @BotFather > revoke and reissue, then update both places. The heartbeat run goes red until both are updated |
| `TELEGRAM_CHAT_ID` | GitHub secret, and `Config.gs` | change both |
| `SEC_USER_AGENT` | GitHub secret | change it |
| `GH_DISPATCH_TOKEN` | Apps Script Script Property | make a new fine-grained token with Actions read and write on `filings-monitor` and `csp-scanner`, replace the property, run `testKick` and `testKickCsp` |
| `STATUS_KEY` | Apps Script Script Property, the status link (the SCOUT message and Builds HQ's `SCOUT_STATUS_URL`), and the SNKRS, TCG and CSP configs | change all of them together, or those bots go stale and Builds HQ's SCOUT dots show "Can't check". If the link is lost, run `showCspSecrets`: it logs the key and the web app address |

## Recovery

| What happened | What to do |
|---|---|
| A state file is corrupt | `seen.json`, `queue.json` and `health.json` repair themselves on the next monitor run. For any other file under `state/`, delete it on GitHub; its module recreates it |
| A module re-alerts old items after its seen file was lost | expected once. Congress and Trump absorb anything older than 10 and 45 days silently |
| Alerts stopped and nothing warned | Actions tab: are runs green? Then the heartbeat log. A red heartbeat names the Telegram problem |
| `state/outbox_*.json` keeps growing | Telegram is refusing the bot. The heartbeat reports it after an hour. Fix the token or put the bot back in the channel; the outbox empties itself on the next run |
| The whole repo is lost | the code is in any clone or downloaded ZIP. State rebuilds itself. The CSV history under `data/` is only in git, so keep an occasional ZIP of the repo |
