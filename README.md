# grist-handcent
----

## Current state — start here

**What this is:** an SMS → Grist bridge. Russian bank SMS arrive on an Android
phone (Handcent app, deviceId 2555969); the pure-Python engine
`sms_grist_sync.py` parses them and writes one row per message to a single
Grist ledger table (id `Transactions`, doc `tRknrJrfbW3L`), reads the row back
to verify it, then deletes the SMS from the phone. Zero inbox, no exceptions:
every message gets a row — parsed (`op_type` per transaction type), unparseable
(`op_type="unparsed"`), codes (`op_type="otp"`, both senders, since
2026-10-09 / decision #14). Nothing is deleted without a verified row
(decision #1, the load-bearing invariant).

**Where it runs — one engine, two executors:**
- **PRIMARY: GitHub Actions** — `.github/workflows/sms-sync.yml`. Triggered by
  a MacroDroid macro on the phone (SMS received → `repository_dispatch` type
  `sms-sync`), by `workflow_dispatch` (manual), and by a `*/15` cron safety
  net that is marked for deletion once the phone trigger proves stable.
  Concurrency group `sms-sync`; secrets + `GRIST_TABLE` flow through the
  composite action `.github/actions/sms-sync-run/action.yml`.
- **SECONDARY: Kimi Work Blueprint Automation**
  `automation_112c8e63-0532-432e-9e61-ef8b627db65d` — manual trigger from the
  "SMS → Grist bridge" widget on the Daily Finance canvas. Dev harness /
  fallback; same engine file.

**Secrets:**
- GitHub repo Secrets: `HANDCENT_AUTH` (Handcent JWT), `GRIST_API_KEY`; repo
  Variable `GRIST_TABLE` (optional; engine default `Transactions`).
- The phone's MacroDroid holds a fine-grained PAT (Contents R/W on this repo
  only) that POSTs the dispatch — it can only ask GitHub to run the sync.
- Local copies for the secondary path: `C:\Users\Userman\Documents\kimi\
  workspace\auth-headers.json` + `grist_api.txt` (engine file fallback).
- The Handcent JWT expires ~every 5 weeks: refresh via the browser
  localStorage ritual, then `gh secret set HANDCENT_AUTH < auth-headers.json`.
  Never print or commit secret values. Details: `_RUNBOOK.md`, `_SECURITY.md`.

**Grist schema right now:** one table, id `Transactions`. Engine-written
columns — base `{datetime (epoch s), notes (raw SMS text), mid, source,
op_type}` + parsed-only `{amount, counterparty, balance_after, doc_number,
account_from, account_to}`. The engine never writes user columns (`category`,
`performance`, `student`, `sprint_*`, manual `notes` edits). `op_type` is a
single-select Choice — add new values in Grist before the engine emits them.
Full schema, vocabulary, and server quirks: `docs/_GRIST.md`.

**How to change things safely:**
- Engine change: edit `sms_grist_sync.py`, run the offline harness
  (`offline_harness.py` — deliberately NOT in this repo; it lives in the local
  task workspace `C:\Users\Userman\Documents\kimi\tasks\2026-09-28\
  09-19-38-157dd3e1\`) — it monkeypatches every network boundary and asserts
  the zero-inbox contract (deletion set, twin-folding, row shapes). Then
  deploy the byte-identical copy to the Kimi Work automation assets and push
  here; Actions picks the new engine up automatically.
- Manual run: Actions tab → "SMS → Grist sync" → Run workflow (cid optional),
  or the widget button for the local path.
- Re-point the table after a Grist rename: edit the `GRIST_TABLE` repo
  Variable — no code change (decision #13). On this Grist instance renaming
  changes the table **id** itself; until the Variable is edited, messages
  queue safely on the phone (decision #1).
- **NEVER:** delete before a verified row; write user columns; commit
  secrets; run full-table Grist reads (chunked mid-filter only); trust
  `GET /msg/text/<mid>` — it returns 200 for deleted messages.

**Known sharp edges:** VPN drops cause DNS failures (check the VPN first,
then re-run); Grist API quirks (25KB POST ceiling, `offset` ignored, chunked
mid-filter reads of 8); the 5-week JWT expiry (401 = refresh ritual);
identical-code twin bursts are debounced by the Actions concurrency group.
Deep docs: `docs/_PIPELINE.md` (how it works), `docs/_RUNBOOK.md`
(operations, token ritual, incident log), `docs/_DECISIONS.md` (why it is
this way, #1–#14), `docs/_GRIST.md` (data), `_EVOLUTION.md` (history),
`_SECURITY.md` (threat model), `docs/_ROADMAP.md` (historical assessment).

## GitHub Actions compute

The primary executor is GitHub Actions, and the repo is **public**, so
standard `ubuntu-latest` minutes are free — the original consumption math in
`docs/_ROADMAP.md` only matters if the repo ever goes private. What still
matters: the `*/15` cron safety net spins up ~48 short runs/day (mostly two
scans and "nothing to delete"); a run with real deletions costs 5–9 min of
runner wall time because of the built-in ~4-min server quiet period. No
VLAT-night gap was ever implemented — the plan was to retire the cron block
once the phone-side dispatch proves stable (see Roadmap below and the comment
in the workflow itself).

## Roadmap
Pending ideas and improvements, in no particular order:

- **MacroDroid self-diagnostics**: save the HTTP return code of the dispatch
  call into an integer variable (`http_code`) and (optionally) fire a
  notification when it isn't `204` — 401 = token problem, 404 = URL typo.
  Turns the phone macro into its own monitoring tool.
- **Retire the 15-min schedule**: once the phone-side trigger proves stable,
  delete the `schedule` block from the workflow to save Actions minutes
  (moot while the repo is public; still worth removing for hygiene).
- **SHA-pin third-party actions**: `actions/checkout@v4`,
  `actions/setup-python@v5`, and `actions/upload-artifact@v4` are pinned to
  major-version tags today; pin them to full commit SHAs (`_SECURITY.md`).
- **Scrub `WS_IDENTITY`**: derive the Handcent account identity from
  `HANDCENT_AUTH` at runtime instead of hardcoding the username in the
  engine (mild disclosure in a public repo — not a credential).
- **Reconcile the `f900-unsent.json` backlog**: ~10 legacy mids were never
  written to any Grist table; decide whether to migrate or drop them, then
  delete the file.

Done recently:

- **0321 codes join the ledger** (2026-10-09): code SMS from both senders
  are written as `op_type="otp"` rows (same 5 base keys as unparsed rows) and
  deleted only after read-back verification — the last delete-without-row
  exemption is gone. See decision #14 in `docs/_DECISIONS.md`.
- **Rename handshake** (2026-10-09): the engine reads its target table id from
  the `GRIST_TABLE` env var (repo Variable in Actions, default `Transactions`)
  instead of a hardcoded constant — future renames are a variable edit, not a
  code change. See decision #13.
