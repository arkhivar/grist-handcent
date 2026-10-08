# Roadmap

> Assessment written 2026-09-29, after the widget went live. Phases are sequential;
> nothing here requires taking the working system down.

## Status (2026-10-09) — read this first

This assessment is **historical**. **Phase 1 shipped 2026-10-08** (decision #11 in
`_DECISIONS.md`), with two deliberate deviations: the dispatcher is a **MacroDroid
macro on the phone** — the Phase 2 Grist widget was never built, and its "Action
writes results back to a `SyncRuns` table" half wasn't implemented either (results
surface via the Actions artifact and the Kimi Work widget) — and the repo **stayed
public** instead of going private first (compensating design in `_SECURITY.md`;
public repos get free standard-runner minutes). The `*/15` cron safety net is still
in place while the phone trigger proves stable; no VLAT-night schedule gap was ever
implemented. The `--dry-run` flag and GitHub-issue-on-401 notifications were not
built — the write→verify→delete invariant (decision #1) is the safety net. Treat
everything below as design history, not pending work.

## Current state (all proven live)

- Engine: `sms_grist_sync.py` — pure Python (`websockets` 17.1 + stdlib), idempotent,
  write→verify→delete. See `_PIPELINE.md`.
- UI: Kimi Work widget "SMS → Grist bridge" (3 buttons, status face, codes strip,
  401 banner) on the Daily Finance canvas.
- Data: Grist `Transactions` single table. See `_GRIST.md`.

## Phase 0 — repository ✅ (2026-09-29)
Source of truth for code + knowledge. Non-negotiables already in force:
`auth-headers.json`, `grist_api.txt` never committed; fixtures use synthetic data only
(real SMS text is financial PII even in a private repo).

## Phase 1 — GitHub Actions backend

**Why it fits:** the pipeline is OS-agnostic, idempotent (mid-dedup makes overlapping
or retried runs safe), and its built-in ~4-minute quiet period is just runner time.

**Minutes math:** ~6 min per run with deletions. 2–3 runs/day, VLAT-aware schedule
(no night runs) ≈ 400–550 min/month, comfortably inside the 2,000 free private-repo
minutes. (Public repos get unlimited minutes — but read the security caveat first.
Actual 2026-10-08 shipping choice: `*/15` cron safety net, 24/7, ~500–900 min/month
if the repo were private — free while public; retire the cron once dispatch proves
stable.)

**Caveats, in order of seriousness:**
1. **Bank credentials move to someone else's cloud.** The Handcent JWT can read and
   delete all bank SMS. Mitigations: private repo; fine-grained PAT scoped to this repo
   only; no PR-triggered workflows; pin third-party actions to a commit SHA. *(Shipped
   as: public repo with secret-free artifacts; PAT lives in the phone's MacroDroid;
   third-party actions pinned to major-version tags — SHA-pinning remains open, see
   README Roadmap.)*
2. **The 5-week token expiry stays human-local.** Refresh still requires the WebBridge
   ritual (see `_RUNBOOK.md`); afterwards one `gh secret set` pushes the new
   headers into the repo. On 401 the Action should open a GitHub issue instead of
   silently failing. *(The issue-on-401 half was not built; the widget's red banner
   and the artifact's `tokenExpired` flag are the signals.)*
3. **Cloud-IP reachability is a question, not a risk.** Both endpoints resolve to
   public IPs from the home network, but Handcent has only ever seen this ISP's
   addresses. A US/Azure egress IP may be ignored or fraud-flagged. Validate with a
   single dry REST scan before building anything on top (~10 min experiment).
   *(Resolved in practice: cloud runs have been syncing successfully since
   2026-10-08 — the risk did not materialize.)*
4. **Destructive deletion unattended deserves a final guard.** Add a `--dry-run` flag
   first; let the first week of scheduled runs report "would have deleted N" without
   deleting. Add a `concurrency` group so cron can't overlap itself. *(Shipped with
   the concurrency group `sms-sync`; the dry-run flag was not built — decision #1's
   invariant is the guard.)*

**Triggers to wire:** `schedule` (cron, VLAT), `workflow_dispatch` (manual), and
`repository_dispatch` (from the Phase 2 Grist widget). *(Shipped: phone
MacroDroid/Tasker sends the `repository_dispatch`; workflow_dispatch manual;
`*/15` schedule as the safety net.)*

## Phase 2 — Grist dispatcher widget
Buttons inside Grist, built on the [arkhivar/grist](https://github.com/arkhivar/grist)
design system (`shared/base.css` tokens, `shared/core.js` helpers, conventions from its
`AGENTS.md`).

**Architecture:** the widget is a *dispatcher, not an executor*. The sync runs 5–13
minutes with a built-in sleep — nothing like that can live in a browser tab. The widget
sends one `repository_dispatch` call to GitHub (fine-grained PAT in widget config;
GitHub's API is CORS-friendly for this) and returns immediately.

**The elegant half:** the Action already holds the Grist API key, so *it* writes results
back — a `SyncRuns` row or a status cell on the Transactions page. Grist then natively
shows "last sync 09:14 · 6 rows · 0 errors" with zero polling, and the widget is just
three buttons and a spinner. The control surface lives where the data is reviewed.

*(Never built — superseded on 2026-10-08 by the phone-side MacroDroid trigger in
decision #11. The write-back half is still a nice idea if the dashboard is ever
de-emphasized further.)*

## What stays local, permanently
- Token refresh ritual (WebBridge + Chrome + localStorage).
- The Kimi Work widget as the machine-local console: latest-codes strip, red 401
  banner, manual trigger that works even when GitHub is down.
- Both UIs share one engine — they complement rather than compete.

## Open decision points
- **Private vs public repo** — recommend private until the Actions phase has run clean
  for a couple of weeks. (Tracked in `_SECURITY.md`.) *(Resolved 2026-10-08: stayed
  public; see `_SECURITY.md` for the compensating design.)*
- **Notification channel** for failures/expiry: GitHub issue (simplest), or push via
  ntfy/Telegram.
- **Auto-sync policy** once Actions lands: keep manual buttons, or schedule 2–3
  VLAT-daytime runs? The bank's message flow suggests mornings matter most.
