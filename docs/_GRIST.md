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

| Group | Columns |
|---|---|
| Engine base (written every row) | `datetime` (epoch seconds; the single source of truth for when — filled from SMS timestamp for bank rows, copied from the old `Date` column for payment rows), `notes` (holds the raw SMS text — the old `text` column was merged into `notes`), `mid` (Int), `source` (Choice, single select), `op_type` (Choice, single select, since 2026-10-01) |
| Engine parsed-only (bank operations) | `amount` (unified money column — payment rows' old `Paid` values were moved here), `counterparty`, `balance_after`, `doc_number`, `account_from`, `account_to` |
| **User-owned — the pipeline NEVER writes these** | `category` (Choice), `performance` (Ref), `student`, `sprint_*`… — plus manual edits of `notes` |

Notes:
- `source` vocabulary: `900`, `0321` (bank senders) · `AVq`, `DSc`, `SBb`, `ALb`
  (manual payment rows).
- `op_type` is the user's single-select curation axis. Vocabulary: `expense`,
  `income` (0321 parsed) · `otp` (900 archived codes) · `unparsed` (zero-inbox
  catch-all) · legacy values from the migrated T900 rows: `purchase`, `purchase_sbp`,
  `transfer_out`, `transfer_in`, `sbp_in`, `refund`, `payment`, `fee`, `confirm`,
  `promo`, `declined`, `court_collection`.
- Unparsed rows carry exactly the 5 base keys with `op_type="unparsed"`.
- Columns removed in the 2026-10-01/02 restructure — do not reference them anywhere:
  `direction` (in/out/info is carried by `op_type`), `card` (user assigns it
  manually; the engine must NOT write it), `doc_date`, `Date` (both retired —
  `datetime` is the single timestamp column), `Paid` (values moved to the unified
  `amount`), `via` (merged into `source`), `notes2` (was always empty),
  `text` (merged into `notes`), `date2` (renamed to `datetime`).

**Review workflow:** filter `op_type = unparsed` → sort/categorize/delete *in Grist*.
The phone inbox is expected to be empty; if it isn't, something's wrong.

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
- **POST body ceiling ~25KB**: ~25-row record batches die at ~23s with nothing
  inserted. **Use 10-row batches** (<1s each).
- Large `/records` full-table reads stall — use **60–90s timeouts**. Dedup/verify via
  `filter={"mid":[...]}` in **chunks of 8**.
- Records-delete endpoint exists and takes a plain array of rowids.

## Known data caveats

- `mid 3100` appears **3×** under 0321 — faithful to the source (the original
  Expenses table had 3 such rows), not a migration bug.
- Mid lookups for dedup are **not source-scoped** (mid is treated as globally unique;
  a cross-sender collision would dedup wrongly — theoretical, documented).
- DNS failures observed historically were the user's VPN dropping, **not** a code
  bug (see `_RUNBOOK.md`).
