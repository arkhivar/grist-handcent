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
real feature, not a mock.

## 4. Single `Transactions` table with `source` (2026-09-28)
**Context:** three tables (Expenses/Income/T900) mirrored sender structure the user no
longer wanted; Expenses already carried user columns (category, performance…).
**Decision:** union-schema `Transactions` (all parsed fields + `source` Choice);
migrate 919 rows with read-back verification; old tables frozen as archive.
**Consequences:** one query surface; writers simplified to a single `fields_txn` map.
Server lacks a rename endpoint, so Transactions is a new table — the user's main Grist
page had to be pointed at it manually. Mid lookups are not source-scoped (collision
caveat documented in `_GRIST.md`). *Superseded by #9 on 2026-10-02 — Transactions was
merged into RevenueBase.*

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
— the cloud moves the *notification*, not the *responsibility*.

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
renamed to `datetime`. The engine re-pointed via its single module constant. New bank
rows leave `Date` empty, pending a decision on whether the engine should fill it from
parsed doc dates.
**Consequences:** one live payment ledger (only the 19-row `Income` archive remains;
Expenses/T900/Transactions/Transactions2 deleted). Engine row schema is now exactly
`{datetime, notes, mid, source, op_type}` + parsed-only `{amount, counterparty,
balance_after, doc_number, account_from, account_to}`. A table rename is still coming
— re-pointing the engine is a one-line change. Live run 2026-10-02 verified end-to-end
against RevenueBase (all writes "VERIFY OK").

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
phone trigger proves stable (delete it to save minutes). OTP burst storms are
debounced by the `sms-sync` concurrency group (serialized runs; GitHub keeps only the
newest queued) plus a Tasker min-interval. The 5-week Handcent JWT refresh now means
editing a GitHub secret, not a local file. Watch the Actions minute budget: a full
run with quiet periods is ~5–9 min; the 15-min schedule alone costs ~500–900
min/month. One tooling wrinkle: the MCP gateway refuses to write `.github/workflows/`,
so the workflow ships at `workflows/sms-sync.yml` and is moved into place manually
(see `_RUNBOOK.md`).
