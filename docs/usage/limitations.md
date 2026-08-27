# Limitations

- **Read-only against Vexa by default.** The adapter reads meetings + transcripts via the `api-gateway`; the summary write-back to Vexa (`VEXA_NOTES_ENABLED`) is OFF by default because Vexa 0.12 has no stable external write surface for `meeting.data.processed`. See [Vexa version targeting](vexa-version-targeting.md). The Obsidian vault is the source of truth for summaries.
- **Local state is the source of truth for "done".** Vexa's PATCH can't carry arbitrary metadata, so the adapter owns the done/poisoned set in `state.json`. Lose `state.json` (unmounted `STATE_DIR`) and every meeting is re-summarized.
- **One note per meeting (note mode).** No incremental update — re-summarizing a meeting that's already done requires clearing its `state.json` entry; the sink's "file already exists" check then re-marks it done without overwriting the note.
- **Graph mode has routine latency.** Uploaded transcripts sit in `uploads/` until the standing Vexa routine next runs; that's up to `GRAPH_ROUTINE_CRON` (15 minutes by default), not immediate.
- **The vault mirror folder is fast-forward only.** `<VAULT_DIR>/<VEXA_VAULT_FOLDER>` must stay a clean mirror of the agent workspace; any local edit there blocks the next `git pull --ff-only` until you resolve it by hand, so don't edit the mirror directly.
- **A diverged workspace remote is logged, not repaired.** If the workspace's git remote has diverged from the agent's local commits, the push step logs it and moves on; resolving the divergence is an operator step on the repo itself.
- **No use of Vexa's own live meeting copilot.** Graph mode only folds finished transcripts after a meeting ends; it doesn't touch whatever real-time in-meeting assistant Vexa ships separately.
- **No diarization of its own.** Speaker tags come from Vexa's transcript segments; the adapter doesn't re-diarize.
- **Python 3.11 only.** Pinned for parity with `discord-vexa-bridge` (the source adapter). Not load-bearing here (no `audioop`), but kept so the adapter pair stays on one runtime.
- **Not a published package.** `package = false` in `pyproject.toml` — it's a runnable service, not something you `pip install`.
- **Not affiliated with Obsidian or Vexa.** This is an independent adapter between two projects.
