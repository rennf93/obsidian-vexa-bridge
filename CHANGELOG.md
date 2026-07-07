# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
