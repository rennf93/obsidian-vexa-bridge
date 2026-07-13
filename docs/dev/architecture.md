# Architecture

The adapter is a long-running poll loop. There is no scheduler, no queue, no worker pool — one pass at a time, then sleep, then repeat. The design is deliberately boring so that crash recovery is boring too.

## The poll loop

```
load_config (env -> Config)
  -> run_once (one pass)
     -> list completed meetings per platform (Vexa GET /meetings)
     -> for each not done / not poisoned (state.json):
        -> fetch transcript (speaker-tagged segments)
        -> min-duration guard (MIN_TRANSCRIPT_SECONDS)
        -> summarize via LLM (LiteLLM, OpenAI-compatible /v1)
        -> assemble note (frontmatter + summary + optional transcript)
        -> write to sink (fs: VAULT_DIR, or mcp: OBSIDIAN_MCP_URL)
        -> optionally PATCH Vexa meeting notes (VEXA_NOTES_ENABLED)
        -> mark_done in state.json  <-- the only commit; runs LAST
  -> sleep POLL_INTERVAL_SECONDS (default 180)
  -> repeat
  -> SIGTERM (docker stop) cancels the sleep and exits cleanly between passes
```

Source: [`summarizer/__main__.py`](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/summarizer/__main__.py).

## The one invariant: `mark_done` is the only commit

`mark_done` is the only write that commits a meeting as summarized, and it runs **last** in the pass. Everything before it is reversible:

- A crash **before** `mark_done` leaves the meeting un-marked, so the next pass simply redoes it.
- A crash **between** the sink write and `mark_done` is caught by the sink's "file already exists" check: the next pass re-attempts the write, sees the file is already there, treats that as success, and marks done. You get one note, not two.

Do **not** weaken the sinks to overwrites — that invariant is what makes a mid-pass crash harmless. See `write_note_fs` (`target.exists() -> return`) and `create_note` (`"exist" in msg -> return`) in [`summarizer/obsidian.py`](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/summarizer/obsidian.py).

## Poison model

A meeting that fails `record_failure` 5 times (`POISON_LIMIT = 5` in [`summarizer/state.py`](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/summarizer/state.py)) is marked `failed` and skipped on every subsequent pass until `state.json` is cleared manually. This stops a broken meeting (e.g. a transcript Vexa can't serve) from hot-looping every poll.

## SIGTERM is clean between passes

`docker stop` sends SIGTERM. The loop's signal handler sets an `asyncio.Event`; the current pass completes (or the sleep is cancelled) and the process exits between passes — never mid-write. `--once` / `ONCE=1` runs a single pass and exits, used for validation, `DRY_RUN` first-runs, and manual reruns.

## `DRY_RUN`

`DRY_RUN=true` runs the full pipeline (including the LLM call) but writes nothing and does not `mark_done`. Safe to repeat for first-run validation — the meeting is retried on the next real pass.