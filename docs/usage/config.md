# Configuration

All vars come from `summarizer/config.py`. "Required when" is conditional — the table notes the condition; `load_config` exits with a clear `ConfigError` if a required var is missing.

## Master switch and LLM

| Variable | Required when | Default | Description |
|---|---|---|---|
| `SUMMARIZE_ENABLED` | never | `true` | Master switch. `false` short-circuits the whole pass; no other var is enforced. |
| `BRIDGE_MODE` | never | `note` | `note` (LLM summary note per meeting, this section applies) or `graph` (upload transcripts into your Vexa agent workspace, no LLM in the bridge; see [Graph mode](#graph-mode) below). |
| `AI_MODEL` | `SUMMARIZE_ENABLED=true` | `anthropic/claude-sonnet-5` | LiteLLM model id, e.g. `openai/qwen2.5:7b` for ollama, `anthropic/claude-sonnet-5` for Anthropic. |
| `AI_API_KEY` | `SUMMARIZE_ENABLED=true` (`not-needed` for local ollama) | - | API key for the LLM provider. ollama ignores it; LiteLLM requires a value, so set `not-needed`. |
| `AI_BASE_URL` | `SUMMARIZE_ENABLED=true` | - | OpenAI-compatible base URL. On the NAS: `http://ollama:11434/v1`. |
| `VEXA_API_URL` | `SUMMARIZE_ENABLED=true` | - | Vexa api-gateway URL, e.g. `http://gateway:8000` in-stack. |
| `VEXA_API_KEY` | `SUMMARIZE_ENABLED=true` | - | Per-user Vexa API token (scope `tx`), minted once via `scripts/mint_token.sh`. Not the admin token. |
| `VEXA_DATABASE_URL` | never | - | Direct-Postgres connection string, used in both modes as a fallback when the gateway's transcript route rejects a platform (notably `discord`, whose rows `discord-vexa-bridge` writes straight into `transcriptions`). Unset by default; the gateway call is tried first either way. |
| `SUMMARIZE_PLATFORMS` | never | `discord` | CSV of platforms to summarize, e.g. `discord,google_meet,zoom`. Zoom/Meet parity is this one var. |
| `MIN_TRANSCRIPT_SECONDS` | never | `30` | Skip meetings with less than this much speech; skipped, not summarized. |

## Graph mode

These vars only matter when `BRIDGE_MODE=graph`. In graph mode `AI_*`, `OBSIDIAN_*`, `INCLUDE_TRANSCRIPT`, and `VEXA_NOTES_ENABLED` are ignored entirely (the agent on the Vexa side writes the graph, not this process), and `VAULT_DIR` becomes optional: set it when the bridge can also reach the vault directly, so it fast-forwards the mirror folder after each pass; leave it unset and the mirror only advances however the agent's own push reaches it.

| Variable | Required when | Default | Description |
|---|---|---|---|
| `VEXA_VAULT_FOLDER` | never | `Vexa` | Name of the fast-forward mirror folder under `VAULT_DIR` that the bridge pulls after each pass. |
| `GRAPH_ROUTINE_NAME` | never | `meeting-to-graph` | Name of the standing Vexa routine that folds `uploads/` into the knowledge graph. Created on the bridge's first pass if a routine with that name doesn't already exist. |
| `GRAPH_ROUTINE_CRON` | never | `0 * * * *` | Cron schedule for that routine. The bridge triggers an immediate run right after it uploads a transcript, so this cron is the safety net, not the primary path. Changing it retires the routine created under the old cron on the bridge's next start (Vexa keys a routine by name and cron together, so a cron change alone would otherwise leave both running). |

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

## Webhook receiver

An event-driven alternative to the poll, supported by both `BRIDGE_MODE=note` and `BRIDGE_MODE=graph`; the poll stays the fallback either way. See [Events](https://github.com/rennf93/obsidian-vexa-bridge#events) in the README for the full picture.

| Variable | Required when | Default | Description |
|---|---|---|---|
| `WEBHOOK_ENABLED` | never | `false` | Start the event-driven receiver alongside the poll. |
| `WEBHOOK_SECRET` | `WEBHOOK_ENABLED=true` | - | Shared secret used to verify `X-Webhook-Signature` (and the legacy `Authorization: Bearer`). |
| `WEBHOOK_HOST` | never | `0.0.0.0` | Bind address for the receiver. |
| `WEBHOOK_PORT` | never | `8080` | Bind port for the receiver. |
| `WEBHOOK_PATH` | never | `/webhook` | Route the receiver listens on. |
| `WEBHOOK_PUBLIC_URL` | never | - | When set, the bridge registers this URL with Vexa (`PUT /user/webhook`) at startup. |
| `WEBHOOK_DELAY_SECONDS` | never | `20` | Wait this long after the event before fetching the transcript, since Vexa's completed transition can precede the last transcript flush. |
| `WEBHOOK_COMMIT_WAIT_SECONDS` | never | `600` | `BRIDGE_MODE=graph` only. On the event path, after triggering the fold, wait up to this long for the agent's commit to land so the push that follows picks it up right away. On timeout the bridge still pushes once (harmless if nothing is ahead) and the next poll pass covers the rest. |
| `WEBHOOK_COMMIT_POLL_SECONDS` | never | `15` | `BRIDGE_MODE=graph` only. Interval between checks while waiting for the agent's commit. |

## Behavior knobs

- `mark_done` is the only commit and runs last; a crash mid-pass leaves the meeting un-marked, so the next pass redoes it.
- The filesystem sink's "file already exists" check is the crash-recovery backstop: if the note was written but `mark_done` didn't run, the next pass sees the existing file as success and just marks done.
- A meeting that fails 5 passes (`POISON_LIMIT`) is poisoned and skipped until `state.json` is cleared.
- `DRY_RUN` runs the full pipeline (including the LLM call) without writing or marking.
