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

## Current posture

- Secrets (`auth-headers.json`, `grist_api.txt`) live as local files next to the
  deployed engine — never printed, never logged, never committed. `.gitignore` must
  exist before the first code commit; fixtures use synthetic data only (real SMS text
  is financial PII even in a private repo).
- The deployed copies (Kimi Work automation assets) stay outside the repo.
- **The repo is public while docs-only. Make it private before any workflow, secret,
  or PAT lands** (see `_ROADMAP.md`, Phase 1).

## GitHub token scope (for the Actions / dispatcher phases)

- One **fine-grained PAT** scoped to `arkhivar/grist-handcent` only:
  Contents (read-write — needed for `repository_dispatch`), Actions (read-write),
  Metadata (read). Nothing else, no other repo, no org scope.
- No PR-triggered workflows; third-party actions pinned to a full commit SHA;
  a `concurrency` group prevents overlapping destructive runs.
- The Grist widget dispatcher holds **only this PAT** — never Handcent or Grist
  credentials. It can ask GitHub to run the sync and nothing more.
- Rotation: Handcent JWT every ~5 weeks via the local ritual (`_RUNBOOK.md`);
  the PAT on suspicion or yearly.
