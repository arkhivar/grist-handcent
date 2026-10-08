# Decision log

> ADR-style. Each entry: context → decision → consequences. Newest last.

## 1. Write → read-back verify → then delete (2026-08-07)
**Context:** the pipeline deletes the only copy of bank records (SMS) after moving them.
**Decision:** a message may be deleted only after its Grist row is written AND
read-back-verified. Explicitly user-blessed ("100% right logic").
**Consequences:** a Grist outage or parse bug degrades to "messages stay on the
phone", never to data loss. This ordering is the pipeline's load-bearing invariant —
keep it ahead of any convenience optimization.

## 2. Twin-fold byte-identical SMS, newest mid wins, delete both copies (2026-08)
**Context:** the bank resumed double-sending every debit — two identical SMS per
operation.
**Decision:** identical `text` folds to one row under the newest mid; **both** mids
enter the deletion set.
**Consequences:** no duplicate rows; the phone still ends up empty. Folding key is the
full raw text — never normalized ("Ваш код" messages with *different* codes are not
twins).

## 3. OTP split (2026-09-27)
**Context:** OTP volume is huge and useless for accounting, but codes must stay
glanceable at the machine.
**Decision:** 0321 OTPs are deleted **without** a Grist row (never archived). 900 OTPs
get a row (`op_type="otp"`) so they flow into the widget's Latest codes strip.
**Consequences:** purge-by-rule stays; archive stays complete; the codes strip is a
real feature, not a mock. *Reversed in part by #14 (2026-10-09): 0321 codes now get
rows like everything else; the 900 arm stands unchanged.*

## 4. Single `Transactions` table with `source` (2026-09-28)
**Context:** three tables (Expenses/Income/T900) mirrored sender structure the user no
longer wanted; Expenses already carried user columns (category, performance…).
**Decision:** union-schema `Transactions` (all parsed fields + `source` Choice);
migrate 919 rows with read-back verification; old tables frozen as archive.
**Consequences:** one query surface; writers simplified to a single `fields_txn` map.
Server lacks a rename endpoint, so Transactions is a new table — the user's main Grist
page had to be pointed at it manually. Mid lookups are not source-scoped (collision
caveat documented in `_GRIST.md`). *Superseded by #9 on 2026-10-02 — the pre-merge
Transactions table was merged into RevenueBase.*

## 5. Zero inbox + `unparsed` rows (2026-09-29)
**Context:** two messages resisted classification for days; keepers (payroll notices,
promos) accumulated on the phone awaiting decisions.
**Decision:** the phone is a dumb relay; Grist is the sole sorting surface. Every
unclassified message becomes an `op_type="unparsed"` row and is deleted after
verification. The 12 keeper messages migrated to Grist the same way.
**Consequences:** "parse fails" became a normal metric (widget label now reads
"unparsed"). Review = filter `op_type=unparsed` in Grist, categorize or delete there.
Parser gaps no longer stall inbox zeroing.

## 6. Writers never touch user columns
**Context:** the user actively sorts with `category`, `performance`, `notes2`.
**Decision:** the pipeline sets only its own fields; trigger columns (`Created_at`,
`Last_updated_at`) fire on their own.
**Consequences:** manual and automated edits coexist without clobbering. Any future
field addition must keep this rule.

## 7. Secrets posture (ongoing)
**Context:** a JWT that can read/delete all bank SMS and a full-access Grist key.
**Decision:** local files only, never printed/logged/committed; repo private until the
GitHub Actions phase stabilizes; future cloud phase uses a fine-grained PAT scoped to
this repo, SHA-pinned actions, no PR-triggered workflows.
**Consequences:** the 5-week refresh stays a deliberate human step (see `_RUNBOOK.md`)
— the cloud moves the *notification*, not the *responsibility*. *Update 2026-10-09:
the repo stayed PUBLIC when the Actions phase landed (decision #11) — free minutes and
secret-free artifacts compensated; actions are pinned to major-version tags, not SHAs
(see `_SECURITY.md`). The PAT lives in the phone's MacroDroid, scoped to this repo.*

## 8. Drop `direction`, make `op_type` a single select (2026-10-01)
**Context:** `direction` (in/out/info) duplicated information already carried by
`op_type`; the user wanted one curation axis.
**Decision:** user removed the `direction` column and converted `op_type` to Choice;
the engine was updated the same day (writers no longer emit `direction`; `op_type` is
sent as a plain string label) and the offline harness re-verified.
**Consequences:** one axis to rule the review workflow; the unparsed row schema became
exactly 5 base keys. When adding new op_type values, add the choice label in Grist
first (or confirm invalid values are allowed on that column).

## 9. Unified ledger: merge `Transactions` into `RevenueBase` (2026-10-02)
**Context:** the user had been keeping manual payment rows in a separate table while
the engine wrote bank rows to `Transactions`; two ledgers meant two surfaces for one
question ("what was paid, when, by whom"). At the same time, `op_type` had just become
a Choice single select (2026-10-01, decision #8), making `direction` definitively
redundant.
**Decision:** merge `Transactions` into the unified ledger `RevenueBase` — 1,977+ rows
= 1,187 manual payment rows + 790 bank rows, with `SUM(Paid)` unchanged as the
migration checksum — and prune fields in the same pass: `direction` removed (op_type
carries the info), `card` reserved for manual assignment (engine must not write it),
`doc_date` retired (data moved to the payment-side `Date`), `via` merged into
`source`, `notes2` dropped (always empty), `text` merged into `notes`, `date2`
renamed to `datetime`. New bank rows leave `Date` empty, pending a decision on
whether the engine should fill it from parsed doc dates.
**Consequences:** one live payment ledger (only the 19-row `Income` archive remains;
Expenses/T900/the pre-merge Transactions/Transactions2 deleted). Engine row schema is
now exactly `{datetime, notes, mid, source, op_type}` + parsed-only `{amount,
counterparty, balance_after, doc_number, account_from, account_to}`. The ledger was
later renamed `Transactions` (2026-10-06, decision #12). Live run 2026-10-02 verified
end-to-end (all writes "VERIFY OK").

## 10. `Date`/`Paid` retired — `datetime` and `amount` are the single source of truth (2026-10-02)
**Context:** after the merge, the ledger had two date-ish columns (`Date` for payment
rows, `datetime` for bank rows) and two money columns (`Paid` for payments, `amount`
for bank rows) — the exact duplication decisions #8/#9 were trying to eliminate.
**Decision:** the user copied `Date` → `datetime` in Grist (rows without a time got a
default "12:00am") and deleted both `Date` and `Paid`. No engine change was needed —
neither column was ever engine-written; the engine's `amount` write path was already
in place. Schema re-verified from Grist (`PRAGMA`-style probe: 24 fields, no stale
columns) and an empty-inbox live run completed cleanly.
**Consequences:** `datetime` is now the sole timestamp column for every row (epoch
seconds), `amount` the sole money column. The pending "should the engine fill `Date`?"
question from #9 is moot. User-owned columns the engine must not touch are now:
`category`, `performance`, `student`, `sprint_*`, plus manual edits of `notes`.

## 11. Event-driven cloud sync on GitHub Actions (2026-10-08)
**Context:** the manual widget button was the only trigger — the engine could only run
while the Kimi Work host PC was awake. The user wants zero-touch sync (PC asleep is
fine).
**Decision:** the phone itself triggers the run: Tasker/MacroDroid fires on an
SMS-received broadcast and POSTs `repository_dispatch` (type `sms-sync`, optional
cid) to this repo; GitHub Actions runs the same engine (`sms_grist_sync.py` + thin
`runner.py`, wired via `.github/actions/sms-sync-run`). Secrets moved from local
files to repo Secrets (`HANDCENT_AUTH`, `GRIST_API_KEY`); `load_config()` now takes
env vars first, with the file fallback keeping the local Kimi Work automation usable
as a dev harness/fallback.
**Consequences:** the PC can sleep. A 15-min `schedule` acts as a safety net until the
phone trigger proves stable (delete it to save minutes — moot while the repo is
public, still worth removing for hygiene). OTP burst storms are debounced by the
`sms-sync` concurrency group (serialized runs; GitHub keeps only the newest queued)
plus a Tasker min-interval. The 5-week Handcent JWT refresh now means editing a
GitHub secret, not a local file. Watch the Actions minute budget if the repo ever
goes private: a full run with quiet periods is ~5–9 min; the 15-min schedule alone
would cost ~500–900 min/month. One tooling wrinkle: the MCP gateway refuses to write
`.github/workflows/`, so the workflow originally shipped at `workflows/sms-sync.yml`
and was moved into place manually (see `_RUNBOOK.md`). *Update 2026-10-09: the
workflow now lives in the repo at `.github/workflows/sms-sync.yml` — the staged-copy
workaround is retired; the gateway still can't write that path, so workflow edits go
through the GitHub web UI or a local git push.*

## 12. Table rename = tell the engine first (2026-10-09)
**Context:** a few days after the merge (2026-10-06) the user renamed the unified
ledger `RevenueBase` → `Transactions`. On this Grist instance renaming changes the
table **id** itself (there is no rename endpoint; the UI produces a new id), so the
engine's hardcoded `TABLE` constant silently pointed at a nonexistent table and every
write failed. Because of decision #1's hard rule — never delete before a verified
write — no message was lost: ~21 queued messages simply stayed on the phone.
**Decision:** re-point the engine's one-line `TABLE` constant to `Transactions`
(commit `4858bd8`) and drain the backlog; no protocol or schema change was needed.
**Consequences:** the ~21 queued messages (11×0321 + 10×900) were caught up the same
morning, all writes read back "VERIFY OK", then deleted from the phone — decision #1
absorbed a full table rename with zero data loss, exactly as designed. Lesson
recorded: renaming the table must re-point the engine the same day (or be accepted as
a quiet period during which messages queue on the phone). The "rename handshake" gap
noted here — reading the table id from configuration instead of a hardcoded constant —
was implemented the same day (decision #13).

## 13. Table id is env-driven: `GRIST_TABLE` (2026-10-09)
**Context:** decision #12 exposed the fragility of a hardcoded table id: a user-side
rename silently broke every write until the code was patched, and the cloud workflow
would have kept running the stale constant between the rename and the fix.
**Decision:** the engine's `TABLE` is now `os.environ.get("GRIST_TABLE") or
"Transactions"` — a default plus an env override, no code change needed to re-point.
The Actions workflow passes `vars.GRIST_TABLE` (repo Variable) through the
`sms-sync-run` composite step as `GRIST_TABLE`; if the variable is unset, the
`Transactions` default applies. Commit `97ba1af`.
**Consequences:** the next rename is a single Variable edit (Settings → Secrets and
variables → Actions → Variables), effective on the very next run, with no silent
breakage window in the cloud. The local Kimi Work automation keeps working unchanged
via the default (or an optional `GRIST_TABLE` env var). Residual caveat: between the
rename and the Variable edit, messages still queue on the phone — the write→verify→
delete invariant (decision #1) keeps that safe, but editing the variable promptly
remains the user's part of the handshake.

## 14. 0321 codes become verified Grist rows — zero-inbox absolute (2026-10-09)
**Context:** the user is cutting the dashboard dependency: Grist is to be the sole
review surface, and a whole class of messages that bypassed the ledger (0321 OTPs,
deleted without a row per decision #3) could never be reviewed, searched, or audited
there.
**Decision:** 0321 code messages now go through the same write → read-back verify →
delete path as everything else, becoming rows with the same 5 base keys as unparsed
rows and `op_type="otp"` — reversing the 0321 arm of decision #3 (the 900 arm is
unchanged; 900 codes already wrote rows). The OTP-purge path (delete-without-row) is
removed from the engine entirely; twin-fold still folds identical code texts to one
row (newest mid wins, both mids deleted). Commit `1bfa393`.
**Consequences:** decision #1's invariant (nothing is deleted without a verified row)
now covers literally every scanned message — no exempt class remains.
`rowsWritten` counts code rows too (expected, cosmetic); `parseFailures` still counts
only `unparsed` rows. `latestCodes` population is unchanged — the codes strip keeps
working, now fed from ledger rows instead of a side channel.
