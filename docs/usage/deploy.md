# Deploy

The adapter is a long-running container that polls Vexa's `api-gateway` over HTTP and writes notes to a filesystem folder (or the Obsidian MCP sink). Run it on the same Docker network as `gateway`; if you use a local ollama for summarization, put it on the same network too.

## Docker Compose

Add this service to your Vexa `docker-compose.yaml` (same network as `gateway`). See [`compose-snippet.yml`](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/compose-snippet.yml) for the full snippet:

```yaml
  obsidian-vexa-bridge:
    image: renzof93/obsidian-vexa-bridge:latest
    environment:
      VEXA_API_URL: http://gateway:8000
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
    depends_on: [gateway]
    volumes:
      - /volume1/vexa-obsidian-notes:/vault           # bind mount — Syncthing shares this host folder
      - obsidian-state:/data/state                    # state.json idempotency
    networks: [vexa, ollama]
    restart: unless-stopped
```

`ollama` is an external network (`ollama_default`) so the adapter can reach the NAS ollama container; `obsidian-state` is a named volume for `state.json`. Mint the tx token once on the NAS with [`scripts/mint_token.sh`](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/scripts/mint_token.sh) and append the printed `VEXA_SUMMARIZER_TOKEN=…` line to your `.env` — the script never writes the `.env` itself and never prints the full token.

## Docker Compose: graph mode

`BRIDGE_MODE=graph` uses the same image; drop the `AI_*` and `OBSIDIAN_*` vars (graph mode ignores them) and add `BRIDGE_MODE`. The `VAULT_DIR` bind mount is optional: include it only when this container should also fast-forward the vault mirror folder.

```yaml
  obsidian-vexa-bridge:
    image: renzof93/obsidian-vexa-bridge:latest
    environment:
      BRIDGE_MODE: "graph"
      VEXA_API_URL: http://gateway:8000
      VEXA_API_KEY: "${VEXA_SUMMARIZER_TOKEN}"        # per-user tx token, minted once
      SUMMARIZE_PLATFORMS: "discord,google_meet,zoom"
      VAULT_DIR: "/vault"                             # optional; omit if this container can't reach the vault
      POLL_INTERVAL_SECONDS: "180"
      STATE_DIR: "/data/state"
    depends_on: [gateway]
    volumes:
      - /volume1/vexa-vault:/vault                    # optional bind mount, only needed with VAULT_DIR set
      - obsidian-state:/data/state                    # state.json idempotency
    networks: [vexa]
    restart: unless-stopped
```

This requires a self-hosted Vexa 0.12.x compose deployment with `agent-api` reachable at `VEXA_API_URL` and a workspace already attached to it; see the graph mode setup walkthrough in the [README](https://github.com/rennf93/obsidian-vexa-bridge#graph-mode-setup). The full graph mode env var list is on [Configuration](config.md#graph-mode).

## Docker Compose: webhook receiver (optional)

Add these vars to either compose snippet above to also receive Vexa's `meeting.completed` webhook instead of relying solely on the poll. When Vexa runs on the same compose network, `WEBHOOK_PUBLIC_URL` can point straight at the service name and no host port needs publishing: Vexa reaches the container over the internal network the same way `gateway` does.

```yaml
  obsidian-vexa-bridge:
    image: renzof93/obsidian-vexa-bridge:latest
    environment:
      # ...the rest of the note-mode or graph-mode vars above, plus:
      WEBHOOK_ENABLED: "true"
      WEBHOOK_SECRET: "${OBSIDIAN_WEBHOOK_SECRET}"
      WEBHOOK_PUBLIC_URL: "http://obsidian-vexa-bridge:8080/webhook"  # in-network Vexa
    networks: [vexa]
    restart: unless-stopped
```

No `ports:` entry is required for this: the receiver only needs to be reachable from Vexa's own containers on the `vexa` network, not from the host. See [Events](https://github.com/rennf93/obsidian-vexa-bridge#events) in the README and the [webhook receiver vars](config.md#webhook-receiver) on Configuration.

## Environment

| Variable | Required when | Default | Description |
|---|---|---|---|
| `VEXA_API_URL` | `SUMMARIZE_ENABLED=true` | - | Vexa api-gateway base URL, e.g. `http://gateway:8000` in-stack. |
| `VEXA_API_KEY` | `SUMMARIZE_ENABLED=true` | - | Per-user Vexa API token (scope `tx`), minted once via `scripts/mint_token.sh`. Not the admin token. Exposed in compose as `VEXA_SUMMARIZER_TOKEN`. |
| `POLL_INTERVAL_SECONDS` | never | `180` | Seconds between passes. |
| `AI_MODEL` | `SUMMARIZE_ENABLED=true` | `anthropic/claude-sonnet-5` | LiteLLM model id, e.g. `openai/qwen2.5:7b` for ollama. See [LLM routing](llm-routing.md). |
| `AI_BASE_URL` | `SUMMARIZE_ENABLED=true` | - | OpenAI-compatible base URL. On the NAS: `http://ollama:11434/v1`. |
| `AI_API_KEY` | `SUMMARIZE_ENABLED=true` (`not-needed` for local ollama) | - | API key for the LLM provider. ollama ignores it; LiteLLM requires a value, so set `not-needed`. |
| `OBSIDIAN_SINK` | `OBSIDIAN_ENABLED=true` | `fs` | `fs` (filesystem, `VAULT_DIR`) or `mcp` (vault-as-mcp HTTP, `OBSIDIAN_MCP_URL`). |
| `VAULT_DIR` | `OBSIDIAN_ENABLED=true` and `OBSIDIAN_SINK=fs` | - | Host folder bind-mounted into the container (e.g. `/vault`); Syncthing mirrors it into the Mac vault. |
| `ONCE` | never | - | `1`/`true` runs a single pass and exits — validation, `DRY_RUN` first-runs, manual reruns. |
| `DRY_RUN` | never | `false` | Run the full pipeline (including the LLM call) but write nothing and don't mark done. Safe to repeat. |

The full env surface (including the rarely-touched knobs) is on the [Configuration](config.md) page.

## Validation: ONCE + DRY_RUN

Before letting the loop run, validate the wiring end-to-end without writing anything:

```bash
# One pass, no writes, no mark_done — safe to repeat.
docker run --rm \
  -e VEXA_API_URL=http://gateway:8000 \
  -e VEXA_API_KEY="$VEXA_SUMMARIZER_TOKEN" \
  -e AI_MODEL="openai/qwen2.5:7b" \
  -e AI_BASE_URL="http://ollama:11434/v1" \
  -e AI_API_KEY="not-needed" \
  -e DRY_RUN=1 \
  -e ONCE=1 \
  renzof93/obsidian-vexa-bridge:latest
```

`DRY_RUN=1` runs the full pipeline (including the LLM call) but writes no note and does not `mark_done`, so the meeting is retried on the next real pass. `ONCE=1` exits after one pass. Watch the logs for `pass complete: N summarized, …`.

## Syncthing into the Mac vault

Share the NAS host folder bound to `/vault` (e.g. `/volume1/vexa-obsidian-notes`) with a folder inside your Mac Obsidian vault (`Renn's Vault/Meetings`). Notes written on the NAS appear in Obsidian on the Mac with no Mac-side runner required.
