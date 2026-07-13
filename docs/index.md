# obsidian-vexa-bridge

Turn completed [Vexa](https://github.com/Vexa-ai/vexa) meetings into structured Obsidian notes — speaker-tagged, summarized by an LLM, and written to a folder that syncs into your vault. One service covers every platform Vexa records (Discord, Google Meet, Zoom, Teams).

This is the **sink** half of an in/out adapter pair around Vexa:

- **[`discord-vexa-bridge`](https://github.com/rennf93/discord-vexa-bridge)** — the *source* adapter: Discord voice → Vexa transcripts (DAVE/E2EE receive path, writes into Vexa's Postgres).
- **`obsidian-vexa-bridge`** (this repo) — the *sink* adapter: Vexa transcripts → Obsidian notes. It polls Vexa for completed meetings across all platforms, summarizes each through an OpenAI-compatible LLM, writes a structured markdown note to a filesystem sink synced into the vault, and optionally writes the summary back to the Vexa meeting's `notes` field.

Same pattern, opposite direction, separate repo + image + release cadence.

## What it is

A pure-REST consumer of Vexa's `api-gateway`. It needs no direct access to Vexa's Postgres — it reads meetings and transcripts through the same HTTP surface any third-party integration would, then writes notes to a filesystem folder (Syncthing mirrors it into your Obsidian vault) or to the Obsidian vault-as-MCP plugin. The summarization runs through any OpenAI-compatible `/v1` endpoint, including a self-hosted [ollama](https://ollama.com) on the same host — no cloud spend required.

The framing the Vexa maintainer uses: it's **Fireflies → Notion for self-hosters** — speaker-tagged meeting notes that land in your own vault, on your own hardware, with no per-seat SaaS in the loop.

## Run model

The container is a **long-running poll loop**, not a cron job. On startup it loads `summarizer/config.py` from env, then repeats: one pass (`run_once`) → sleep `POLL_INTERVAL_SECONDS` (default 180) → repeat. A `SIGTERM` (`docker stop`) cancels the loop cleanly between passes — no mid-pass corruption, because `mark_done` is the only commit and it runs last. `--once` (or `ONCE=1`) runs a single pass and exits, for validation, `DRY_RUN` first-runs, and manual reruns.

## Pipeline

```
Vexa (completed meetings, any platform)
  -> poll loop (POLL_INTERVAL_SECONDS) lists completed meetings per platform
  -> skip already-done / poisoned (state.json) and below MIN_TRANSCRIPT_SECONDS
  -> fetch transcript (speaker-tagged segments)
  -> summarize via OpenAI-compatible LLM (AI_MODEL / AI_BASE_URL / AI_API_KEY)
  -> assemble note (frontmatter + TL;DR + key points + action items + transcript)
  -> write to filesystem sink (OBSIDIAN_SINK=fs, VAULT_DIR) -> Syncthing -> Obsidian vault
     OR write via Obsidian MCP sink (OBSIDIAN_SINK=mcp, OBSIDIAN_MCP_URL)
  -> optionally PATCH Vexa meeting notes (VEXA_NOTES_ENABLED)
  -> mark_done in state.json (the only commit; crash mid-pass is harmless)
```

## Next

- [Deploy](usage/deploy.md) it alongside your Vexa stack.
- [Configuration](usage/config.md) — the full env surface.
- [Architecture](dev/architecture.md) — the poll loop and the idempotency model.