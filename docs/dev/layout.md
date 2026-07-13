# Source layout

The service is the `summarizer/` package — a runnable application, not a published library (`package = false` in `pyproject.toml`).

## `summarizer/`

| File | Role |
|---|---|
| [`config.py`](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/summarizer/config.py) | Env → typed `Config`. `ConfigError` on missing required vars for enabled features. Pure: reads a mapping (defaults to `os.environ`). |
| [`types.py`](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/summarizer/types.py) | `Meeting`, `Utterance`, `MeetingMeta` dataclasses. |
| [`vexa.py`](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/summarizer/vexa.py) | Vexa api-gateway client: `list_completed_meetings`, `get_transcript`, `write_notes`. Read-only except `write_notes` (only fired when `VEXA_NOTES_ENABLED`). HTTP split into async seams so tests fake them without aiohttp. |
| [`llm.py`](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/summarizer/llm.py) | LiteLLM-based `summarize`. `build_prompt` and `parse_response` are pure (no network, no deps); `summarize` is the single impure seam (lazy-imports litellm). |
| [`obsidian.py`](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/summarizer/obsidian.py) | Sink-agnostic note assembly (`assemble_note`, `note_path`, `frontmatter`) + the two sinks: `create_note` (MCP) and `write_note_fs` (filesystem). Both sinks implement the "file already exists = success" idempotent backstop. |
| [`state.py`](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/summarizer/state.py) | `state.json` store: done / poisoned / failure-count. `mark_done` is the commit point (runs last). `POISON_LIMIT = 5`. |
| [`__main__.py`](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/summarizer/__main__.py) | The poll loop (`main`) + `run_once` (one pass) + `--once`/`ONCE=1` + SIGTERM handling. |

## The `run_once` path

`run_once` (in `__main__.py`) is the single pass. In order: master-switch short-circuit → list meetings → per-meeting (skip if done/poisoned; fetch transcript; min-duration guard; summarize; assemble note; write to sink; optional Vexa notes PATCH; `mark_done`). `mark_done` runs last so a crash mid-pass is harmless.

## Sinks

- **`fs`** (`OBSIDIAN_SINK=fs`, default): `write_note_fs` atomically writes to `cfg.vault_dir / path`. Syncthing mirrors the host folder into the Mac vault. No Mac-side runner.
- **`mcp`** (`OBSIDIAN_SINK=mcp`): `create_note` is a JSON-RPC `tools/call` over Streamable HTTP POST to `OBSIDIAN_MCP_URL` with a bearer token — the original Mac-local vault-as-mcp plugin sink.

## `state.json`

One record per meeting id: `{note_path, summarized_at, attempts, status}`. `status ∈ {pending, done, failed, skipped}`. Atomic write (`.tmp` → `replace`). Location: `STATE_DIR/state.json` (default `~/.local/share/vexa-summarizer`; in a container, mount a named volume at `/data/state`).

## Supporting files

- [`scripts/mint_token.sh`](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/scripts/mint_token.sh) — NAS-side helper that mints a Vexa per-user `tx` token and prints the `VEXA_SUMMARIZER_TOKEN=…` env line for the user to append to `.env`.
- [`tests/`](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/tests/) — unit tests (pytest, `asyncio_mode=auto`). No network or live Vexa; HTTP seams are monkeypatched.