# Sink adapter: summarize completed Vexa meetings into Obsidian notes.
# Pinned to 3.11 for parity with discord-vexa-bridge (the source adapter) —
# not load-bearing here (no audioop, no Opus decode on the sink side), but
# keeps the adapter pair on the same Python so ops stay symmetric.
# Do NOT bump past 3.11 (dependabot is configured to skip python minor/major bumps).
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY summarizer ./summarizer
CMD ["python", "-m", "summarizer"]
