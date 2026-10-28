# Deploy

The adapter is a long-running container that polls Vexa's `gateway` over HTTP and writes notes to a filesystem folder (or the Obsidian MCP sink). Run it on the same Docker network as `gateway`; if you use a local ollama for summarization, put it on the same network too.

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

## Docker Compose: recording-only mode (CPU hosts)

On a CPU-only box Vexa's live transcription falls behind the bot: the worker answers most chunk uploads with HTTP 503 and that audio never reaches the transcript, so a one-hour call can end with seconds of text while the recording is complete. Recording-only mode stops the bots from transcribing live and transcribes the finished recording instead, with the same model:

1. Set `TRANSCRIBE_ENABLED: "false"` on Vexa's `meeting-api` service. It is the default for every bot spawn (API, dashboard, auto-join) that does not set `transcribe_enabled` itself.
2. Add the `recording-transcriber` service from [`recording_transcriber/`](https://github.com/rennf93/obsidian-vexa-bridge/tree/master/recording_transcriber). Each pass finds completed meetings (`PLATFORMS`, ended within `LOOKBACK_HOURS`) whose master audio recordings are all final and that have no transcript rows (a meeting that already has live segments is left alone), transcribes each session's `master.webm` with faster-whisper, and inserts every segment in one transaction, with times relative to the session start. With diarization (on by default in the image) each row is one speaker's utterance labelled `Speaker 1`, `Speaker 2`, ... (numbered by first appearance, per session); without it each row is one whisper segment and `speaker` is left empty. Done and failed meetings are kept in `/state/state.json`; a meeting stops being retried after 3 attempts.
3. Set `LOW_TRANSCRIPT_GRACE_SECONDS` on the bridge so it waits for the transcript instead of marking the meeting skipped on its first poll. Matching `LOOKBACK_HOURS` (e.g. `172800` for 48 h) means a transcriber outage shorter than that still ends in a note once the service is back; past it, clear the meeting from the bridge's `state.json` to retry.

4. Optional: the transcriber diarizes recordings into `Speaker 1`, `Speaker 2`, ..., so set `SPEAKER_NAMING_ENABLED: "true"` (and `OWNER_NAME` to the recording owner) on the bridge. Vexa records no participant roster, so before summarizing the bridge asks the LLM to name the speakers from the conversation itself (people addressing each other, introducing themselves, the owner hint) and uses those names in the note, its `participants` and its filename. A speaker the conversation does not name keeps its generic label, and if the call or the reply is unusable the note is written with the generic labels.

```yaml
  recording-transcriber:
    build:
      context: ./recording-transcriber    # a copy of recording_transcriber/ from this repo
      args:
        BASE_IMAGE: vexa-transcription:cpu-v012   # the image your transcription-worker runs
    depends_on: [postgres]
    environment:
      DATABASE_URL: ${DATABASE_URL}       # Vexa's Postgres, postgresql:// scheme
      MODEL_SIZE: large-v3-turbo          # match the worker so the weights are already on disk
      COMPUTE_TYPE: int8
      CPU_THREADS: "8"
      PLATFORMS: "google_meet,zoom,teams"
      LOOKBACK_HOURS: "48"
    volumes:
      - storage-data:/storage:ro          # Vexa's object storage volume (versitygw posix root)
      - whisper-models:/app/models:ro     # the worker's model cache, read-only
      - recording-transcriber-state:/state
    networks: [vexa]
    restart: unless-stopped
```

Diarization runs on CPU after transcription, using NeMo TitaNet small speaker embeddings (sherpa-onnx) over 1.5 s windows, average-linkage cosine clustering at a 0.75 cut, tiny clusters merged and the labels smoothed. The image downloads the model at build time to `/app/diarization/titanet_small.onnx` (from the sherpa-onnx `speaker-recongition-models` release) and fails the build unless its sha256 is `ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e`. It was calibrated on ONE 55-minute 2-speaker meeting only (2 speakers found without being told, 95.9% of words attributed correctly, about 56 s per audio hour at 4 threads), so expect it to be weaker with 3 or more speakers, noisy audio, overlapping speech or a speaker under 5% of the talk. `DIARIZATION_ENABLED` (default `true`) turns it off and `DIARIZATION_MODEL` (default `/app/diarization/titanet_small.onnx`) points at another copy of the model. If it is off, the model file is missing or diarization fails, the meeting is transcribed as before (one row per segment, `speaker` empty); a diarization failure never loses the transcript.

`RECORDINGS_ROOT` (default `/storage/data/vexa`) is where the recording's `storage_path` resolves: the versitygw posix root plus the bucket name. On the reference NAS (12 cores, int8, 8 threads, nothing else transcribing) a 55-minute recording took 936 s; meetings are done one at a time, so a backlog adds up.

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
