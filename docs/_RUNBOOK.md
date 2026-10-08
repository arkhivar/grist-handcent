# Runbook

Operations for the SMS → Grist pipeline. Complements
`_PIPELINE.md` (how it works) and `_GRIST.md` (where data lives).

## Token refresh (the ~5-week ritual)

**Symptoms:** REST 401; widget red banner "token refresh needed"; artifact field
`tokenExpired: true`.

**Why it can't be automated:** the fresh JWT is minted inside the Handcent web app's
browser session; grabbing it needs the local WebBridge daemon (127.0.0.1:10086).

**Procedure:**
1. Handcent web app open in Chrome (visible tab).
2. Via WebBridge, read localStorage key `flutter.pref_login_info` — the value is
   **double-JSON-encoded**; the inner object has `username`, `token`, `res`, `email`.
   A fresh JWT's payload carries a **new `res`** — patch BOTH `res` and `token`.
3. Write both into `auth-headers.json` (every script reads that file; some old scripts
   have stale hardcoded headers — don't copy from them).
4. When the GitHub Actions phase lands: `gh secret set HANDCENT_AUTH < auth-headers.json`
   after each refresh.
5. WebBridge daemon down (connection refused on :10086)? Restart Kimi Work.

## GitHub Actions phase

The engine also runs on GitHub Actions (`arkhivar/grist-handcent`), triggered
by the phone — no awake PC required. The workflow ships at `workflows/sms-sync.yml`
because the MCP gateway refuses to write `.github/workflows/`; **activate it
manually**: move the file to `.github/workflows/sms-sync.yml` in the GitHub UI
(or `git mv` + push). The run+upload step it calls lives in
`.github/actions/sms-sync-run/action.yml` and is already in place.

- **Repo Secrets** (Settings → Secrets and variables → Actions):
  - `HANDCENT_AUTH` — full contents of `auth-headers.json` (one line, valid JSON).
  - `GRIST_API_KEY` — the Grist API key (contents of `grist_api.txt`).
- **JWT refresh (~every 5 weeks):** same browser-localStorage ritual as above,
  then update the secret instead of the file:
  `gh secret set HANDCENT_AUTH < auth-headers.json` (run from the repo root).
- **Manual trigger:** Actions tab → "SMS → Grist sync" → Run workflow → pick
  cid (276 / 277 / 0321 / 900 / all, default all).
- **Phone trigger:** Tasker/MacroDroid fires on the SMS-received broadcast and
  POSTs `repository_dispatch` (type `sms-sync`, optional `client_payload.cid`).
- **Logs/artifacts:** the run page shows the live log; the returned artifact JSON
  (per-sender counters, latest codes, log tail — never credentials) is attached
  as the `sms-sync-artifact` download.
- **Safety net:** a 15-min `schedule` run exists while the phone-side trigger is
  being proven; it burns Actions minutes (~500–900 min/month) — delete the
  `schedule` block in the workflow once dispatch is stable.

## Widget states — how to read them

- **Error chip on one sender** with run-level "succeeded": per-sender crash isolation
  did its job. Open the run log (or the artifact's `log`) for the actual error.
- **`unparsed` metric > 0**: normal under zero-inbox — that many rows were written for
  you to review in Grist.
- **`pending` > 0 after a run**: messages that arrived *during* the run window (the
  bank sends constantly). Next sync sweeps them.
- **Red token banner**: see above.

## Incident log

### 2026-09-29 — transient DNS failure (root cause found 2026-10-01: the VPN)
Two button presses (~09:39, 09:42 VLAT) failed with
`URLError: [Errno 11001] getaddrinfo failed` — the machine couldn't resolve
`aw.handcent.com`. The script retried once per run, reported `ok:false` per sender,
and deleted nothing. **Root cause (confirmed by repeated testing): the user's VPN** —
when it drops or reconnects, DNS resolution fails briefly. Not a pipeline defect.
**If `getaddrinfo` fails: check the VPN first**, then just re-run.

### 2026-09-29 — `dict(msgs)` bug (fixed)
First zero-inbox run crashed on 0321 with
`ValueError: dictionary update sequence element #0 has length 5; 2 is required` —
`dict()` was called on a list of 5-key dicts. Fired only when the conversation was
non-empty (900 happened to be empty). Fixed; an offline harness
(`offline_harness.py`, task workspace) replays a synthetic 0321 dataset against the
real `run_sender` to prove the deletion set, twin-folding, and row shapes.

### 2026-09-28 — stalled POST triplicates (cleaned)
During migration, one retried batch insert landed 3× (the server accepts the write
*and* times out the connection). Detected by count mismatch, cleaned via the
records-delete endpoint. **Lesson:** on this server, verify counts after every write
batch, not just at the end.

## Environment notes

- Managed Python ships `websockets` 17.1 (sync API) — no pip installs needed.
- All display times are **Asia/Vladivostok (UTC+10)**.
- **Windows Defender aggressively quarantines fresh unsigned binaries** on this
  machine (once ate `kimi-slides.exe`, cloud-heuristic false positive). If a tool or
  daemon "disappears", check Protection history before debugging code. Recommended:
  a permanent exclusion for the tools directory.
- Secrets (`auth-headers.json`, `grist_api.txt`) live as local files next to the
  deployed engine; they are never printed, logged, or committed. In the cloud phase
  they additionally live as GitHub Secrets (`HANDCENT_AUTH`, `GRIST_API_KEY`).
