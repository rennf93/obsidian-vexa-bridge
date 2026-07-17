# Limitations

- **Read-only against Vexa by default.** The adapter reads meetings + transcripts via the `api-gateway`; the summary write-back to Vexa (`VEXA_NOTES_ENABLED`) is OFF by default because Vexa 0.12 has no stable external write surface for `meeting.data.processed`. See [Vexa version targeting](vexa-version-targeting.md). The Obsidian vault is the source of truth for summaries.
- **Local state is the source of truth for "done".** Vexa's PATCH can't carry arbitrary metadata, so the adapter owns the done/poisoned set in `state.json`. Lose `state.json` (unmounted `STATE_DIR`) and every meeting is re-summarized.
- **One note per meeting.** No incremental update — re-summarizing a meeting that's already done requires clearing its `state.json` entry; the sink's "file already exists" check then re-marks it done without overwriting the note.
- **No diarization of its own.** Speaker tags come from Vexa's transcript segments; the adapter doesn't re-diarize.
- **Python 3.11 only.** Pinned for parity with `discord-vexa-bridge` (the source adapter). Not load-bearing here (no `audioop`), but kept so the adapter pair stays on one runtime.
- **Not a published package.** `package = false` in `pyproject.toml` — it's a runnable service, not something you `pip install`.
- **Not affiliated with Obsidian or Vexa.** This is an independent adapter between two projects.
