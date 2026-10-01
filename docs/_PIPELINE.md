# Pipeline technical reference

> The engine behind the "SMS → Grist bridge". Full API history (including dead ends)
> lives in the `handcent-sms-dedup` skill; this file is the working summary.
> Everything below was proven against the live services in Sep–Oct 2026.

## Run phases (per sender, then combined artifact)

1. **Scan** — paged REST read of the conversation (30/page, newest first,
   `before_timestamp=min(ts)-1`, 0.4s pacing). The *full* conversation is always
   scanned: a mid already in Grist can still be on the phone, and it's a deletion
   target — early-stop on "all known" was removed as a correctness fix.
2. **Classify** — parsers (see below); anything unmatched becomes an **unparsed** row.
3. **Twin-fold** — byte-identical SMS texts fold to ONE row (newest mid wins); **all**
   twin mids become deletion targets (the bank double-sends every debit).
4. **Dedup vs Grist** — `GET /records?filter={"mid":[...]}` in chunks of 8 (0.3–0.4s
   between chunks). Never full-table reads.
5. **Write** — `POST /tables/Transactions/records`, batches of 10 rows (server drops
   ~25KB POSTs — see `_GRIST.md`).
6. **Read-back verify** — re-fetch by mids (chunked), assert presence (and amount where
   applicable). **A message may only be deleted after its row is verified.** The only
   exception: 0321 OTPs, which are purged without any row.
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
  income row (grammar incl. multi-doc "по N документам", counterparty, balance,
  doc_date `DD.MM.YYYY`). Regexes were iterated against the full corpus to zero
  failures.
- **900**: card-account message shapes with an op-type dispatch table:
  `purchase`, `purchase_sbp`, `transfer_out`, `transfer_in`, `sbp_in`, `refund`,
  `payment`, `fee`, `confirm`, `promo`, plus special shapes `declined` (insufficient
  funds — info only), `court_collection` (court-order debit), `otp`. In/out/info is
  carried by `op_type` semantics; the former `direction` column was removed as
  redundant (2026-10-01).
- **OTP classifier** (both senders): contains `Не сообщайте код никому`.
  Rule: **0321 OTPs are deleted WITHOUT a Grist row** (never archived); **900 OTPs get
  a row** (`op_type="otp"`) so the codes stay glanceable in the widget strip.
- **unparsed** (zero-inbox): every scanned message that isn't parsed and isn't a
  0321 OTP gets `{date, text, mid, source, op_type:"unparsed"}` — nothing else.
  Review in Grist, not on the phone.

## Grist writes

- Base: `https://seoffice.getgrist.com/api/docs/tRknrJrfbW3L`; Bearer key from
  `grist_api.txt` (never printed).
- One table — `Transactions` — receives everything; `source` = `"0321"`/`"900"`.
- `op_type` is a **Choice (single select)** column; writers send plain string labels.
- Writers set ONLY their own fields; user columns (`category`, `performance`,
  `Created_at`/`Last_updated_at` triggers, `notes2`) are never touched.
- `date` = `sms_ts // 1000` (s); `doc_date` = epoch of `DD.MM.YYYY` tagged UTC;
  display TZ is Asia/Vladivostok (UTC+10).

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
- A run can be runner-`succeeded` while a sender has `ok:false` + `error` (per-sender
  crash isolation). Always read the senders, not just the run status.
- `pending` = messages in the final rescan (includes arrivals during the run window).
