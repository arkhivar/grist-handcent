# Evolution of this project

> The short version: a manual click-grind in a browser became a reverse-engineered
> API pipeline, then a one-click dashboard widget, then a single-table Grist store
> with a zero-inbox philosophy. Built alongside [Kimi Work](https://www.kimi.com)
> (Moonshot AI) — it did the reverse-engineering, parser iteration, and all of the
> wiring described below.

## Timeline

### August 3–5 — the WebBridge click-grind
- Duplicate 0321 bank SMS were being deleted by hand: WebBridge-driven CDP mouse events
  against the Handcent web app (`Input.dispatchMouseEvent`, hover-before-click).
- Learned the hard way: synthetic DOM events are flaky; semantics/screenshots lag ~5s;
  identical screenshot byte size = stale frame; **"Ваш код" OTP messages with different
  codes are NOT duplicates** (dedup = byte-identical text only).
- Session output: everything packaged into the `handcent-sms-dedup` skill (still the
  canonical API reference — see `_PIPELINE.md`).

### August 7 — REST API discovered, Grist born
- Found Handcent Anywhere's read-only REST API (`/nws/any/a/msgs`) by probing the web app.
- Critical trap documented: `GET /msg/text/<mid>` returns HTTP 200 **even for deleted
  messages** — deletion can only be verified by re-fetching the message list.
- User caught a parser bug: "Баланс …" is never a standalone record, always the tail of a
  Списание/Поступление message.
- Grist migration handoff prepared; the **write → read-back → verify → then delete**
  ordering was explicitly user-blessed ("100% right logic"): a Grist outage
  or parse bug must never eat a record.

### September 13–27 — browser-free deletion
- WebBridge daemon died (Chrome off). Instead of reviving it, built `py_pipe.py`: a
  pure-Python websocket pipe to Handcent's own delete channel.
- Breakthrough: blind frame replays are silently ignored — the server needs a proper
  `ws_config` subscribe handshake. With that solved: 110 delete frames through one
  unbroken socket, zero flaps.
- Sep 27: two full sync days — every new message written to Grist, read back, verified,
  and deleted from the phone; 57 OTP codes purged; bank twin-sending handled by
  twin-folding. Ended with the dashboard handoff spec.

### September 28 — the widget, and the scare that wasn't
- A Kimi Work update appeared to eat yesterday's history. Investigation showed nothing
  was lost: the work had continued inside an old conversation whose title/date metadata
  had frozen — it was hiding in plain sight. Lesson: verify before mourning.
- Built the **SMS → Grist bridge**: Blueprint Widget (three buttons, per-sender status
  face, latest-codes strip, 401 banner) + manual-run Python Automation + Binding.
  Live-verified on first run (2 real messages: written, verified, deleted).
- Migrated Grist to a single **Transactions** table (919 rows, `source` column);
  old tables kept as archive. See `_GRIST.md`.

### September 29 — zero inbox
- Adopted the philosophy: **the phone is a dumb relay, Grist is the only sorting
  surface.** Unclassified messages became first-class `op_type="unparsed"` rows;
  both phone conversations were emptied end-to-end (34 + 2 messages).
- Survived the first real incident (transient DNS failure — root cause later traced to
  the user's VPN; see `_RUNBOOK.md`) and shipped the fix for its one real bug.
- Repo created; knowledge base written (the `_*.md` set).

### October 1 — settling in
- Zero-inbox in daily use: incoming SMS curated straight from Grist, phone stays empty.
- Schema tidy: `direction` column removed (redundant with `op_type`), `op_type`
  converted to single-select; engine updated and harness-verified the same day.

### October 1–2 — one ledger to rule them all
- The user merged `Transactions` into **RevenueBase** — the unified live payment
  ledger: 1,977+ rows = 1,187 manual payment rows + 790 bank rows, with `SUM(Paid)`
  unchanged as the migration checksum.
- Manual field restructure in the same pass (6 changes): `direction` deleted;
  `card` made user-assigned only (engine no longer writes it); `doc_date` retired
  (data moved to the payment-side `Date`); `via` merged into `source`; `notes2`
  dropped (was always empty); and the rename/merge pair — `text` into `notes`,
  `date2` into `datetime`.
- Engine re-pointed to RevenueBase (one module constant) and live-verified end-to-end
  on 2026-10-02: 900 → 2 scanned / 2 rows / 2 deleted; 0321 → 2 scanned / 1 row
  (1 OTP purged) / 2 deleted; every write read back "VERIFY OK".
- Archives reduced to `Income` (19 rows) — Expenses, T900, the pre-merge Transactions
  and Transactions2 are gone.

### October 8 — event-driven cloud sync
- The engine moved to GitHub Actions: phone-side Tasker/MacroDroid POSTs a
  `repository_dispatch` on incoming SMS; Actions runs the same verified engine
  (`runner.py` + `sms_grist_sync.py`, run/upload step in `.github/actions/sms-sync-run`)
  with secrets in repo Secrets. A 15-min schedule is the safety net while the phone
  trigger is proven (it costs Actions minutes; delete once stable).
- Secret resolution flipped: env vars first, local files as fallback — the Kimi Work
  automation remains the dev harness/fallback. See decision #11 in `_DECISIONS.md`.
- One manual activation step: the workflow ships at `workflows/sms-sync.yml` (the MCP
  gateway won't write `.github/workflows/`); move it there to activate.

### October 9 — the rename that broke the engine (softly)
- The user had renamed the unified ledger `RevenueBase` → `Transactions` on
  2026-10-06; since this Grist instance has no rename endpoint, the table **id**
  changed and the engine's hardcoded `TABLE` constant silently broke all writes.
- Discovered today; the engine was re-pointed in a one-line change. The write →
  read-back → verify → then-delete invariant did its job: ~21 messages (11×0321 +
  10×900) had simply queued on the phone, and all were synced that morning — every
  write "VERIFY OK", then deleted. The cloud Actions runs pick up the fix from the
  repo automatically. See decision #12 in `_DECISIONS.md`.

## Where the pieces live

| Piece | Location |
|---|---|
| Canonical sync engine (`sms_grist_sync.py`) | this repo (to be imported — deployed copy lives in the Kimi Work automation assets) |
| Actions entry (`runner.py`) + workflow | this repo (`workflows/sms-sync.yml` → move to `.github/workflows/` to activate) |
| API history & rituals | `handcent-sms-dedup` skill (local Kimi Work skills dir) |
| Operations console | Kimi Work widget "SMS → Grist bridge" on the Daily Finance canvas |
| Data | Grist doc `tRknrJrfbW3L`, table `Transactions` (unified ledger, renamed from `RevenueBase` on 2026-10-06; `Income` is the only archive) |
| Design system for future Grist widgets | [arkhivar/grist](https://github.com/arkhivar/grist) (`shared/base.css`, `shared/core.js`, `AGENTS.md`) |
