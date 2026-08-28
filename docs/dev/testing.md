# Testing

The suite is unit tests with pytest (`asyncio_mode=auto`). No network and no live Vexa — the HTTP seams in `vexa.py` / `obsidian.py` / `llm.py` are monkeypatched, so tests run hermetically.

## Run the suite

```bash
make install     # uv sync --extra dev
make test        # uv run pytest -v
```

The full gate set (kept green in CI):

```bash
make lint        # ruff check + ruff format --check
make typecheck   # mypy summarizer (must stay clean — prefer narrowing over # type: ignore)
make security    # bandit -r summarizer -ll
make check-all   # lint + typecheck + security + test
make pre-commit  # pre-commit run --all-files (ruff, ruff-format, mypy, bandit)
```

## What the tests cover

| File | Covers |
|---|---|
| `tests/test_config.py` | `load_config` required/optional vars, master switch, sink selection. |
| `tests/test_vexa.py` | Meeting list + transcript parsing (incl. the fallback detail fetch + alternate response shapes). |
| `tests/test_llm.py` | `build_prompt` / `parse_response` (pure) + `summarize` via an injected `call_api` seam. |
| `tests/test_obsidian.py` | Note assembly, path derivation, both sinks' "file already exists" idempotent backstop. |
| `tests/test_state.py` | `mark_done` / `record_failure` / poison at `POISON_LIMIT` / atomic save. |
| `tests/test_run.py` / `tests/test_main_loop.py` | The `run_once` pass + the loop's SIGTERM behavior. |

## `--once` / `DRY_RUN` validation flow

Before letting the loop run against a real Vexa, validate the wiring end-to-end:

```bash
# Single pass, full pipeline incl. the LLM call, no writes, no mark_done.
docker run --rm \
  -e VEXA_API_URL=http://gateway:8000 \
  -e VEXA_API_KEY="$VEXA_SUMMARIZER_TOKEN" \
  -e AI_MODEL="openai/qwen2.5:7b" \
  -e AI_BASE_URL="http://ollama:11434/v1" \
  -e AI_API_KEY="not-needed" \
  -e DRY_RUN=1 -e ONCE=1 \
  renzof93/obsidian-vexa-bridge:latest
```

Watch for `pass complete: N summarized, N skipped, N failed, N idle`. `DRY_RUN` means the meeting is retried on the next real pass (nothing was marked done).
