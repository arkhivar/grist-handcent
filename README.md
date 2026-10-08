# grist-handcent
----
## GitHub Actions minutes consumption
Even though it's running locally now - we need to specify how much compute is spent and ways to reduce consumption (no runs during the night Vladivostok time VLAT, etc).

## Roadmap
Pending ideas and improvements, in no particular order:

- **MacroDroid self-diagnostics**: save the HTTP return code of the dispatch
  call into an integer variable (`http_code`) and (optionally) fire a
  notification when it isn't `204` — 401 = token problem, 404 = URL typo.
  Turns the phone macro into its own monitoring tool.
- **Retire the 15-min schedule**: once the phone-side trigger proves stable,
  delete the `schedule` block from the workflow to save Actions minutes
  (the nightly VLAT gap belongs to the section above).
- **Scrub `WS_IDENTITY`**: derive the Handcent account identity from
  `HANDCENT_AUTH` at runtime instead of hardcoding the username in the
  engine (mild disclosure in a public repo — not a credential).
- **Reconcile the `f900-unsent.json` backlog**: ~10 legacy mids were never
  written to any Grist table; decide whether to migrate or drop them, then
  delete the file.

Done recently:

- **Rename handshake** (2026-10-09): the engine reads its target table id from
  the `GRIST_TABLE` env var (repo Variable in Actions, default `Transactions`)
  instead of a hardcoded constant — future renames are a variable edit, not a
  code change. See decision #13.
