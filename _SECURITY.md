# Security

> Answers the three notes in the original draft: SMS safety, PAT scope, public vs private.

## Threat model, honestly

- **SMS is inherently unsafe** — plaintext, carrier-visible, and (by the bank's own
  design) synced to a third-party cloud, Handcent Anywhere. Acknowledged and accepted.
  This project doesn't worsen that: it *shortens* phone-side retention (zero inbox)
  and moves curation into a store the user controls (self-hosted Grist).
- **What the credentials can do:** the Handcent JWT reads and deletes every message in
  the synced account; the Grist key grants full doc access. Both are treated as
  bank-grade secrets.

## Current posture (updated 2026-10-09)

- **The repo is PUBLIC and hosts live automation** (decision #11): the Actions phase
  shipped 2026-10-08, deviating from the "private first" plan in `docs/_ROADMAP.md`.
  Compensations on record: free unlimited standard-runner minutes on a public repo;
  secrets structurally kept out of the repo and out of the artifact (the engine never
  prints or logs header/key values; the composite action masks them and uploads only
  per-sender counters + log tail); no credential-like values in code or comments.
- **Secrets live in GitHub Secrets** (Settings → Secrets and variables → Actions):
  `HANDCENT_AUTH` (full contents of auth-headers.json) and `GRIST_API_KEY`, plus the
  non-secret `GRIST_TABLE` Variable. Local copies for the secondary Kimi Work path
  stay as files on the user's PC (`C:\Users\Userman\Documents\kimi\workspace\
  auth-headers.json`, `grist_api.txt`) and in the automation assets dir — never
  printed, never logged, never committed.
- **The phone holds one fine-grained PAT** inside MacroDroid (the SMS-received macro
  that POSTs `repository_dispatch`): scoped to `arkhivar/grist-handcent` only —
  Contents (read-write, needed for dispatch), Actions (read-write), Metadata (read).
  It can ask GitHub to run the sync and nothing more; it never sees Handcent or Grist
  credentials. (The "Grist dispatcher widget" from `docs/_ROADMAP.md` Phase 2 was
  superseded by this phone-side trigger and was never built.)
- **Workflow hardening in place:** no PR-triggered workflows (only
  `repository_dispatch` / `workflow_dispatch` / `schedule`); the `concurrency` group
  `sms-sync` prevents overlapping destructive runs; third-party actions are pinned to
  major-version tags today (`actions/checkout@v4`, `actions/setup-python@v5`,
  `actions/upload-artifact@v4`) — SHA-pinning them is an open item on the README
  Roadmap.
- Fixtures and tests use synthetic data only — real SMS text is financial PII even in
  a private repo. `.gitignore` guards the secret file names at the repo root.
- **Rotation:** Handcent JWT every ~5 weeks via the local ritual, then
  `gh secret set HANDCENT_AUTH < auth-headers.json` (`_RUNBOOK.md`); the phone PAT on
  suspicion or yearly.
