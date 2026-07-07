# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## Project overview

`obsidian-vexa-bridge` is the **sink** adapter in an in/out pair around [Vexa](https://github.com/Vexa-ai/vexa): `discord-vexa-bridge` is the *source* (Discord → Vexa transcripts); this repo is the *sink* (Vexa transcripts → Obsidian notes). A long-running container polls Vexa for completed meetings across all platforms, summarizes each through an OpenAI-compatible LLM, writes a structured markdown note to a filesystem sink synced into the Obsidian vault, and optionally writes the summary back to the Vexa meeting `notes` field.

**Python**: pinned to **3.11** for parity with `discord-vexa-bridge` (not load-bearing here — no `audioop` — but kept so the adapter pair stays symmetric). Do not bump the runtime or base image past 3.11.
**Package manager**: uv. **Not** a published package (`package = false`) — it's a runnable service.

## Layout

- `summarizer/` — the service package:
  - `config.py` — env → typed `Config`; `ConfigError` on missing required vars for enabled features.
  - `types.py` — `Meeting`, `Utterance`, `MeetingMeta` dataclasses.
  - `vexa.py` — Vexa api-gateway client: list completed meetings, fetch transcript, write notes.
  - `llm.py` — LiteLLM-based summarize; lazy-imported.
  - `obsidian.py` — sink-agnostic note assembly (`assemble_note`, `note_path`, `frontmatter`) + the two sinks: `create_note` (MCP) and `write_note_fs` (filesystem).
  - `state.py` — `state.json` store: done / poisoned / failure-count.
  - `__main__.py` — the poll loop (`main`) + `run_once` (one pass) + `--once`/`ONCE=1`.
- `tests/` — unit tests (pytest, asyncio_mode=auto). No network or live Vexa required; the HTTP seams are monkeypatched.
- `scripts/mint_token.sh` — NAS-side helper that mints a Vexa per-user tx token and prints the `VEXA_SUMMARIZER_TOKEN=…` env line for the user to append to `/volume1/vexa/.env`.

## Pipeline (data flow)

Vexa completed meetings → poll loop → fetch transcript (speaker-tagged) → min-duration guard → LLM summarize → assemble note → filesystem sink (`VAULT_DIR`, Syncthing → vault) or MCP sink → optional Vexa `notes` PATCH → `mark_done` in `state.json`.

## Commands

```bash
make install     # uv sync --extra dev
make test        # pytest
make lint        # ruff check + format check
make fix         # ruff auto-fix + format
make typecheck   # mypy (must stay clean)
make security    # bandit
make check-all   # lint + typecheck + security + test
make pre-commit  # run all pre-commit hooks
make build       # build the Docker image
```

CI (`.github/workflows/ci.yml`) runs `pre-commit run --all-files` (ruff, ruff-format, mypy, bandit) and the pytest suite on Python 3.11. Keep all of these green.

## Conventions

- Match the surrounding style. The docstrings in `summarizer/` document the idempotency / poison model and the sink contract — keep them accurate; don't strip them.
- `mypy summarizer` is currently clean (0 errors) and gated in CI — keep it that way. Prefer narrowing/guards over `# type: ignore`.
- Never commit secrets. `VEXA_API_KEY` / `VEXA_SUMMARIZER_TOKEN`, `AI_API_KEY`, `OBSIDIAN_MCP_TOKEN`, `.env`, and `.env.*.local` are runtime config and gitignored.
- The LLM routes through an OpenAI-compatible `/v1` endpoint. On the NAS use `http://ollama:11434/v1` with `AI_MODEL=openai/<pulled-model>` and `AI_API_KEY=not-needed` (ollama ignores the key; LiteLLM requires a value). Plain `ollama/*` ids hit `/api/generate` and the local raw models have no chat template — use the `openai/` prefix.
- The invariant: `mark_done` is the only commit and it runs last. `create_note`'s fail-if-exists and `write_note_fs`'s "file already exists" no-op are the crash-recovery backstops across both sinks — don't weaken them to overwrites, or a crash mid-pass can duplicate or clobber a note.
