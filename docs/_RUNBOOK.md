# Runbook

Operations for the SMS → Grist pipeline. Complements
`_PIPELINE.md` (how it works) and `_GRIST.md` (where data lives).

## Local verification

Use the existing `.venv` (Python 3.12.14 on this host) and the repository
requirements. If rebuilding it, create `.venv` with an available Python 3.12
interpreter; the `py` launcher is unavailable on this host.

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

`tests/test_signed_amounts.py` uses synthetic fixtures and mocked service
boundaries. Report only the results of checks actually run. The old external
`offline_harness.py` is no longer available; historical references below describe
past validation, not a runnable current test suite. Importing or compiling the
engine does not contact services. Calling `run()` or `runner.py` starts live
synchronization and requires an explicit user request.

## Signed-amount rollout (prepared 2026-10-14; pending)

The local engine now uses negative outgoing amounts, positive incoming amounts,
and blank amounts for confirmation requests, declines, codes, promos, and
unparsed messages. It keeps `op_type` only as an internal parser label. Existing
Grist data and schema have not been changed, and deployment is pending.

Validation on 2026-10-14: 49 synthetic offline tests passed. A separate live API
check wrote and read back seven synthetic rows in a temporary table without an
`op_type` column; positive, negative, and blank amounts matched. The temporary
table was removed, and no phone synchronization was run. The read-only plan
covered 810 bank rows: 770 debit sign changes and two confirmation amounts to
clear, with no unresolved rows. One inferred expense has a stored magnitude
different from its SMS; the plan preserves the stored magnitude and flags the
difference. Counts must be refreshed if data changes before rollout.

The configured document id `tRknrJrfbW3L` is the same development document
targeted by active scheduled SMS automation. Its observed name is
`sandbox(copy)` and the supplied access is editor-level. The user confirmed
that SeaTable is production and this Grist document is development; scheduled
Grist writers still need coordinated rollout. A schema read on 2026-10-14 found
`amount` Numeric, `op_type` Text, and no formula referencing `op_type`.

1. Run offline tests and deploy the compatible writer to every active executor
   first. Actions uses the deployed repository copy; the secondary automation
   needs the same asset update. Account for active and queued old executions.
   The 15-minute fallback and phone dispatch can otherwise keep running old code.
   Do not delete the column while an old writer still needs it.
2. Plan the existing-row migration with `tools/migrate_signed_amounts.py`, whose
   default mode is read-only. Read only bounded, filtered bank records for
   `source` `0321` or `900` with message identifiers, using SQL keyset pages or
   small exact-id reads; this server ignores record offsets. Classify known
   legacy operation labels and the debit/credit prefixes of old 0321 notes.
   The 2026-10-14 read found 594 blank-label 0321 rows: 575 debit and 19 credit.
   Review unknown or ambiguous rows rather than guessing their direction.
3. Apply only the planned `amount` changes to bank rows, in bounded batches by
   Grist row id. Recheck current values before each write and read each batch
   back afterwards. Duplicate mids exist, so mid alone is not a migration row
   identity. Preserve notes, manual payment rows, and every user-managed field.
   Blank informational amounts must remain distinct from zero.
4. Verify migration coverage and remaining exceptions before removing `op_type`
   last. Review views or filters that still use it; the old unparsed filter no
   longer works after removal. Bank rows with blank amounts can be reviewed using
   `source` and `notes`. Record the deployed revision and migration result when
   rollout actually happens.

The migration tool reads its key only from `GRIST_API_KEY`. Supply it through
the local environment without printing it or placing it in a command, document,
or committed file. To create a read-only plan:

```powershell
.venv\Scripts\python.exe tools/migrate_signed_amounts.py --doc-id tRknrJrfbW3L --plan .local/signed-amounts-plan.json
```

The ignored `.local` plan contains before/after amounts and notes hashes, without
raw SMS text or credentials. A new plan refuses to overwrite an existing filename.
Use a fresh filename and rebuild the plan if bank rows or classification inputs
change. After deploying the new writer and allowing old executions to finish,
apply and verify the reviewed plan:

```powershell
.venv\Scripts\python.exe tools/migrate_signed_amounts.py --doc-id tRknrJrfbW3L --plan .local/signed-amounts-plan.json --apply --writers-updated
```

`--writers-updated` records the prerequisite; it does not deploy the engine or
check Actions for old runs. The tool never drops `op_type`. Remove the column
separately only after successful migration verification and dependency review.

A row with the old wrong amount sign will fail the prepared sync's verification
and remain on the phone until the separate migration fixes it. Do not relax the
verification invariant to drain the inbox. Planning and migration do not authorize
a live synchronization run.

## Token refresh (the ~5-week ritual)

**Symptoms:** REST 401; widget red banner "token refresh needed"; artifact field
`tokenExpired: true`.

**Why it can't be automated:** the fresh JWT is minted inside the Handcent web app's
browser session; grabbing it needs the local WebBridge daemon (127.0.0.1:10086).

**Procedure:**
1. Handcent web app open in Chrome (visible tab).
2. Via WebBridge, read localStorage key `flutter.pref_login_info` — the value is
   **double-JSON-encoded**; the inner object has `username`, `token`, `res`, `email`.
   A fresh JWT's payload carries a **new `res`** — patch BOTH `res` and `token`.
3. Write both into `auth-headers.json` (every script reads that file; some old scripts
   have stale hardcoded headers — don't copy from them).
4. Push the refreshed headers to GitHub — the cloud path reads them from repo
   Secrets: `gh secret set HANDCENT_AUTH < auth-headers.json` (run from the repo
   root). The local Kimi Work path keeps using the file as-is.
5. WebBridge daemon down (connection refused on :10086)? Restart Kimi Work.

## GitHub Actions phase

The engine's PRIMARY executor is GitHub Actions (`arkhivar/grist-handcent`),
triggered by the phone — no awake PC required. The live workflow is in the repo at
`.github/workflows/sms-sync.yml`; the automation tooling that maintains this repo
(MCP gateway) refuses to write under `.github/workflows/`, so workflow changes are
made in the GitHub web UI (or via a local git push), not through the gateway. The old
staged-copy workaround (`workflows/sms-sync.yml` re-copied by hand) is retired — the
staged file no longer exists. The run+upload step the workflow calls lives in
`.github/actions/sms-sync-run/action.yml` and deploys normally.

- **Repo Secrets** (Settings → Secrets and variables → Actions):
  - `HANDCENT_AUTH` — full contents of `auth-headers.json` (one line, valid JSON).
  - `GRIST_API_KEY` — the Grist API key (contents of `grist_api.txt`).
- **Target table id**: the engine writes to the `Transactions` table by default. To
  point it at a different table id **without any code change**, create a repo
  **Variable** (same Settings page → **Variables** tab): name `GRIST_TABLE`, value =
  the new table id. The workflow passes it as `GRIST_TABLE` on the next run; no
  variable → the engine default `Transactions` applies. (Decision #13.)
- **JWT refresh (~every 5 weeks):** same browser-localStorage ritual as above,
  then update the secret instead of the file:
  `gh secret set HANDCENT_AUTH < auth-headers.json` (run from the repo root).
- **Manual trigger:** Actions tab → "SMS → Grist sync" → Run workflow → pick
  cid (276 / 277 / 0321 / 900 / all, default all).
- **Phone trigger:** Tasker/MacroDroid fires on the SMS-received broadcast and
  POSTs `repository_dispatch` (type `sms-sync`, optional `client_payload.cid`).
- **Logs/artifacts:** the run page shows the live log; the returned artifact JSON
  (per-sender counters, latest codes, log tail — never credentials) is attached
  as the `sms-sync-artifact` download.
- **Safety net:** a 15-min `schedule` run exists while the phone-side trigger is
  being proven; on a public repo the minutes are free, but delete the `schedule`
  block once dispatch is stable (hygiene).

## Widget states — how to read them

- **Error chip on one sender** with run-level "succeeded": per-sender crash isolation
  did its job. Open the run log (or the artifact's `log`) for the actual error.
- **`unparsed` metric > 0**: normal under zero-inbox — that many rows were written for
  you to review in Grist.
- **`pending` > 0 after a run**: messages that arrived *during* the run window (the
  bank sends constantly). Next sync sweeps them.
- **Red token banner**: see above.

## Incident log

### 2026-09-29 — transient DNS failure (root cause found 2026-10-01: the VPN)
Two button presses (~09:39, 09:42 VLAT) failed with
`URLError: [Errno 11001] getaddrinfo failed` — the machine couldn't resolve
`aw.handcent.com`. The script retried once per run, reported `ok:false` per sender,
and deleted nothing. **Root cause (confirmed by repeated testing): the user's VPN** —
when it drops or reconnects, DNS resolution fails briefly. Not a pipeline defect.
**If `getaddrinfo` fails: check the VPN first**, then just re-run.

### 2026-09-29 — `dict(msgs)` bug (fixed)
First zero-inbox run crashed on 0321 with
`ValueError: dictionary update sequence element #0 has length 5; 2 is required` —
`dict()` was called on a list of 5-key dicts. Fired only when the conversation was
non-empty (900 happened to be empty). Fixed; the then-available offline harness
(`offline_harness.py`, task workspace) replayed a synthetic 0321 dataset against the
real `run_sender` to verify the deletion set, twin-folding, and row shapes. That
external harness is now unavailable; use the repository tests for current changes.

### 2026-09-28 — stalled POST triplicates (cleaned)
During migration, one retried batch insert landed 3× (the server accepts the write
*and* times out the connection). Detected by count mismatch, cleaned via the
records-delete endpoint. **Lesson:** on this server, verify counts after every write
batch, not just at the end.

## Environment notes

- Local and Actions validation use Python 3.12 and `requirements.txt`
  (`websockets>=12,<14`); install into the virtual environment before testing.
- All display times are **Asia/Vladivostok (UTC+10)**.
- **Windows Defender aggressively quarantines fresh unsigned binaries** on this
  machine (once ate `kimi-slides.exe`, cloud-heuristic false positive). If a tool or
  daemon "disappears", check Protection history before debugging code. Recommended:
  a permanent exclusion for the tools directory.
- Secrets (`auth-headers.json`, `grist_api.txt`) live as local files next to the
  deployed engine; they are never printed, logged, or committed. In the cloud phase
  they additionally live as GitHub Secrets (`HANDCENT_AUTH`, `GRIST_API_KEY`).
