# LLM routing (ollama)

The summarization is provider-agnostic: any OpenAI-compatible `/v1` endpoint works. The default deployment routes to a self-hosted ollama on the same host as the adapter — no cloud spend, no Mac dependency.

## The model string

LiteLLM's `AI_MODEL` is a `provider/model` id. For ollama, the provider prefix must be `openai/`, **not** `ollama/`:

| `AI_MODEL` | What LiteLLM does | Result |
|---|---|---|
| `openai/qwen2.5:7b` | Hits `AI_BASE_URL` (`/v1/chat/completions`) with the OpenAI shape | Works — ollama's OpenAI-compatible server speaks this. |
| `ollama/qwen2.5:7b` | Hits ollama's native `/api/generate` | Broken — the local raw models have no chat template; you get garbage or an empty response. |

So the rule is: prefix the model with `openai/` and point `AI_BASE_URL` at ollama's `/v1`:

```yaml
AI_MODEL: "openai/qwen2.5:7b"
AI_BASE_URL: "http://ollama:11434/v1"
AI_API_KEY: "not-needed"   # ollama ignores the key; LiteLLM requires a value
```

## Gotcha: no `:cloud` suffix against direct ollama

When routing to a local ollama `/v1` endpoint directly, use the bare `openai/<tag>` form. Do **not** add a `:cloud` suffix — that suffix is a marker some proxies/routers use for hosted models, and ollama's own `/v1` does not understand it. Pull the model on the NAS first (`ollama pull qwen2.5:7b`) and use the exact tag you pulled.

## Other providers

Any OpenAI-compatible endpoint works the same way — Anthropic, vLLM, Groq, OpenRouter, a hosted OpenAI key. Set `AI_MODEL` to the LiteLLM `provider/model` id, `AI_BASE_URL` to the endpoint, and `AI_API_KEY` to the real key. The provider dialect mirrors Vexa's dashboard AI convention so the same string works on both sides.