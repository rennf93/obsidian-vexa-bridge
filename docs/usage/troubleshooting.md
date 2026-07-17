# Troubleshooting

## A meeting keeps failing every pass

A meeting that fails `POISON_LIMIT` (5) passes is marked `failed` in `state.json` and skipped on every subsequent pass. To retry it, clear its entry:

```bash
# state.json is at $STATE_DIR/state.json (in a container: /data/state/state.json)
# Remove the offending meeting_id key, or delete the file to retry everything.
```

Look at the adapter logs first — the `meeting <id> failed: <exc>` line names the cause (Vexa 5xx, LLM error, sink write failure). Common causes: a transient Vexa restart, an ollama model that isn't pulled, or a `VAULT_DIR` that isn't bind-mounted.

## The same meeting summarized twice

It shouldn't — `mark_done` is the only commit and runs last. If you see a duplicate note in the vault, it means a crash happened between `write_note_fs` and `mark_done`. The filesystem sink's "file already exists" check is the backstop: the next pass sees the existing file as success and just marks done, so you get **one** note, not two. If you genuinely have two identical-path notes, the path derivation changed (participants or start time shifted) — check `note_path` in `summarizer/obsidian.py`.

## `state.json` lost after a redeploy

Mount `STATE_DIR` on a **named volume** (`obsidian-state:/data/state`), not a bind mount that gets wiped on `compose up --d`. Without it, every redeploy re-summarizes every meeting. The `compose-snippet.yml` uses a named volume for exactly this reason.

## ollama unreachable from the adapter

The adapter container must be on the same Docker network as ollama. In the compose snippet, `networks: [vexa, ollama]` attaches it to `ollama_default` (declared as `external: true`). If you see `ClientConnectorError` / `NameResolutionError` against `ollama`, check:

- `docker network inspect ollama_default` includes the adapter container.
- `AI_BASE_URL` is `http://ollama:11434/v1` (the service name `ollama`, not `localhost` — inside the container `localhost` is the adapter itself).
- The model is pulled on the NAS: `docker exec ollama ollama list`.

## `config error: <VAR> is required`

`load_config` exits with a clear `ConfigError` naming the missing var. The "Required when" column in [Configuration](config.md) tells you the condition that makes a var required. `SUMMARIZE_ENABLED=false` short-circuits the whole pass and skips all enforcement — handy for a dry smoke test of the image.

## `--once` / `DRY_RUN` for validation

`ONCE=1 DRY_RUN=1` runs one full pass (including the LLM call) and writes nothing. Use it after any config change to confirm the wiring before letting the loop run.
