# Grist state

Doc: `https://seoffice.getgrist.com/tRknrJrfbW3L` (page URLs like `/p/1`, `/p/17`
refer to Grist *pages* — verify which table a page shows via schema, not URL).

## Tables

### `Transactions` — the single unified ledger (merged 2026-10-02, renamed from `RevenueBase` 2026-10-06)
The pre-merge sender-mirror tables were folded into one unified ledger, created
2026-10-02 under the table id `RevenueBase` (1,977+ rows = 1,187 manual payment rows
+ 790 bank rows). On 2026-10-06 the user renamed that table to `Transactions` — on
this Grist instance there is no rename endpoint, so renaming **changes the table id
itself**, and the engine's hardcoded id silently broke all writes until re-pointed
(fixed 2026-10-09; see decision #12 in `_DECISIONS.md`). `Transactions` is now the
one live payment ledger and the sync engine's only write target. The engine reads
the table id from the `GRIST_TABLE` env var and defaults to `Transactions` — future
renames are a Variable edit, no code change (decision #13).

**2026-10-14 rollout complete:** Actions engine `d98cd6d` uses signed amounts
and omits persisted `op_type` (decision #15). Migration verified 810 bank rows:
770 debit amounts became negative and two confirmation amounts became blank.
The curated magnitude difference was preserved; manual payment rows and
user-managed fields were untouched. `op_type` was removed after verification,
then Actions was re-enabled. The pre-migration schema read found that column
was Text, despite decision #8's historical Choice description, and found no
formula referencing it. `amount` remains Numeric. See `_RUNBOOK.md` for the
deployment and migration record.

| Group | Columns |
|---|---|
| Engine base (written every row) | `datetime` (epoch seconds; the single source of truth for when — filled from SMS timestamp for bank rows, copied from the old `Date` column for payment rows), `notes` (holds the raw SMS text — the old `text` column was merged into `notes`), `mid` (Int), `source` (Choice, single select) |
| Engine parsed-only | `amount` (Numeric; signed cash movement for bank rows, blank for informational messages), `counterparty`, `balance_after`, `doc_number`, `account_from`, `account_to` |
| **User-owned — the pipeline NEVER writes these** | `category` (Choice), `performance` (Ref), `student`, `sprint_*`… — plus manual edits of `notes` |

Notes:
- `source` vocabulary: `900`, `0321` (bank senders) · `AVq`, `DSc`, `SBb`, `ALb`
  (manual payment rows).
- The legacy `op_type` vocabulary and current internal parser labels are `expense`,
  `income` (0321 parsed) · `otp` (archived codes — both senders since 2026-10-09,
  decision #14) · `unparsed` (zero-inbox catch-all) · legacy values from the
  migrated T900 rows: `purchase`, `purchase_sbp`, `transfer_out`, `transfer_in`,
  `sbp_in`, `refund`, `payment`, `fee`, `confirm`, `promo`, `declined`,
  `court_collection`.
- The writer gives unparsed rows and code rows the four base keys
  `{datetime, notes, mid, source}` with no amount. Their internal labels remain
  `unparsed` and `otp`. The old writer also included `op_type`. 0321 codes became rows on
  2026-10-09 (decision #14) — before that they were deleted without a row.
- Columns removed in the 2026-10-01/02 restructure — do not reference them anywhere:
  `direction` (the writer conveys cash direction through amount sign), `card` (user assigns it
  manually; the engine must NOT write it), `doc_date`, `Date` (both retired —
  `datetime` is the single timestamp column), `Paid` (values moved to the unified
  `amount`), `via` (merged into `source`), `notes2` (was always empty),
  `text` (merged into `notes`), `date2` (renamed to `datetime`).

Outgoing amounts are negative and incoming amounts are positive:

| Internal operation | Grist `amount` |
|---|---|
| `expense`, `purchase`, `purchase_sbp`, `transfer_out`, `payment`, `fee`, `court_collection` | `-abs(parsed amount)` |
| `income`, `transfer_in`, `sbp_in`, `refund` | `abs(parsed amount)` |
| `confirm`, `declined`, `otp`, `promo`, `unparsed` | Blank |

Sign normalization is idempotent and does not alter `balance_after`.
Confirmation requests and declines record no completed cash movement; their
requested amounts remain in the raw SMS notes. Blank is distinct from a
numeric zero. Existing manual payment rows keep their amounts and all other
fields.

**Review workflow:** review blank-amount bank rows in Grist using `source` and
`notes`; this group includes codes, informational messages, and unparsed SMS.
The old `op_type = unparsed` filter was retired with the column.
Phone messages with missing or mismatched verified rows stay queued safely.

### Archive (kept intact, do not write)
`Income` (19 rows) is the only remaining archive table. The old `Expenses`, `T900`,
pre-merge `Transactions` and `Transactions2` tables no longer exist — the user
deleted them.

## Server quirks (probed 2026-09-28 — this Grist instance is *not* stock)

- **No table rename or delete endpoints** (`POST /tables/{id}/rename` → 404). Schema
  changes go through table-create + per-column `PATCH`. (The 2026-10-06 "rename" was
  done in the Grist UI and produced a **new table id**; nothing was renamed in place.)
- **SQL endpoint works**: `POST /sql` is the reliable way to count rows
  (`SELECT count(*) FROM Transactions WHERE source='900'`), and the only sane way on
  this network.
- `GET /records?limit=N` **is** honored; `offset` is **ignored** (returns the first
  page). No gt/gte filters — exact-match lists only.
- **POST body ceiling ~25KB** (observed 2026-09-28 with the old migration writer):
  oversized batches died at ~23s with nothing inserted. The current engine's
  `WRITE_BATCH` is 25 rows and has been live-proven on this instance since
  2026-10 (10-row batches were the original conservative workaround). Don't raise
  the batch size without re-probing, and verify counts after every write batch.
- Large `/records` full-table reads stall — use **60–90s timeouts**. Dedup/verify via
  `filter={"mid":[...]}` in **chunks of 8**.
- Records-delete endpoint exists and takes a plain array of rowids.

## Known data caveats

- The 2026-10-14 read found 594 legacy 0321 rows with blank `op_type`: 575
  debit-prefix rows and 19 credit-prefix rows. A bounded amount migration
  must recognize these prefixes as well as known legacy operation labels;
  unknown or ambiguous rows require review and must not be guessed.
- `mid 3100` appears **3×** under 0321 — faithful to the source (the original
  Expenses table had 3 such rows), not a migration bug.
- Mid lookups for dedup are **not source-scoped** (mid is treated as globally unique;
  a cross-sender collision would dedup wrongly — theoretical, documented).
- DNS failures observed historically were the user's VPN dropping, **not** a code
  bug (see `_RUNBOOK.md`).
