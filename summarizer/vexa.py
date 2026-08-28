"""Reads Vexa (meetings + transcripts) and writes meeting notes.

Read-only except write_notes (only fired when VEXA_NOTES_ENABLED). X-API-Key header auth.
start_time/end_time are ISO-8601 strings (epoch-second floats also accepted) -> UTC datetimes.
The list endpoint may omit platform_specific_id (the native meeting id); when it does, fall
back to GET /meetings/{id}.

GET /transcripts/{platform}/{native_meeting_id} returns a meeting object with a "segments"
array; each segment has start/end, text, speaker. (An older {"transcripts":[...]} shape with
start_time/end_time is also accepted for compat.) Discord and Vexa's mixed lane write these as
meeting-relative offsets starting near 0, but Vexa 0.12's Google Meet lane stamps them with the
bot's wall-clock Date.now() instead, i.e. absolute epoch seconds. get_transcript normalizes any
epoch-scale segment back to a relative offset before returning it, so both transcript builders
below (the gateway/segments path and _get_transcript_from_db) answer through one guard.

HTTP is split into three async seams (_http_get_json / _http_patch_json) so tests fake them
without aiohttp. Response shapes are documented in tests/test_vexa.py.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from summarizer.config import Config
    from summarizer.types import Meeting, Utterance

log = logging.getLogger("vexa-summarizer")

# Vexa 0.12's Google Meet lane stamps segments with the bot's wall-clock Date.now() (absolute
# epoch seconds) rather than an offset from meeting start. 1e9 is well below any real epoch
# second (that's the year 2001) and well above any real meeting length in seconds, so it
# cleanly tells the two units apart.
_EPOCH_SCALE_SECONDS = 1e9


class VexaError(RuntimeError):
    """Raised on Vexa HTTP failures / non-200s."""


def _parse_dt(ts: Any) -> datetime:
    """Parse a Vexa timestamp to an aware UTC datetime.

    The live api-gateway returns ISO-8601 strings (e.g. "2026-07-06T13:20:08.031143",
    sometimes with a trailing Z). Epoch-second numbers/numeric strings are also accepted
    (kept for compat / tests). Naive datetimes are assumed UTC.
    """
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=UTC)
    s = str(ts).strip()
    try:
        return datetime.fromtimestamp(float(s), tz=UTC)
    except ValueError:
        pass
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _native_id(rec: dict[str, Any]) -> str | None:
    return rec.get("platform_specific_id") or rec.get("native_meeting_id")


async def list_completed_meetings(cfg: Config, platforms: list[str]) -> list[Meeting]:
    status, data = await _http_get_json(f"{cfg.vexa_api_url}/meetings", _headers(cfg))
    if status != 200:
        raise VexaError(f"GET /meetings -> HTTP {status}")
    rows = data["meetings"] if isinstance(data, dict) and "meetings" in data else data
    if not isinstance(rows, list):
        raise VexaError(f"unexpected /meetings shape: {type(data).__name__}")
    platforms_set = set(platforms)

    from summarizer.types import Meeting

    out: list[Meeting] = []
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        if rec.get("status") != "completed":
            continue
        if rec.get("platform") not in platforms_set:
            continue
        native = _native_id(rec)
        if not native:
            # List response omitted the native id — fetch the detail record.
            dstatus, detail = await _http_get_json(f"{cfg.vexa_api_url}/meetings/{rec['id']}", _headers(cfg))
            if dstatus != 200 or not isinstance(detail, dict):
                raise VexaError(f"GET /meetings/{rec['id']} -> HTTP {dstatus}")
            native = _native_id(detail)
            if not native:
                raise VexaError(f"meeting {rec['id']} has no native_meeting_id")
            rec = {**rec, **detail}
        out.append(
            Meeting(
                id=int(rec["id"]),
                platform=str(rec["platform"]),
                native_meeting_id=str(native),
                start=_parse_dt(rec["start_time"]),
                end=_parse_dt(rec.get("end_time") or rec["start_time"]),
            )
        )
    return out


async def get_transcript(cfg: Config, meeting: Meeting) -> list[Utterance]:
    url = f"{cfg.vexa_api_url}/transcripts/{meeting.platform}/{meeting.native_meeting_id}"
    status, data = await _http_get_json(url, _headers(cfg))
    if status != 200:
        # The 0.12 api-gateway's transcript route validates `platform` against a fixed
        # enum (google_meet/zoom/teams/browser_session); platforms outside it, notably
        # discord, come back 422, even though GET /meetings lists them fine. When a
        # direct Postgres DSN is configured, fall back to it: the DB is the source of
        # truth for meetings the discord-bridge writes straight into `transcriptions`.
        # Opt-in: unset VEXA_DATABASE_URL and this behaves exactly as before.
        if not cfg.vexa_database_url:
            raise VexaError(f"GET transcripts -> HTTP {status}")
        utts = await _get_transcript_from_db(cfg, meeting)
    else:
        # Real shape (verified against the live api-gateway): a meeting object with a
        # "segments" array; each segment has start/end, text, speaker. Alternate/older
        # shape: {"transcripts":[...]} with start_time/end_time. A bare list is also
        # accepted.
        if isinstance(data, dict) and "segments" in data:
            rows = data["segments"]
        elif isinstance(data, dict) and "transcripts" in data:
            rows = data["transcripts"]
        else:
            rows = data
        if not isinstance(rows, list):
            raise VexaError(f"unexpected transcripts shape: {type(data).__name__}")

        from summarizer.types import Utterance

        utts = [
            Utterance(
                speaker=str(r.get("speaker") or r.get("speaker_name") or "Unknown"),
                start_time=float(r.get("start_time") or r.get("start") or 0.0),
                end_time=float(r.get("end_time") or r.get("end") or 0.0),
                text=str(r.get("text") or r.get("transcript_text") or ""),
            )
            for r in rows
            if isinstance(r, dict)
        ]
        utts.sort(key=lambda u: u.start_time)

    # One guard for both builders above: normalize epoch-scale segments (Vexa 0.12's Google
    # Meet lane, see the module docstring) back to meeting-relative offsets in place.
    _normalize_epoch_timestamps(meeting, utts)
    return utts


def _normalize_epoch_timestamps(meeting: Meeting, utts: list[Utterance]) -> None:
    """Rewrite epoch-scale start/end fields to meeting-relative offsets, in place, field by
    field rather than row by row.

    A transcript can mix epoch-scale fields (Vexa 0.12's Google Meet lane) with already-relative
    ones in the very same row: a row with no "start" key falls back to start_time=0.0 above
    while its end_time is still epoch-scale, and a stray already-relative row can sit next to
    real epoch rows. A single list-wide offset decision would either miss the real epoch values
    (a relative field wins min()) or wrongly shift a relative field, so each field is judged and
    adjusted on its own: only fields >= 1e9 feed the offset search and only fields >= 1e9 get
    shifted. (A NaN field fails that comparison, so it is left alone too, offset search included.)

    The offset is meeting.start's epoch, so a shifted field lands near 0; falls back to the
    smallest epoch-scale field itself (landing that one exactly at 0) when meeting.start is
    missing or would push it negative -- clock skew between the bot and Vexa's API server, for
    example. After shifting, an end_time that landed before its own start_time (e.g. only one of
    the two fields was epoch-scale to begin with) is pulled up to start_time, so no utterance can
    end up with a negative span.
    """
    epoch_fields = [v for u in utts for v in (u.start_time, u.end_time) if v >= _EPOCH_SCALE_SECONDS]
    if not epoch_fields:
        return
    earliest = min(epoch_fields)
    offset = meeting.start.timestamp() if meeting.start is not None else earliest
    if earliest - offset < 0:
        offset = earliest
    for u in utts:
        if u.start_time >= _EPOCH_SCALE_SECONDS:
            u.start_time = max(0.0, u.start_time - offset)
        if u.end_time >= _EPOCH_SCALE_SECONDS:
            u.end_time = max(0.0, u.end_time - offset)
        if u.end_time < u.start_time:
            u.end_time = u.start_time
    log.info("meeting %s: normalized epoch-scale transcript timestamps, offset=%.3f", meeting.id, offset)


async def _get_transcript_from_db(cfg: Config, meeting: Meeting) -> list[Utterance]:
    """Direct-Postgres transcript fallback. See get_transcript for why.

    Reads the same `transcriptions` rows the discord-bridge inserts (speaker, text, start/end),
    ordered by start time. No gateway round-trip. Returns start/end exactly as stored; the
    epoch-scale check lives in get_transcript, which wraps every call site.

    Any asyncpg error (server-side PostgresError or client-side InterfaceError) or OSError
    (connection refused, DNS failure, auth failure, a bad query, a closed connection)
    is re-raised as VexaError so callers see the same error type as the gateway path -- the
    original exception is kept as __cause__ for debugging.
    """
    import asyncpg

    from summarizer.types import Utterance

    try:
        conn = await asyncpg.connect(cfg.vexa_database_url)
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as exc:
        raise VexaError(f"asyncpg.connect failed for meeting {meeting.id}: {exc}") from exc
    try:
        rows = await conn.fetch(
            "SELECT speaker, text, start_time, end_time FROM transcriptions WHERE meeting_id=$1 ORDER BY start_time",
            meeting.id,
        )
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as exc:
        raise VexaError(f"transcript query failed for meeting {meeting.id}: {exc}") from exc
    finally:
        await conn.close()
    return [
        Utterance(
            speaker=str(r["speaker"] or "Unknown"),
            start_time=float(r["start_time"]),
            end_time=float(r["end_time"]),
            text=str(r["text"] or ""),
        )
        for r in rows
    ]


async def write_notes(cfg: Config, meeting: Meeting, markdown: str) -> None:
    url = f"{cfg.vexa_api_url}/meetings/{meeting.platform}/{meeting.native_meeting_id}"
    status, data = await _http_patch_json(url, _headers(cfg), {"data": {"notes": markdown}})
    if status not in (200, 204):
        raise VexaError(f"PATCH meetings -> HTTP {status}: {str(data)[:200]}")


def _headers(cfg: Config) -> dict[str, str]:
    return {"X-API-Key": cfg.vexa_api_key, "Accept": "application/json"}


async def _http_get_json(url: str, headers: dict[str, str]) -> tuple[int, Any]:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            return resp.status, await _maybe_json(resp)


async def _http_patch_json(url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
    import aiohttp

    headers = {**headers, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.patch(url, headers=headers, json=body) as resp:
            return resp.status, await _maybe_json(resp)


async def _maybe_json(resp: Any) -> Any:
    import json as _json

    text = await resp.text()
    try:
        return _json.loads(text)
    except (ValueError, _json.JSONDecodeError):
        return text
