# Configuration

All vars come from `summarizer/config.py`. "Required when" is conditional — the table notes the condition; `load_config` exits with a clear `ConfigError` if a required var is missing.

## Master switch and LLM

| Variable | Required when | Default | Description |
|---|---|---|---|
| `SUMMARIZE_ENABLED` | never | `true` | Master switch. `false` short-circuits the whole pass; no other var is enforced. |
| `AI_MODEL` | `SUMMARIZE_ENABLED=true` | `anthropic/claude-sonnet-5` | LiteLLM model id, e.g. `openai/qwen2.5:7b` for ollama, `anthropic/claude-sonnet-5` for Anthropic. |
| `AI_API_KEY` | `SUMMARIZE_ENABLED=true` (`not-needed` for local ollama) | - | API key for the LLM provider. ollama ignores it; LiteLLM requires a value, so set `not-needed`. |
| `AI_BASE_URL` | `SUMMARIZE_ENABLED=true` | - | OpenAI-compatible base URL. On the NAS: `http://ollama:11434/v1`. |
| `VEXA_API_URL` | `SUMMARIZE_ENABLED=true` | - | Vexa api-gateway URL, e.g. `http://api-gateway:8000` in-stack. |
| `VEXA_API_KEY` | `SUMMARIZE_ENABLED=true` | - | Per-user Vexa API token (scope `tx`), minted once via `scripts/mint_token.sh`. Not the admin token. |
| `SUMMARIZE_PLATFORMS` | never | `discord` | CSV of platforms to summarize, e.g. `discord,google_meet,zoom`. Zoom/Meet parity is this one var. |
| `MIN_TRANSCRIPT_SECONDS` | never | `30` | Skip meetings with less than this much speech; skipped, not summarized. |

## Obsidian sink

| Variable | Required when | Default | Description |
|---|---|---|---|
| `OBSIDIAN_ENABLED` | never | `true` | Write an Obsidian note per meeting. |
| `OBSIDIAN_SINK` | `OBSIDIAN_ENABLED=true` | `fs` | Sink type: `fs` (filesystem, `VAULT_DIR`) or `mcp` (vault-as-mcp HTTP, `OBSIDIAN_MCP_URL`). |
| `VAULT_DIR` | `OBSIDIAN_ENABLED=true` and `OBSIDIAN_SINK=fs` | - | Host folder bind-mounted into the container (e.g. `/vault`); Syncthing mirrors it into the Mac vault. No sane default in a container. |
| `OBSIDIAN_MCP_URL` | `OBSIDIAN_ENABLED=true` and `OBSIDIAN_SINK=mcp` | `http://localhost:8765/mcp` | vault-as-mcp plugin URL (Mac-local runs). |
| `OBSIDIAN_MCP_TOKEN` | `OBSIDIAN_ENABLED=true` and `OBSIDIAN_SINK=mcp` | - | Bearer token for the vault-as-mcp plugin. |
| `OBSIDIAN_NOTE_FOLDER` | never | `Meetings` | Folder (relative to the vault root) for notes. |
| `INCLUDE_TRANSCRIPT` | never | `true` | Append the full speaker-tagged transcript to the note. |

## Vexa write-back, dry run, loop

| Variable | Required when | Default | Description |
|---|---|---|---|
| `VEXA_NOTES_ENABLED` | never | `false` | Also PATCH the summary into the Vexa meeting `notes` field. |
| `DRY_RUN` | never | `false` | Run the full pipeline (including the LLM call) but write nothing and don't mark done. Safe to repeat. |
| `STATE_DIR` | never | `~/.local/share/vexa-summarizer` | Where `state.json` lives. In a container, mount a named volume here (e.g. `/data/state`). |
| `POLL_INTERVAL_SECONDS` | never | `180` | Seconds between passes. |
| `ONCE` | never | - | `1`/`true` runs a single pass and exits — validation, `DRY_RUN` first-runs, manual reruns. |

## Behavior knobs

- `mark_done` is the only commit and runs last; a crash mid-pass leaves the meeting un-marked, so the next pass redoes it.
- The filesystem sink's "file already exists" check is the crash-recovery backstop: if the note was written but `mark_done` didn't run, the next pass sees the existing file as success and just marks done.
- A meeting that fails 5 passes (`POISON_LIMIT`) is poisoned and skipped until `state.json` is cleared.
- `DRY_RUN` runs the full pipeline (including the LLM call) without writing or marking.