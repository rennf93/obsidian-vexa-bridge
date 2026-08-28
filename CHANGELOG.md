# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Docs and the compose snippet now point `VEXA_API_URL` and `depends_on` at Vexa's `gateway` service (Vexa 0.12's stock compose name), not `api-gateway` (the 0.10 name), which failed DNS on any current Vexa stack.

## [0.3.3] - 2026-08-28

### Fixed
- Google Meet transcripts no longer render absolute epoch timestamps as elapsed time. Vexa 0.12's Meet lane stamps each segment with the bot's wall-clock `Date.now()`, so `start`/`end` arrive as epoch seconds while Discord and the mixed lane send meeting-relative offsets; a real Meet utterance rendered as `[496648:28:08]` with a `duration` of `00:00:38`. `vexa.get_transcript` now rebases epoch-scale values onto `meeting.start` at the one chokepoint both transcript builders return through, deciding per field rather than per row (a row can carry an epoch `end_time` next to a `start_time` that fell back to `0.0`), and clamps an `end_time` that lands before its own `start_time`.

### Added
- Coverage gate: a meeting whose speech seconds cover less than `MIN_TRANSCRIPT_COVERAGE` (default `0.05`, `0` disables) of its wall-clock duration is skipped instead of summarized. Speech seconds alone cannot tell a quiet meeting from one whose audio the transcription worker rejected: a 45-minute call that yielded 38 seconds of transcript still cleared `MIN_TRANSCRIPT_SECONDS=30` and was published as a confident summary. Only meetings at least five minutes long are gated, the ratio is clamped at 1.0 (Discord streams each speaker separately, so overlapping speech legitimately exceeds wall clock), and the webhook path leaves a gated meeting eligible for a later poll rather than marking it skipped.

## [0.3.2] - 2026-08-27

### Changed
- Graph mode's poll pass (`_run_once_graph`) and webhook event path (`process_event_meeting`) now share one helper, `graph.finalize_graph_pass`, for the trigger-the-fold / push / pull sequence instead of duplicating it; behavior is unchanged, including the event path's extra wait for the agent's commit before pushing.
- The direct-Postgres transcript fallback (`vexa._get_transcript_from_db`) now wraps `asyncpg.connect` and query failures (any `asyncpg` error or `OSError`) as `VexaError` instead of letting them surface raw, with the original exception kept as `__cause__`.

## [0.3.1] - 2026-08-27

### Changed
- The event path now pushes the agent's commit as soon as it lands instead of waiting for the next poll pass: `process_event_meeting` (graph mode, after an upload) records the workspace's HEAD sha, triggers the fold, then polls `GET /agent/workspace/git` for a new commit (`WEBHOOK_COMMIT_WAIT_SECONDS`, default `600`, interval `WEBHOOK_COMMIT_POLL_SECONDS`, default `15`) before pushing; a timeout logs a warning and still pushes once, so the poll pass remains the eventual backstop. `summarizer/agent_api.py` gains `git_head`; `summarizer/graph.py` gains `wait_for_commit`.

## [0.3.0] - 2026-08-27

### Added
- Event-driven webhook receiver: the bridge can now receive Vexa's signed `meeting.completed` webhook and process that meeting immediately instead of waiting for the next poll tick. The poll stays the fallback either way. Supported by both `BRIDGE_MODE=note` and `BRIDGE_MODE=graph`.
- `summarizer/webhook.py`: signature verification (`X-Webhook-Signature: sha256=<hmac>`, plus the legacy `Authorization: Bearer <secret>`), `webhook.v1` envelope parsing, an aiohttp app (`POST <WEBHOOK_PATH>`, `GET /healthz`) with `event_id` dedup and a delayed background dispatch to the meeting handler, and `register_with_vexa` (`PUT /user/webhook`).
- New env vars: `WEBHOOK_ENABLED` (default `false`), `WEBHOOK_HOST` (default `0.0.0.0`), `WEBHOOK_PORT` (default `8080`), `WEBHOOK_PATH` (default `/webhook`), `WEBHOOK_SECRET` (required when enabled), `WEBHOOK_PUBLIC_URL` (optional, triggers automatic registration with Vexa at startup), `WEBHOOK_DELAY_SECONDS` (default `20`). See [Configuration](docs/usage/config.md#webhook-receiver).
- `process_event_meeting`: processes one webhook-delivered meeting outside the poll loop, reusing the same per-meeting logic (`process_meeting`) as the poll passes; a transcript still below `MIN_TRANSCRIPT_SECONDS` at event time is left for the next poll instead of being marked skipped, since Vexa's completed transition can precede the last transcript flush.
- `Dockerfile`: `EXPOSE 8080` for the optional webhook receiver.

## [0.2.1] - 2026-08-27

### Changed
- Graph mode triggers an immediate run of the fold routine right after a pass uploads a transcript, instead of waiting for the next cron tick; `GRAPH_ROUTINE_CRON` now defaults to hourly (`0 * * * *`, was every 15 minutes) since the cron is now just the safety net.
- `ensure_routine` now retires a routine whose cron no longer matches `GRAPH_ROUTINE_CRON` before creating the new one, so an existing deployment upgrading past the hourly default doesn't end up with both the old and new cron running side by side.

## [0.2.0] - 2026-08-27

### Added
- `BRIDGE_MODE=graph`: an alternate mode with no LLM in the bridge. Each completed meeting's transcript is uploaded into a Vexa agent workspace; a standing Vexa routine folds it into an Open Knowledge Format knowledge graph (people, companies, projects, meetings, decisions, topics, all wikilinked), and the bridge pushes the agent's commits and fast-forwards the vault mirror folder. Requires a self-hosted Vexa 0.12 with `agent-api`; hosted and Kubernetes Vexa stay `BRIDGE_MODE=note`.
- `workspace-template/`: the Open Knowledge Format conventions, the fold routine prompt, and Dataview dashboards shipped for a new graph-mode workspace repo.
- `python -m summarizer init-workspace <target> --repo-url <url> [--api-base <url>]`: copies the workspace template into a local repo clone and prints the Vexa calls that attach it as the agent workspace.
- Direct-Postgres transcript fallback (`VEXA_DATABASE_URL`) for platforms the gateway's transcript route rejects, notably `discord` rows written by `discord-vexa-bridge`. Used by both modes; opt-in, tried only after the gateway call fails.
- New env vars: `BRIDGE_MODE` (default `note`), `VEXA_VAULT_FOLDER` (default `Vexa`), `GRAPH_ROUTINE_NAME` (default `meeting-to-graph`), `GRAPH_ROUTINE_CRON` (default `*/15 * * * *`), `VEXA_DATABASE_URL` (optional). See [Configuration](docs/usage/config.md).

### Fixed
- Graph mode accepts the upload response shape Vexa 0.12.22 actually returns (`{"files": [...]}`); a successful upload was previously recorded as a failure.
- A `DRY_RUN` pass in graph mode no longer creates the Vexa routine; a dry run writes nothing, on Vexa's side included.

## [0.1.1] - 2026-07-07

### Added
- GitHub community + workflow ecosystem (FUNDING, issue/PR templates, dependabot, labeler + labels, code-ql, stale, sync-labels, greetings), release automation (`release.yml` — auto-builds + pushes the image to GHCR + Docker Hub on release publish), a CLA assistant workflow + CLA.md, and a `scripts/bump_version.py` helper. Parity with `discord-vexa-bridge`.

## [0.1.0] - 2026-07-07

### Added
- Initial release: the `summarizer` package extracted from `discord-vexa-bridge` 0.4.x as a standalone Vexa → Obsidian sink adapter — the out-side of the in/out adapter pair around Vexa (`discord-vexa-bridge` is Discord → Vexa; this is Vexa → Obsidian).
- Long-running container poll loop (replaces the Mac launchd timer): lists completed meetings, summarizes each, sleeps `POLL_INTERVAL_SECONDS`, repeats. Graceful `SIGTERM` exit so `docker stop` is clean between passes.
- Filesystem note sink (`OBSIDIAN_SINK=fs` + `VAULT_DIR`) for NAS deployment with Syncthing into the Mac vault — no Mac-side runner required.
- MCP sink retained (`OBSIDIAN_SINK=mcp`) for Mac-local runs against the `vault-as-mcp` plugin.
- LLM via any OpenAI-compatible endpoint (defaults route to a self-hosted ollama at `http://ollama:11434/v1`); provider-agnostic via LiteLLM's `AI_MODEL=provider/model` dialect.
- `--once` / `ONCE=1` one-shot mode for validation, `DRY_RUN` first-runs, and manual reruns.
- Idempotent `state.json` with 5-strike poison; `mark_done` is the only commit, the filesystem sink's "file already exists" check is the crash-recovery backstop.
- Per-platform filter (`SUMMARIZE_PLATFORMS`) so Discord, Zoom, and Google Meet are all covered by one env var — no code change for a new platform.

### Changed (vs the in-bridge summarizer)
- Poll loop instead of a Mac launchd timer; the service is now a long-running container, not a per-tick process.
- Default sink is the filesystem (`OBSIDIAN_SINK=fs` + `VAULT_DIR`); the vault-as-mcp HTTP sink was the previous default and is now the `OBSIDIAN_SINK=mcp` alternate.
- `OBSIDIAN_MCP_TOKEN` is now required only when `OBSIDIAN_SINK=mcp` (previously required whenever `OBSIDIAN_ENABLED=true`).
- `VAULT_DIR` is required when `OBSIDIAN_SINK=fs` (no sane default in a container).
