# Pipeline technical reference

> The engine behind the "SMS → Grist bridge". Full API history (including dead ends)
> lives in the `handcent-sms-dedup` skill; this file is the working summary.
> The transport behavior below was established in Sep–Oct 2026. The signed-amount
> change was deployed to Actions on 2026-10-14 at `d98cd6d`; bank amounts were
> migrated and the legacy Grist `op_type` column was removed. Decision #15 and
> `_RUNBOOK.md` record the rollout and the unavailable Kimi fallback.

## Run phases (per sender, then combined artifact)

1. **Scan** — paged REST read of the conversation (30/page, newest first,
   `before_timestamp=min(ts)-1`, 0.4s pacing). The *full* conversation is always
   scanned: a mid already in Grist can still be on the phone, and it's a deletion
   target — early-stop on "all known" was removed as a correctness fix.
2. **Classify** — parsers (see below); anything unmatched becomes an **unparsed** row.
   The parsers normalize cash amounts by direction: negative outgoing,
   positive incoming, blank for informational messages. `op_type` is an internal
   label only and is omitted from the Grist payload.
3. **Twin-fold** — byte-identical SMS texts fold to ONE row (newest mid wins); **all**
   twin mids become deletion targets (the bank double-sends every debit).
4. **Dedup vs Grist** — `GET /records?filter={"mid":[...]}` in chunks of 8 (0.3–0.4s
   between chunks). Never full-table reads.
5. **Write** — `POST /tables/<table id>/records`, batches of 25 rows (`WRITE_BATCH`).
   The table id comes from the `GRIST_TABLE` env var and defaults to `Transactions`
   (it was hardcoded `RevenueBase` until the 2026-10-06 rename broke it and the
   2026-10-09 env-driven rework — decisions #12/#13 in `_DECISIONS.md`). On this
   Grist instance renaming changes the id itself; there is no rename endpoint. The
   older ~25KB POST-body ceiling that motivated smaller batches is documented in
   `_GRIST.md` — don't raise the batch size without re-probing.
6. **Read-back verify** — re-fetch by mids (chunked), assert presence and compare
   the expected signed amount within 0.005. Numeric values must be finite;
   missing values, strings, booleans, NaN, infinity, and opposite signs cannot
   substitute for a parsed amount. Expected blank requires actual blank and is
   distinct from numeric zero. Existing rows are not silently rewritten on a
   mismatch. **A message may only be deleted after its row is verified — no
   exceptions, no exempt message classes.**
7. **Delete** — websocket pipe (below), 0.8s frame spacing, fresh token + reconnect per
   batch on any error. Never trust `send()` success.
8. **Quiet period + rescan** — after any send burst, wait ~4 min (server lags minutes
   under load), rescan the list, resend stragglers, up to 3 rounds. Deletion is
   confirmed ONLY by absence from the msgs list — `GET /msg/text/<mid>` returns 200
   even for deleted messages.
9. **Artifact** — delivered to the widget through the Binding (see contract below).

## Handcent Anywhere

- REST base: `https://aw.handcent.com`; `deviceId=2555969`.
- Conversations: **0321 (СберБизнес) = cid 276**, **900 (consumer Sber) = cid 277**.
  Discovery: `GET /nws/any/a/cons?deviceId=2555969`.
- Auth headers on every request: `username`, `res`, `token` (JWT) — read from
  `auth-headers.json`, **never logged or printed**. Token expires ~every 5 weeks
  (refresh ritual: `_RUNBOOK.md`).
- Messages carry: `mid`, `timestamp` (ms), `data` (full text), `messageType`.

## Websocket deletion pipe (browser-free since 2026-09-27)

1. `POST /nws/any/a/ws/token` body `{"agent":"test"}` → token at `data.value`.
2. Connect `wss://aw.handcent.com/ws` — no token in URL.
3. **Mandatory** subscribe frame (`ws_config` with identity
   `seoffice@0_android_acc2959783cb55fe`, `user_token`, reply topics) and wait for
   `ws_info` — without it every frame is silently ignored. This was the single
   breakthrough that made scripted deletion work.
4. Delete frame per message: `{type:"user_data", data:{...sms:{date:<ts ms>, id:<mid>,
   cid:<276|277>, type:<messageType>, mode:"dm", hash:366922896}}}`. (Any int works for
   `hash`; 366922896 is what the app itself sends.)
5. Sockets flap: half-dead sockets accept sends and drop frames — hence verify-only-via-
   rescan. Reconnect-per-batch with a fresh token self-heals; the pure-Python socket is
   far more stable than the browser one ever was (110 frames, one socket).

## Parsers

- **0321**: `СберБизнес. Списание …` → expense row; `СберБизнес. Поступление …` →
  income row (grammar incl. multi-doc "по N документам", counterparty, balance).
  The parser uses `-abs(amount)` for expense and `abs(amount)` for income.
  Historical regexes were iterated against the original corpus.
- **900**: card-account message shapes with an op-type dispatch table:
  `purchase`, `purchase_sbp`, `transfer_out`, `transfer_in`, `sbp_in`, `refund`,
  `payment`, `fee`, `confirm`, `promo`, plus special shapes `declined` (insufficient
  funds — info only), `court_collection` (court-order debit), `otp`. The
  parser writes negative `purchase`, `purchase_sbp`, `transfer_out`, `payment`,
  `fee`, and `court_collection` amounts; positive `transfer_in`, `sbp_in`, and
  `refund` amounts. It normalizes magnitude with `abs` so existing signed SMS do
  not have their sign reversed. `confirm` and `declined` have blank amounts:
  neither records completed cash movement. `balance_after` is unchanged.
- **OTP classifier** (both senders): contains `Не сообщайте код никому`.
  Rule: **both senders' codes get a row** (internal label `otp`, four
  base keys with blank amount, the same shape as unparsed rows) so the codes stay
  glanceable in the widget strip. Nothing
  is purged without a verified row (decision #14, `_DECISIONS.md`).
- **unparsed** (zero-inbox): every scanned message that isn't parsed gets
  `{datetime, notes, mid, source}` — exactly those four base keys with
  blank amount. `unparsed` remains an internal label for counters. `promo` also
  has no cash amount. Review bank rows with blank amounts and their notes in Grist.

## Grist writes

- Base: `https://seoffice.getgrist.com/api/docs/tRknrJrfbW3L`; Bearer key from
  `grist_api.txt` (never printed).
- One table — `Transactions` by default, overridable via the `GRIST_TABLE` env var
  (Actions passes the repo Variable of the same name) — receives everything;
  `source` = `"0321"`/`"900"` for bank rows.
- Engine row keys:
  - **Base, written every row**: `datetime`, `notes`
    (raw SMS text), `mid`, `source`.
  - **Parsed bank operations only**: `amount`, `counterparty`, `balance_after`,
    `doc_number`, `account_to`, `account_from`.
  - `card`, `doc_date`, `direction`, `text`, `date2`, `notes2`, `via` are **not**
    written anymore — removed or renamed in the 2026-10-01/02 restructure (see
    `_GRIST.md`).
- The writer omits `op_type`; classification remains internal for parsing,
  OTP collection, and counters. The legacy Text column was removed after the
  signed-amount migration on 2026-10-14. See `_GRIST.md` for the sign mapping.
- Writers set ONLY their own fields; user columns (`category`, `performance`,
  `student`, `sprint_*`, manual edits of `notes`) are never touched.
- `datetime` = `sms_ts // 1000` (epoch seconds); display TZ is Asia/Vladivostok (UTC+10).
- Bank-row amount migration is a separate bounded operation; the normal sync
  deduplicates existing rows and preserves user-managed fields and notes.
- The engine handles legacy `f900-unsent` backlog messages through the
  normal full sender scan and signed verification. It no longer performs a second
  presence-only deletion pass that could bypass a failed amount check.

## Automation deployment

- **PRIMARY (since 2026-10-08, decision #11): GitHub Actions** —
  `.github/workflows/sms-sync.yml` (phone-triggered `repository_dispatch` +
  `workflow_dispatch` + a `*/15` cron safety net; concurrency group `sms-sync`).
  The run+upload step is the composite action `.github/actions/sms-sync-run/action.yml`,
  which passes `HANDCENT_AUTH` / `GRIST_API_KEY` secrets and the `GRIST_TABLE`
  Variable. Workflow edits can't be pushed by the MCP gateway (it refuses
  `.github/workflows/`) — use the GitHub web UI or a local git push. See `_RUNBOOK.md`.
- **HISTORICAL FALLBACK: Kimi Work Blueprint Automation**
  `automation_112c8e63-0532-432e-9e61-ef8b627db65d` ("SMS → Grist sync"): manual
  trigger, Python, 15-min timeout, entry `sms_grist_sync.py`; triggered from the
  "SMS → Grist bridge" widget on the Daily Finance canvas. It was unavailable
  during this rollout and its assets were not upgraded. Deploy and verify the
  same engine as Actions before reusing it; the old writer expects the removed
  column and unsigned amounts.
- Signed-amount rollout completed at `d98cd6d`: Actions was paused while 810 bank
  rows were verified, 770 debit signs changed, and two confirmation amounts
  cleared. The curated magnitude difference was preserved. The legacy column
  was removed after verification and Actions re-enabled. No manual phone
  synchronization was run; future manual runs require an explicit user request.
- Live verification 2026-10-02 against the unified ledger (then id `RevenueBase`,
  now `Transactions`): 900 → 2 scanned / 2 rows / 2 deleted; 0321 → 2 scanned /
  1 row (1 OTP purged) / 2 deleted; all writes "VERIFY OK".

## Artifact contract (widget Binding)

```jsonc
{
  "generatedAt": "ISO8601+10:00",
  "requested": "276|277|all|0321|900",
  "tokenExpired": false,           // true => REST 401; widget shows red banner
  "senders": {
    "900":  { "scanned": 0, "newFound": 0, "rowsWritten": 0, "smsDeleted": 0,
              "pending": 0, "parseFailures": 0, "lastSync": "ISO8601", "ok": true },
    "0321": { /* same */ }
  },
  "latestCodes": [{ "sender": "900|0321", "date": "ISO8601", "text": "…" }],
  "log": [ "trimmed to last 40 lines" ]
}
```

Semantics worth knowing:
- `parseFailures` counts **unparsed rows written** (post twin-fold) — nonzero is normal
  operation under the zero-inbox philosophy, not an error.
- `rowsWritten` includes code rows (internal label `otp`) since 2026-10-09
  (decision #14); omitting the persisted label does not change that contract.
- A run can be runner-`succeeded` while a sender has `ok:false` + `error` (per-sender
  crash isolation). Always read the senders, not just the run status.
- `pending` = messages in the final rescan (includes arrivals during the run window).
