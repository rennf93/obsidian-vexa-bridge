# Vexa version targeting

This page records which Vexa versions the adapter reads from / writes to, and where the surface is stable vs. still-moving. The short version: **reads are future-proof against Vexa 0.12; write-back of summaries to Vexa is OFF for now; the Obsidian vault is the source of truth.**

## Reads (meetings + transcripts) — stable

The adapter is a pure-REST consumer of Vexa's `api-gateway`. It reads:

- `GET /meetings` — list completed meetings, filtered by `status == "completed"` and `SUMMARIZE_PLATFORMS`.
- `GET /meetings/{id}` — fallback to fetch the native meeting id when the list response omits it.
- `GET /transcripts/{platform}/{native_meeting_id}` — the speaker-tagged segments.

These reads ride the free-form `data` field that Vexa exposes per meeting, so they are future-proof against Vexa 0.12: the meeting/transcript read shapes don't depend on any internal column the dashboard owns. New platform support is just a new value in `SUMMARIZE_PLATFORMS`.

## Writes (summary write-back) — OFF for now

`VEXA_NOTES_ENABLED` defaults to `false`. The `write_notes` path PATCHes `{"data": {"notes": markdown}}` into the Vexa meeting. Vexa 0.12 has no external write surface for `meeting.data.processed` views — there is no stable, documented way for an external integration to write back into the meeting's processed-data view. That gap is tracked by **Vexa issue #477**. Until that lands, keep `VEXA_NOTES_ENABLED=false` and treat the Obsidian vault as the source of truth for summaries.

## Graph mode: self-hosted only

`BRIDGE_MODE=graph` targets a self-hosted Vexa 0.12.x compose deployment running `agent-api`; the hosted Vexa service and the Kubernetes deployment both answer 502 on `/agent/*`, so `BRIDGE_MODE=note` is the only option against those targets. Graph mode calls four Agent API routes through the gateway's `/agent/*` proxy: `POST /agent/workspace/upload` to land each transcript in the workspace inbox, `GET /agent/routines` and `POST /agent/routines` to create the standing fold routine once, `GET /agent/workspace/git-remote-status` to check whether the agent has committed locally, and `POST /agent/workspace/push` to push those commits to the workspace's git home.

## Vault is the source of truth

Because Vexa's PATCH can't carry arbitrary metadata and the write-back surface isn't stable, the adapter owns the "which meetings have I done" set locally in `state.json` and writes the human-readable artifact to the vault. The vault note (with frontmatter, TL;DR, action items, and the raw transcript) is the durable record; Vexa stays the source of truth only for the raw meeting + transcript.
