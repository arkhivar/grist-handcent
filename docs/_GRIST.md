# Grist state

Doc: `https://seoffice.getgrist.com/tRknrJrfbW3L` (page URLs like `/p/1`, `/p/17`
refer to Grist *pages* — verify which table a page shows via schema, not URL).

## Tables

### `Transactions` — the main store (since 2026-09-28)
Single table for both senders, 919 rows at migration. Union schema:

| Group | Columns |
|---|---|
| Common (written every row) | `date` (DateTime, VLAT), `text`, `mid` (Int), `source` (Choice: `900` / `0321`) |
| 900-side | `op_type` (Choice/single select), `amount`, `counterparty`, `balance_after`, `card` |
| 0321-side | `doc_number`, `doc_date` (Date), `account_from`, `account_to` |
| **User-owned — the pipeline NEVER writes these** | `category` (Choice), `performance` (Ref), `Created_at`, `Last_updated_at` (trigger columns), `notes2` |

Notes:
- `direction` was **removed 2026-10-01** — in/out/info is carried by `op_type`;
  the engine no longer sends it.
- `op_type` is the user's single-select curation axis. Vocabulary: `expense`,
  `income` (0321 parsed) · `otp` (900 archived codes) · `unparsed` (zero-inbox
  catch-all) · legacy values from the migrated T900 rows: `purchase`, `purchase_sbp`,
  `transfer_out`, `transfer_in`, `sbp_in`, `refund`, `payment`, `fee`, `confirm`,
  `promo`, `declined`, `court_collection`.

**Review workflow:** filter `op_type = unparsed` → sort/categorize/delete *in Grist*.
The phone inbox is expected to be empty; if it isn't, something's wrong.

### Archive (kept intact, do not write)
`Expenses` (576), `Income` (19), `T900` (324) — pre-merge tables, row-verified
identical to their Transactions subsets at migration time. Delete manually when
confident. A junk `Transactions2` (empty, API-undeletable) was removed by hand on
2026-09-29.

## Server quirks (probed 2026-09-28 — this Grist instance is *not* stock)

- **No table rename or delete endpoints** (`POST /tables/{id}/rename` → 404). Schema
  changes go through table-create + per-column `PATCH`.
- **SQL endpoint works**: `POST /sql` is the reliable way to count rows
  (`SELECT count(*) FROM Transactions WHERE source='900'`), and the only sane way on
  this network.
- `GET /records?limit=N` **is** honored; `offset` is **ignored** (returns the first
  page). No gt/gte filters — exact-match lists only.
- **POST body ceiling ~25KB**: ~25-row record batches die at ~23s with nothing
  inserted. **Use 10-row batches** (<1s each).
- Large `/records` full-table reads stall (90s timeouts). Dedup/verify via
  `filter={"mid":[...]}` in **chunks of 8**.
- Records-delete endpoint exists and takes a plain array of rowids.

## Known data caveats

- `mid 3100` appears **3×** under 0321 — faithful to the source (the original
  Expenses table had 3 such rows), not a migration bug.
- Mid lookups for dedup are **not source-scoped** (mid is treated as globally unique;
  a cross-sender collision would dedup wrongly — theoretical, documented).
