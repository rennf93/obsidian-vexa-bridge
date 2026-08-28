# obsidian-vexa-bridge

[![CI](https://github.com/rennf93/obsidian-vexa-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/rennf93/obsidian-vexa-bridge/actions/workflows/ci.yml)
[![Docs](https://github.com/rennf93/obsidian-vexa-bridge/actions/workflows/docs.yml/badge.svg)](https://rennf93.github.io/obsidian-vexa-bridge/)
[![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/release/python-3110/)

Turn completed [Vexa](https://github.com/Vexa-ai/vexa) meetings into structured Obsidian notes — speaker-tagged, summarized by an LLM, and written to a folder that syncs into your vault. One service covers every platform Vexa records (Discord, Google Meet, Zoom, Teams).

This is the **sink** half of an in/out adapter pair around Vexa:

- **[`discord-vexa-bridge`](https://github.com/rennf93/discord-vexa-bridge)** — the *source* adapter: Discord voice → Vexa transcripts (DAVE/E2EE receive path, writes into Vexa's Postgres).
- **`obsidian-vexa-bridge`** (this repo) — the *sink* adapter: Vexa transcripts → Obsidian notes. It polls Vexa for completed meetings across all platforms, summarizes each through an OpenAI-compatible LLM, writes a structured markdown note to a filesystem sink synced into the vault, and optionally writes the summary back to the Vexa meeting's `notes` field.

Same pattern, opposite direction, separate repo + image + release cadence.

> **Which Vexa version?** This adapter is **REST-only** and rides Vexa's sealed API surface, so
> reads are future-proof against Vexa **0.12** (it consumes the free-form meeting `data` field).
> Writing summaries back into Vexa is **OFF by default** — Vexa 0.12 has no external write surface
> for processed views yet; that surface is tracked for the **0.12.x** line
> ([Vexa #477](https://github.com/Vexa-ai/vexa/issues/477)). Until it lands, the Obsidian vault is
> the source of truth. See [Which Vexa version?](docs/usage/vexa-version-targeting.md) for details.

---

## Two modes

`BRIDGE_MODE=note` (default): one summary note per completed meeting, written by an LLM call, into your vault (filesystem or MCP sink). Works against any Vexa 0.12 deployment, hosted included.

`BRIDGE_MODE=graph`: no LLM in the bridge. Each transcript is uploaded into your Vexa agent workspace, a standing Vexa routine folds it into an Open Knowledge Format knowledge graph (people, companies, projects, meetings, decisions, topics, all `[[wikilinked]]`), the bridge pushes the agent's commits to the repo's home and fast-forwards the `Vexa/` folder of your vault. Requires a self-hosted Vexa 0.12 with `agent-api` (the hosted service and Kubernetes answer 502 on `/agent/*`) and a Claude Code credential on that stack.

### Graph mode setup

1. Create a private git repo on an HTTPS host (GitHub works; Vexa speaks HTTPS with a personal access token only) and clone it locally.
2. `python -m summarizer init-workspace <clone> --repo-url https://github.com/<you>/<repo>.git`; commit and push the template.
3. Save the token and attach the repo as your workspace with the two `curl` calls the command prints (`/agent/workspace/git-token`, `/agent/workspace/swap`), then check `/agent/workspace/tree` shows `CLAUDE.md` at the root.
4. Clone the same repo into your vault as `<vault>/Vexa` (or the name you set in `VEXA_VAULT_FOLDER`; the Dataview dashboards assume `Vexa`).
5. Run the bridge with `BRIDGE_MODE=graph`, `VEXA_API_URL`, `VEXA_API_KEY`, and `VAULT_DIR` when the bridge runs where the vault lives. On its first pass it creates the routine `meeting-to-graph`; the bridge triggers an immediate run right after it uploads a transcript, and the routine's cron (`GRAPH_ROUTINE_CRON`, default hourly) is the safety net.

### The vault folder rule

`<vault>/Vexa` is a read-only mirror: read it and link into it from the rest of your vault, but change it by talking to the agent, not by editing files. The bridge pulls it with `git pull --ff-only` when it can reach it; when the bridge runs on another machine, keep the folder pulled with the Obsidian Git plugin or any timer (`cron`, `systemd`, `launchd`) running the same command.

---

## How it works

```
Vexa (completed meetings, any platform)
  -> poll loop (POLL_INTERVAL_SECONDS) lists completed meetings per platform
  -> skip already-done / poisoned (state.json), below MIN_TRANSCRIPT_SECONDS, or below
     MIN_TRANSCRIPT_COVERAGE for calls long enough to judge
  -> fetch transcript (speaker-tagged segments)
  -> summarize via OpenAI-compatible LLM (AI_MODEL / AI_BASE_URL / AI_API_KEY)
  -> assemble note (frontmatter + TL;DR + key points + action items + transcript)
  -> write to filesystem sink (OBSIDIAN_SINK=fs, VAULT_DIR) -> Syncthing -> Obsidian vault
     OR write via Obsidian MCP sink (OBSIDIAN_SINK=mcp, OBSIDIAN_MCP_URL)
  -> optionally PATCH Vexa meeting notes (VEXA_NOTES_ENABLED)
  -> mark_done in state.json (the only commit; crash mid-pass is harmless)
```

The summarization is provider-agnostic: any OpenAI-compatible `/v1` endpoint works. The default deployment routes to a self-hosted ollama on the same host as the adapter — no cloud spend, no Mac dependency.

---

## Events

Alongside the poll, the bridge can receive Vexa's signed `meeting.completed` webhook and process that meeting immediately instead of waiting for the next tick. The poll stays the fallback either way: it is what eventually processes a meeting whose event never arrived, or arrived before its transcript was fully flushed (`WEBHOOK_DELAY_SECONDS` gives Vexa a moment to finish writing before the bridge fetches the transcript; a still-short transcript at that point is left for the poll instead of being marked skipped). Both `BRIDGE_MODE=note` and `BRIDGE_MODE=graph` support the webhook. In graph mode, once a webhook upload triggers the fold, the bridge waits for the agent's commit and pushes it right away, instead of leaving it for the next poll pass.

Set `WEBHOOK_ENABLED=true` and `WEBHOOK_SECRET` to start the receiver (an aiohttp server on `WEBHOOK_HOST:WEBHOOK_PORT`, default `0.0.0.0:8080`, at `WEBHOOK_PATH`, default `/webhook`); set `WEBHOOK_PUBLIC_URL` to have the bridge register that URL with Vexa automatically at startup (`PUT /user/webhook`).

| Variable | Required when | Default | Description |
|---|---|---|---|
| `WEBHOOK_ENABLED` | never | `false` | Start the event-driven receiver alongside the poll. |
| `WEBHOOK_SECRET` | `WEBHOOK_ENABLED=true` | - | Shared secret used to verify `X-Webhook-Signature` (and the legacy `Authorization: Bearer`). |
| `WEBHOOK_HOST` | never | `0.0.0.0` | Bind address for the receiver. |
| `WEBHOOK_PORT` | never | `8080` | Bind port for the receiver. |
| `WEBHOOK_PATH` | never | `/webhook` | Route the receiver listens on. |
| `WEBHOOK_PUBLIC_URL` | never | - | When set, the bridge registers this URL with Vexa (`PUT /user/webhook`) at startup. |
| `WEBHOOK_DELAY_SECONDS` | never | `20` | Wait this long after the event before fetching the transcript. |
| `WEBHOOK_COMMIT_WAIT_SECONDS` | never | `600` | `BRIDGE_MODE=graph` only. Wait up to this long for the agent's commit before giving up and letting the next poll pass push it. |
| `WEBHOOK_COMMIT_POLL_SECONDS` | never | `15` | `BRIDGE_MODE=graph` only. Interval between checks while waiting for the agent's commit. |

Discord meetings that [`discord-vexa-bridge`](https://github.com/rennf93/discord-vexa-bridge) writes straight into Vexa's Postgres don't go through Vexa's own webhook delivery; that sibling bridge emits the same envelope shape itself for the meetings it writes, so this receiver accepts events from either source.

---

## Run model

The container is a **long-running poll loop**, not a cron job. On startup it loads `summarizer/config.py` from env, then repeats: one pass (`run_once`) → sleep `POLL_INTERVAL_SECONDS` (default 180) → repeat. A `SIGTERM` (`docker stop`) cancels the loop cleanly between passes — no mid-pass corruption, because `mark_done` is the only commit and it runs last. `--once` (or `ONCE=1`) runs a single pass and exits, for validation, `DRY_RUN` first-runs, and manual reruns.

---

## Configuration

All vars come from `summarizer/config.py`. "Required when" is conditional — the table notes the condition; `load_config` exits with a clear `ConfigError` if a required var is missing.

| Variable | Required when | Default | Description |
|---|---|---|---|
| `SUMMARIZE_ENABLED` | never | `true` | Master switch. `false` short-circuits the whole pass; no other var is enforced. |
| `AI_MODEL` | `SUMMARIZE_ENABLED=true` | `anthropic/claude-sonnet-5` | LiteLLM model id, e.g. `openai/qwen2.5:7b` for ollama, `anthropic/claude-sonnet-5` for Anthropic. |
| `AI_API_KEY` | `SUMMARIZE_ENABLED=true` (use `not-needed` for local ollama) | - | API key for the LLM provider. ollama ignores it; LiteLLM requires a value, so set `not-needed`. |
| `AI_BASE_URL` | `SUMMARIZE_ENABLED=true` | - | OpenAI-compatible base URL. On the NAS: `http://ollama:11434/v1`. |
| `VEXA_API_URL` | `SUMMARIZE_ENABLED=true` | - | Vexa api-gateway URL, e.g. `http://api-gateway:8000` in-stack. |
| `VEXA_API_KEY` | `SUMMARIZE_ENABLED=true` | - | Per-user Vexa API token (scope `tx`), minted once via `scripts/mint_token.sh`. Not the admin token. |
| `SUMMARIZE_PLATFORMS` | never | `discord` | CSV of platforms to summarize, e.g. `discord,google_meet,zoom`. Zoom/Meet parity is this one var. |
| `MIN_TRANSCRIPT_SECONDS` | never | `30` | Skip meetings with less than this much speech; skipped, not summarized. |
| `MIN_TRANSCRIPT_COVERAGE` | never | `0.05` | Skip meetings (at least 300s wall clock) where speech seconds / wall-clock seconds falls below this ratio, e.g. Vexa's transcription worker rejected most of the audio. `0` disables the gate. |
| `OBSIDIAN_ENABLED` | never | `true` | Write an Obsidian note per meeting. |
| `OBSIDIAN_SINK` | `OBSIDIAN_ENABLED=true` | `fs` | Sink type: `fs` (filesystem, `VAULT_DIR`) or `mcp` (vault-as-mcp HTTP, `OBSIDIAN_MCP_URL`). |
| `VAULT_DIR` | `OBSIDIAN_ENABLED=true` and `OBSIDIAN_SINK=fs` | - | Host folder bind-mounted into the container (e.g. `/vault`); Syncthing mirrors it into the Mac vault. No sane default in a container. |
| `OBSIDIAN_MCP_URL` | `OBSIDIAN_ENABLED=true` and `OBSIDIAN_SINK=mcp` | `http://localhost:8765/mcp` | vault-as-mcp plugin URL (Mac-local runs). |
| `OBSIDIAN_MCP_TOKEN` | `OBSIDIAN_ENABLED=true` and `OBSIDIAN_SINK=mcp` | - | Bearer token for the vault-as-mcp plugin. |
| `OBSIDIAN_NOTE_FOLDER` | never | `Meetings` | Folder (relative to the vault root) for notes. |
| `INCLUDE_TRANSCRIPT` | never | `true` | Append the full speaker-tagged transcript to the note. |
| `VEXA_NOTES_ENABLED` | never | `false` | Also PATCH the summary into the Vexa meeting `notes` field. |
| `DRY_RUN` | never | `false` | Run the full pipeline (including the LLM call) but write nothing and don't mark done. Safe to repeat. |
| `STATE_DIR` | never | `~/.local/share/vexa-summarizer` | Where `state.json` lives. In a container, mount a named volume here (e.g. `/data/state`). |
| `POLL_INTERVAL_SECONDS` | never | `180` | Seconds between passes. |

---

## Deploy

Add this service to your Vexa `docker-compose.yaml` (same network as `api-gateway`). See [`compose-snippet.yml`](compose-snippet.yml) for the full snippet:

```yaml
  obsidian-vexa-bridge:
    image: renzof93/obsidian-vexa-bridge:latest
    environment:
      VEXA_API_URL: http://api-gateway:8000
      VEXA_API_KEY: "${VEXA_SUMMARIZER_TOKEN}"        # per-user tx token, minted once
      SUMMARIZE_PLATFORMS: "discord,google_meet,zoom"
      AI_MODEL: "openai/qwen2.5:7b"                   # whatever you `ollama pull`
      AI_BASE_URL: "http://ollama:11434/v1"           # NAS ollama container
      AI_API_KEY: "not-needed"                        # ollama ignores it; litellm requires a value
      OBSIDIAN_ENABLED: "true"
      OBSIDIAN_SINK: "fs"
      VAULT_DIR: "/vault"
      OBSIDIAN_NOTE_FOLDER: "Meetings"
      INCLUDE_TRANSCRIPT: "true"
      POLL_INTERVAL_SECONDS: "180"
      STATE_DIR: "/data/state"
    depends_on: [api-gateway]
    volumes:
      - /volume1/vexa-obsidian-notes:/vault           # bind mount — Syncthing shares this host folder
      - obsidian-state:/data/state                    # state.json idempotency
    networks: [vexa, ollama]
    restart: unless-stopped
```

`ollama` is an external network (`ollama_default`) so the adapter can reach the NAS ollama container; `obsidian-state` is a named volume for `state.json`. Mint the tx token once on the NAS with `scripts/mint_token.sh` and append the printed `VEXA_SUMMARIZER_TOKEN=…` line to your `.env` — the script never writes the `.env` itself and never prints the full token.

**Syncthing**: share the NAS host folder bound to `/vault` (e.g. `/volume1/vexa-obsidian-notes`) with a folder inside your Mac Obsidian vault (`Renn's Vault/Meetings`). Notes written on the NAS appear in Obsidian on the Mac with no Mac-side runner required.

---

## Idempotency & poison model

`state.json` is the source of truth — Vexa's PATCH can't carry arbitrary metadata, so the adapter owns the "which meetings have I done" set. `mark_done` is the only write that commits a meeting as summarized, and it runs last; a crash mid-pass leaves the meeting un-marked, so the next pass simply redoes it. The filesystem sink's "file already exists" check is the crash recovery backstop: if the note was written but `mark_done` didn't run, the next pass sees the existing file as success and just marks done. A meeting that fails 5 passes is poisoned and skipped until `state.json` is cleared. `DRY_RUN` runs the full pipeline (including the LLM call) without writing or marking.

---

## Development

```bash
uv sync --extra dev     # install runtime + dev deps
make test               # run the unit suite
make lint               # ruff lint + format check
make fix                # auto-fix
make typecheck          # mypy (must stay clean)
make build              # build the Docker image
```

See [CONTRIBUTING.md](CONTRIBUTING.md). The service is the `summarizer/` package; run it with `python -m summarizer` (or `--once` for a single pass).

## Documentation

Full docs are published at <https://rennf93.github.io/obsidian-vexa-bridge/> (source in [`docs/`](docs/) + [`mkdocs.yml`](mkdocs.yml)).

## License

Open-source use is licensed under [AGPL-3.0-or-later](LICENSE). A commercial license is available for entities that cannot comply with AGPL §13 (closed SaaS / proprietary embedding) — see [LICENSE-COMMERCIAL.md](LICENSE-COMMERCIAL.md). Not affiliated with Obsidian or Vexa.
