# Evolution of this project

> Fills the intent of `_EVOLUTION.md`. The short version: a manual click-grind in a
> browser became a reverse-engineered API pipeline, then a one-click dashboard widget,
> then a single-table Grist store with a zero-inbox philosophy. Built alongside
> [Kimi Work](https://www.kimi.com) (Moonshot AI) — it did the reverse-engineering,
> parser iteration, and all of the wiring described below.

## Timeline

### August 3–5 — the WebBridge click-grind
- Duplicate 0321 bank SMS were being deleted by hand: WebBridge-driven CDP mouse events
  against the Handcent web app (`Input.dispatchMouseEvent`, hover-before-click).
- Learned the hard way: synthetic DOM events are flaky; semantics/screenshots lag ~5s;
  identical screenshot byte size = stale frame; **"Ваш код" OTP messages with different
  codes are NOT duplicates** (dedup = byte-identical text only).
- Session output: everything packaged into the `handcent-sms-dedup` skill (still the
  canonical API reference — see [PIPELINE](PIPELINE.md)).

### August 7 — REST API discovered, Grist born
- Found Handcent Anywhere's read-only REST API (`/nws/any/a/msgs`) by probing the web app.
- Critical trap documented: `GET /msg/text/<mid>` returns HTTP 200 **even for deleted
  messages** — deletion can only be verified by re-fetching the message list.
- User caught a parser bug: "Баланс …" is never a standalone record, always the tail of a
  Списание/Поступление message.
- Grist migration handoff prepared; the **write → read-back → verify → then delete**
  ordering was explicitly user-blessed ("Yes please, 100% right logic"): a Grist outage
  or parse bug must never eat a record.

### September 13–27 — browser-free deletion
- WebBridge daemon died (Chrome off). Instead of reviving it, built `py_pipe.py`: a
  pure-Python websocket pipe to Handcent's own delete channel.
- Breakthrough: blind frame replays are silently ignored — the server needs a proper
  `ws_config` subscribe handshake. With that solved: 110 delete frames through one
  unbroken socket, zero flaps.
- Sep 27: two full sync days — every new message written to Grist, read back, verified,
  and deleted from the phone; 57 OTP codes purged; bank twin-sending handled by
  twin-folding. Ended with the dashboard handoff spec (see `_EVOLUTION.md`'s sibling:
  the spec became this repo's Phase 0).

### September 28 — the widget, and the scare that wasn't
- A Kimi Work update appeared to eat yesterday's history. Investigation showed nothing
  was lost: the work had continued inside an old conversation whose title/date metadata
  had frozen — it was hiding in plain sight. Lesson: verify before mourning.
- Built the **SMS → Grist bridge**: Blueprint Widget (three buttons, per-sender status
  face, latest-codes strip, 401 banner) + manual-run Python Automation + Binding.
  Live-verified on first run (2 real messages: written, verified, deleted).
- Migrated Grist to a single **Transactions** table (919 rows, `source` column);
  old tables kept as archive. See [GRIST](GRIST.md).

### September 29 — zero inbox
- Adopted the philosophy: **the phone is a dumb relay, Grist is the only sorting
  surface.** Unclassified messages became first-class `op_type="unparsed"` rows;
  both phone conversations were emptied end-to-end (34 + 2 messages).
- Survived the first real incident (transient DNS failure — see [RUNBOOK](RUNBOOK.md))
  and shipped the fix for its one real bug.
- Repo created. You are here.

## Where the pieces live (today)

| Piece | Location |
|---|---|
| Canonical sync engine (`sms_grist_sync.py`) | this repo (to be imported — currently deployed copy lives in the Kimi Work automation assets) |
| API history & rituals | `handcent-sms-dedup` skill (local Kimi Work skills dir) |
| Operations console | Kimi Work widget "SMS → Grist bridge" on the Daily Finance canvas |
| Data | Grist doc `tRknrJrfbW3L`, table `Transactions` |
| Design system for future Grist widgets | [arkhivar/grist](https://github.com/arkhivar/grist) (`shared/base.css`, `shared/core.js`, `AGENTS.md`) |
