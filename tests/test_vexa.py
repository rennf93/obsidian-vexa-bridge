"""Tests for summarizer.vexa — read Vexa meetings/transcripts, write notes. HTTP faked.

The Vexa api-gateway shapes (verified against the live API):
- GET /meetings -> {"meetings":[{id, platform, platform_specific_id, start_time, end_time, status}, ...]}
  (a bare list is also accepted)
- GET /transcripts/{platform}/{native_meeting_id} -> a meeting object with a "segments" array;
  each segment has {start, end, text, speaker} (second offsets). An older {"transcripts":[...]}
  shape with start_time/end_time is also accepted for compat.
- PATCH /meetings/{platform}/{native_meeting_id} body {"data":{"notes": <md>}}
start_time / end_time are ISO-8601 strings (epoch-second floats also accepted). X-API-Key header auth.
"""

import sys
import types
from datetime import UTC

import pytest

from summarizer import config, vexa


def _cfg(url="http://vexa:8056", key="vexa-key"):
    return config.Config(vexa_api_url=url, vexa_api_key=key)


async def test_list_completed_meetings_filters_status_and_platform(monkeypatch):
    payload = {
        "meetings": [
            {
                "id": 1,
                "platform": "discord",
                "platform_specific_id": "d1",
                "start_time": 1000.0,
                "end_time": 1100.0,
                "status": "completed",
            },
            {
                "id": 2,
                "platform": "discord",
                "platform_specific_id": "d2",
                "start_time": 2000.0,
                "end_time": 2100.0,
                "status": "recording",
            },  # dropped
            {
                "id": 3,
                "platform": "zoom",
                "platform_specific_id": "z1",
                "start_time": 3000.0,
                "end_time": 3100.0,
                "status": "completed",
            },  # dropped (not in platforms)
            {
                "id": 4,
                "platform": "google_meet",
                "platform_specific_id": "g1",
                "start_time": 4000.0,
                "end_time": 4100.0,
                "status": "completed",
            },
        ]
    }

    async def fake_get(url, headers):
        assert url == "http://vexa:8056/meetings"
        assert headers["X-API-Key"] == "vexa-key"
        return 200, payload

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    meetings = await vexa.list_completed_meetings(_cfg(), ["discord", "google_meet"])
    ids = [m.id for m in meetings]
    assert ids == [1, 4]
    m1 = meetings[0]
    assert m1.platform == "discord"
    assert m1.native_meeting_id == "d1"
    assert m1.start.tzinfo == UTC
    assert m1.start.timestamp() == 1000.0


async def test_list_accepts_bare_list_response(monkeypatch):
    async def fake_get(url, headers):
        return 200, [
            {
                "id": 9,
                "platform": "discord",
                "platform_specific_id": "d9",
                "start_time": 1.0,
                "end_time": 2.0,
                "status": "completed",
            }
        ]

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    meetings = await vexa.list_completed_meetings(_cfg(), ["discord"])
    assert [m.id for m in meetings] == [9]


async def test_list_parses_iso_start_time_strings(monkeypatch):
    """Live api-gateway returns start_time/end_time as ISO-8601 strings, not epoch seconds."""
    payload = {
        "meetings": [
            {
                "id": 1,
                "platform": "discord",
                "platform_specific_id": "d1",
                "start_time": "2026-07-06T13:20:08.031143",
                "end_time": "2026-07-06T14:03:27.060312",
                "status": "completed",
            }
        ]
    }

    async def fake_get(url, headers):
        return 200, payload

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    meetings = await vexa.list_completed_meetings(_cfg(), ["discord"])
    m1 = meetings[0]
    assert m1.start.tzinfo == UTC
    assert m1.start.year == 2026 and m1.start.month == 7 and m1.start.day == 6
    assert m1.start.hour == 13 and m1.start.minute == 20
    assert m1.end.minute == 3  # 14:03


async def test_list_falls_back_to_per_meeting_detail_when_native_id_missing(monkeypatch):
    """If the list response omits platform_specific_id, fetch GET /meetings/{id} for it."""
    list_payload = {
        "meetings": [{"id": 1, "platform": "discord", "start_time": 1000.0, "end_time": 1100.0, "status": "completed"}]
    }

    calls = []

    async def fake_get(url, headers):
        calls.append(url)
        if url.endswith("/meetings"):
            return 200, list_payload
        if url.endswith("/meetings/1"):
            return 200, {
                "id": 1,
                "platform": "discord",
                "platform_specific_id": "d1",
                "start_time": 1000.0,
                "end_time": 1100.0,
                "status": "completed",
            }
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    meetings = await vexa.list_completed_meetings(_cfg(), ["discord"])
    assert meetings[0].native_meeting_id == "d1"
    assert "/meetings/1" in calls[-1]


async def test_get_transcript_orders_by_start_time(monkeypatch):
    payload = {
        "transcripts": [
            {"speaker": "Renzo", "start_time": 1050.0, "end_time": 1060.0, "text": "second"},
            {"speaker": "David", "start_time": 1000.0, "end_time": 1010.0, "text": "first"},
            {"speaker": "Renzo", "start_time": 1020.0, "end_time": 1030.0, "text": "middle"},
        ]
    }

    async def fake_get(url, headers):
        assert url == "http://vexa:8056/transcripts/discord/d1"
        return 200, payload

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    from datetime import datetime as _dt

    from summarizer.types import Meeting

    m = Meeting(
        id=1,
        platform="discord",
        native_meeting_id="d1",
        start=_dt.fromtimestamp(1000.0, tz=UTC),
        end=_dt.fromtimestamp(1100.0, tz=UTC),
    )
    utts = await vexa.get_transcript(_cfg(), m)
    assert [u.text for u in utts] == ["first", "middle", "second"]
    assert utts[0].speaker == "David"


async def test_get_transcript_accepts_bare_list(monkeypatch):
    async def fake_get(url, headers):
        return 200, [{"speaker": "Renzo", "start_time": 1.0, "end_time": 2.0, "text": "hi"}]

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    from datetime import datetime as _dt

    from summarizer.types import Meeting

    m = Meeting(
        id=1,
        platform="discord",
        native_meeting_id="d1",
        start=_dt.fromtimestamp(1.0, tz=UTC),
        end=_dt.fromtimestamp(2.0, tz=UTC),
    )
    utts = await vexa.get_transcript(_cfg(), m)
    assert len(utts) == 1 and utts[0].text == "hi"


async def test_get_transcript_reads_segments_shape(monkeypatch):
    """Live api-gateway shape: a meeting object with a 'segments' array (start/end offsets)."""
    payload = {
        "id": 1,
        "platform": "discord",
        "native_meeting_id": "d1",
        "status": "completed",
        "segments": [
            {"speaker": "Renzo", "start": 18.1, "end": 19.6, "text": "I've heard, yeah."},
            {"speaker": "David Freire", "start": 1.0, "end": 3.6, "text": "Okay."},
            {"speaker": "David Freire", "start": 40.1, "end": 53.1, "text": "This is cool..."},
        ],
    }

    async def fake_get(url, headers):
        assert url == "http://vexa:8056/transcripts/discord/d1"
        return 200, payload

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    from datetime import datetime as _dt

    from summarizer.types import Meeting

    m = Meeting(
        id=1,
        platform="discord",
        native_meeting_id="d1",
        start=_dt.fromtimestamp(1000.0, tz=UTC),
        end=_dt.fromtimestamp(1100.0, tz=UTC),
    )
    utts = await vexa.get_transcript(_cfg(), m)
    assert [u.text for u in utts] == ["Okay.", "I've heard, yeah.", "This is cool..."]
    assert utts[0].speaker == "David Freire"
    assert utts[0].start_time == 1.0 and utts[0].end_time == 3.6


async def test_get_transcript_leaves_relative_timestamps_untouched(monkeypatch):
    """Discord and the mixed lane already write meeting-relative offsets; get_transcript must
    not rewrite them just because a meeting also has a distant meeting.start."""
    payload = {
        "segments": [
            {"speaker": "David Freire", "start": 1.0, "end": 3.6, "text": "Okay."},
            {"speaker": "Renzo", "start": 18.1, "end": 19.6, "text": "I've heard, yeah."},
        ],
    }

    async def fake_get(url, headers):
        return 200, payload

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    from datetime import datetime as _dt

    from summarizer.types import Meeting

    m = Meeting(
        id=1,
        platform="discord",
        native_meeting_id="d1",
        start=_dt.fromtimestamp(1_700_000_000.0, tz=UTC),
        end=_dt.fromtimestamp(1_700_003_600.0, tz=UTC),
    )
    utts = await vexa.get_transcript(_cfg(), m)
    assert (utts[0].start_time, utts[0].end_time) == (1.0, 3.6)
    assert (utts[1].start_time, utts[1].end_time) == (18.1, 19.6)


async def test_get_transcript_normalizes_epoch_scale_timestamps(monkeypatch, caplog):
    """Vexa 0.12's Google Meet lane stamps segments with the bot's wall-clock Date.now(),
    i.e. absolute epoch seconds, instead of an offset from meeting start."""
    meeting_start_epoch = 1_787_934_400.0
    payload = {
        "segments": [
            {
                "speaker": "Renzo",
                "start": meeting_start_epoch + 20.0,
                "end": meeting_start_epoch + 25.0,
                "text": "second",
            },
            {
                "speaker": "David",
                "start": meeting_start_epoch + 5.0,
                "end": meeting_start_epoch + 10.0,
                "text": "first",
            },
        ],
    }

    async def fake_get(url, headers):
        return 200, payload

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    from datetime import datetime as _dt

    from summarizer.types import Meeting

    m = Meeting(
        id=10,
        platform="google_meet",
        native_meeting_id="g10",
        start=_dt.fromtimestamp(meeting_start_epoch, tz=UTC),
        end=_dt.fromtimestamp(meeting_start_epoch + 2700.0, tz=UTC),
    )
    with caplog.at_level("INFO", logger="vexa-summarizer"):
        utts = await vexa.get_transcript(_cfg(), m)
    # relative ordering preserved, first utterance lands near 0
    assert [u.text for u in utts] == ["first", "second"]
    assert utts[0].start_time == pytest.approx(5.0)
    assert utts[0].end_time == pytest.approx(10.0)
    assert utts[1].start_time == pytest.approx(20.0)
    assert utts[1].end_time == pytest.approx(25.0)
    assert "meeting 10" in caplog.text and "normalized" in caplog.text


async def test_get_transcript_normalization_falls_back_when_meeting_start_would_go_negative(monkeypatch):
    """Clock skew: if meeting.start's epoch is *after* the earliest utterance's own epoch
    timestamp, subtracting it would push that utterance below 0 -- fall back to subtracting
    the earliest utterance's own start_time so it lands exactly at 0 instead."""
    utt_epoch = 1_787_934_400.0
    payload = {
        "segments": [
            {"speaker": "David", "start": utt_epoch, "end": utt_epoch + 5.0, "text": "first"},
            {"speaker": "Renzo", "start": utt_epoch + 15.0, "end": utt_epoch + 20.0, "text": "second"},
        ],
    }

    async def fake_get(url, headers):
        return 200, payload

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    from datetime import datetime as _dt

    from summarizer.types import Meeting

    m = Meeting(
        id=10,
        platform="google_meet",
        native_meeting_id="g10",
        start=_dt.fromtimestamp(utt_epoch + 100.0, tz=UTC),  # later than the earliest segment
        end=_dt.fromtimestamp(utt_epoch + 2700.0, tz=UTC),
    )
    utts = await vexa.get_transcript(_cfg(), m)
    assert utts[0].start_time == 0.0
    assert utts[0].end_time == pytest.approx(5.0)
    assert utts[1].start_time == pytest.approx(15.0)
    assert utts[1].end_time == pytest.approx(20.0)


async def test_get_transcript_normalizes_per_field_in_a_mixed_transcript(monkeypatch):
    """A transcript can mix an already-relative row with real epoch rows in the same list; a
    single list-wide offset decision (picking the smallest start_time overall) would let the
    relative row's small value win and leave the epoch rows unshifted. Each field must be
    judged (and shifted) on its own instead."""
    meeting_start_epoch = 1_787_935_800.0
    payload = {
        "segments": [
            {"speaker": "Stray", "start": 5.2, "end": 8.0, "text": "already relative"},
            {
                "speaker": "Renzo",
                "start": meeting_start_epoch + 38.0,
                "end": meeting_start_epoch + 40.0,
                "text": "second",
            },
            {"speaker": "David", "start": meeting_start_epoch, "end": meeting_start_epoch + 2.0, "text": "first"},
        ],
    }

    async def fake_get(url, headers):
        return 200, payload

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    from datetime import datetime as _dt

    from summarizer.types import Meeting

    m = Meeting(
        id=12,
        platform="google_meet",
        native_meeting_id="g12",
        start=_dt.fromtimestamp(meeting_start_epoch, tz=UTC),
        end=_dt.fromtimestamp(meeting_start_epoch + 2700.0, tz=UTC),
    )
    utts = await vexa.get_transcript(_cfg(), m)
    by_text = {u.text: u for u in utts}
    # epoch rows rebased against meeting.start
    assert (by_text["first"].start_time, by_text["first"].end_time) == (pytest.approx(0.0), pytest.approx(2.0))
    assert (by_text["second"].start_time, by_text["second"].end_time) == (pytest.approx(38.0), pytest.approx(40.0))
    # the stray already-relative row is untouched, byte-identical
    assert (by_text["already relative"].start_time, by_text["already relative"].end_time) == (5.2, 8.0)


async def test_get_transcript_bounds_a_row_missing_start_with_an_epoch_end(monkeypatch):
    """Regression: a segment missing 'start' falls back to start_time=0.0 above, while its
    end_time is epoch-scale. Before per-field normalization, this row's own end_time was left
    at ~1.79 billion, so duration math downstream (__main__'s min-duration and coverage gates)
    saw a multi-billion-second span instead of a real one."""
    meeting_start_epoch = 1_787_935_800.0
    payload = {
        "segments": [
            {"speaker": "David", "end": meeting_start_epoch + 35.0, "text": "malformed row"},
        ],
    }

    async def fake_get(url, headers):
        return 200, payload

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    from datetime import datetime as _dt

    from summarizer.types import Meeting

    m = Meeting(
        id=13,
        platform="google_meet",
        native_meeting_id="g13",
        start=_dt.fromtimestamp(meeting_start_epoch, tz=UTC),
        end=_dt.fromtimestamp(meeting_start_epoch + 2700.0, tz=UTC),
    )
    utts = await vexa.get_transcript(_cfg(), m)
    assert utts[0].start_time == 0.0
    assert utts[0].end_time == pytest.approx(35.0)
    assert utts[0].end_time < 10_000  # bounded and sane, nowhere near the billions


async def test_get_transcript_normalization_clamps_end_before_start(monkeypatch):
    """A row with an epoch start but no 'end' field (defaulted to 0.0, relative-scale) can land
    with end_time < start_time after per-field normalization; pull end_time up to start_time so
    no utterance ever reports a negative span."""
    meeting_start_epoch = 1_787_935_800.0
    payload = {
        "segments": [
            {"speaker": "David", "start": meeting_start_epoch + 50.0, "text": "no end field"},
        ],
    }

    async def fake_get(url, headers):
        return 200, payload

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    from datetime import datetime as _dt

    from summarizer.types import Meeting

    m = Meeting(
        id=14,
        platform="google_meet",
        native_meeting_id="g14",
        start=_dt.fromtimestamp(meeting_start_epoch, tz=UTC),
        end=_dt.fromtimestamp(meeting_start_epoch + 2700.0, tz=UTC),
    )
    utts = await vexa.get_transcript(_cfg(), m)
    assert utts[0].start_time == pytest.approx(50.0)
    assert utts[0].end_time == utts[0].start_time


async def test_write_notes_patches_with_data_notes(monkeypatch):
    captured = {}

    async def fake_patch(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, {"ok": True}

    monkeypatch.setattr(vexa, "_http_patch_json", fake_patch)
    from datetime import datetime as _dt

    from summarizer.types import Meeting

    m = Meeting(
        id=1,
        platform="discord",
        native_meeting_id="d1",
        start=_dt.fromtimestamp(1.0, tz=UTC),
        end=_dt.fromtimestamp(2.0, tz=UTC),
    )
    await vexa.write_notes(_cfg(), m, "# TL;DR\nstuff")
    assert captured["url"] == "http://vexa:8056/meetings/discord/d1"
    assert captured["headers"]["X-API-Key"] == "vexa-key"
    assert captured["body"] == {"data": {"notes": "# TL;DR\nstuff"}}


async def test_list_5xx_raises(monkeypatch):
    async def fake_get(url, headers):
        return 500, "err"

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    with pytest.raises(vexa.VexaError):
        await vexa.list_completed_meetings(_cfg(), ["discord"])


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False
        self.queries = []

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return self.rows

    async def close(self):
        self.closed = True


def _fake_asyncpg(monkeypatch, rows, dsn_seen):
    conn = _FakeConn(rows)

    async def connect(dsn):
        dsn_seen.append(dsn)
        return conn

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=connect))
    return conn


async def test_transcript_falls_back_to_db_on_422_when_dsn_configured(monkeypatch):
    async def fake_get(url, headers):
        return 422, {"detail": "unsupported platform"}

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    dsn_seen = []
    rows = [
        {"speaker": "Renzo", "text": "later", "start_time": 5.0, "end_time": 9.0},
        {"speaker": None, "text": None, "start_time": 0.0, "end_time": 2.0},
    ]
    conn = _fake_asyncpg(monkeypatch, rows, dsn_seen)
    cfg = _cfg()
    cfg.vexa_database_url = "postgresql://u:p@db/vexa"
    from datetime import datetime as _dt

    from summarizer.types import Meeting

    meeting = Meeting(
        id=7,
        platform="discord",
        native_meeting_id="d7",
        start=_dt.fromtimestamp(1.0, tz=UTC),
        end=_dt.fromtimestamp(2.0, tz=UTC),
    )
    utts = await vexa.get_transcript(cfg, meeting)
    assert dsn_seen == ["postgresql://u:p@db/vexa"]
    assert conn.closed is True
    assert conn.queries[0][1] == (7,)
    assert [(u.speaker, u.text, u.start_time) for u in utts] == [("Renzo", "later", 5.0), ("Unknown", "", 0.0)]


async def test_transcript_from_db_fallback_also_gets_normalized(monkeypatch):
    """get_transcript is the one funnel for both builders -- the DB fallback path must get the
    same epoch-scale normalization as the gateway/segments path, not a separate copy of it."""

    async def fake_get(url, headers):
        return 422, {"detail": "unsupported platform"}

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    dsn_seen: list[str] = []
    meeting_start_epoch = 1_787_934_400.0
    rows = [
        {
            "speaker": "Renzo",
            "text": "second",
            "start_time": meeting_start_epoch + 20.0,
            "end_time": meeting_start_epoch + 25.0,
        },
        {
            "speaker": "David",
            "text": "first",
            "start_time": meeting_start_epoch + 5.0,
            "end_time": meeting_start_epoch + 10.0,
        },
    ]
    _fake_asyncpg(monkeypatch, rows, dsn_seen)
    cfg = _cfg()
    cfg.vexa_database_url = "postgresql://u:p@db/vexa"
    from datetime import datetime as _dt

    from summarizer.types import Meeting

    meeting = Meeting(
        id=10,
        platform="google_meet",
        native_meeting_id="g10",
        start=_dt.fromtimestamp(meeting_start_epoch, tz=UTC),
        end=_dt.fromtimestamp(meeting_start_epoch + 2700.0, tz=UTC),
    )
    utts = await vexa.get_transcript(cfg, meeting)
    # _get_transcript_from_db doesn't re-sort (relies on the SQL's ORDER BY, unmodeled by the
    # fake connection above); normalization must not reorder either, just shift values.
    assert [u.text for u in utts] == ["second", "first"]
    assert utts[0].start_time == pytest.approx(20.0)  # "second"
    assert utts[1].start_time == pytest.approx(5.0)  # "first"


async def test_transcript_422_without_dsn_still_raises(monkeypatch):
    async def fake_get(url, headers):
        return 422, {"detail": "unsupported platform"}

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    cfg = _cfg()
    cfg.vexa_database_url = None
    from datetime import datetime as _dt

    from summarizer.types import Meeting

    meeting = Meeting(
        id=7,
        platform="discord",
        native_meeting_id="d7",
        start=_dt.fromtimestamp(1.0, tz=UTC),
        end=_dt.fromtimestamp(2.0, tz=UTC),
    )
    with pytest.raises(vexa.VexaError):
        await vexa.get_transcript(cfg, meeting)


async def test_get_transcript_from_db_wraps_connect_failure_as_vexa_error(monkeypatch):
    class _FakePostgresError(Exception):
        pass

    original = OSError("connection refused")

    async def connect(dsn):
        raise original

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        types.SimpleNamespace(connect=connect, PostgresError=_FakePostgresError, InterfaceError=_FakePostgresError),
    )
    from datetime import datetime as _dt

    from summarizer.types import Meeting

    meeting = Meeting(
        id=7,
        platform="discord",
        native_meeting_id="d7",
        start=_dt.fromtimestamp(1.0, tz=UTC),
        end=_dt.fromtimestamp(2.0, tz=UTC),
    )
    cfg = _cfg()
    cfg.vexa_database_url = "postgresql://u:p@db/vexa"
    with pytest.raises(vexa.VexaError) as exc_info:
        await vexa._get_transcript_from_db(cfg, meeting)
    assert exc_info.value.__cause__ is original
