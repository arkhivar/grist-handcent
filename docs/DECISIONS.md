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
caveat documented in [GRIST](GRIST.md)).

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
**Consequences:** the 5-week refresh stays a deliberate human step (see
[RUNBOOK](RUNBOOK.md)) — the cloud moves the *notification*, not the *responsibility*.
