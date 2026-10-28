"""recording_transcriber's SQL against a real Postgres (CI runs one; locally set TEST_DATABASE_URL).

The fixtures mirror Vexa 0.12's schema and the meeting.data shapes that broke or could break the
candidate query: a JSON null `recordings` or `media_files`, a second session still finalizing,
a meeting that already has live segments, another platform, and a meeting outside the lookback.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

SCHEMA = """
CREATE TABLE meetings (id int PRIMARY KEY, platform varchar, status varchar, end_time timestamp, data jsonb);
CREATE TABLE meeting_sessions (id serial, meeting_id int, session_uid varchar, session_start_time timestamptz);
CREATE TABLE transcriptions (
    id serial, meeting_id int NOT NULL, start_time double precision NOT NULL, end_time double precision NOT NULL,
    text text NOT NULL, speaker varchar(255), language varchar(10), created_at timestamp,
    session_uid varchar, segment_id varchar);
CREATE UNIQUE INDEX ON transcriptions (meeting_id, segment_id) WHERE segment_id IS NOT NULL;
"""


def _recording(session_uid: str, created_at: str, final: bool = True) -> dict:
    path = f"recordings/1/1/{session_uid}/audio/master.webm"
    return {
        "session_uid": session_uid,
        "created_at": created_at,
        "media_files": [{"type": "audio", "is_final": final, "storage_path": path}],
    }


MEETINGS = [
    # id, platform, ended hours ago, data, sessions (uid, start)
    (
        1,
        "google_meet",
        1,
        {"recordings": [_recording("s1", "2026-10-28T17:32:08Z")]},
        [("s1", "2026-10-28T17:31:17.88Z")],
    ),
    (2, "google_meet", 1, {"recordings": None}, [("s2", "2026-10-28T17:31:17Z")]),
    (
        3,
        "google_meet",
        1,
        {"recordings": [{"session_uid": "s3", "media_files": None}]},
        [("s3", "2026-10-28T17:31:17Z")],
    ),
    (
        4,
        "google_meet",
        1,
        {"recordings": [_recording("s4a", "2026-10-28T17:32:08Z"), _recording("s4b", "2026-10-28T17:40:00Z", False)]},
        [("s4a", "2026-10-28T17:31:17Z"), ("s4b", "2026-10-28T17:39:00Z")],
    ),
    (5, "google_meet", 1, {"recordings": [_recording("s5", "2026-10-28T17:32:08Z")]}, [("s5", "2026-10-28T17:31:17Z")]),
    (
        6,
        "zoom",
        1,
        {"recordings": [_recording("s6a", "2026-10-28T17:32:08Z"), _recording("s6b", "2026-10-28T17:40:00Z")]},
        [("s6a", "2026-10-28T17:31:17Z"), ("s6b", "2026-10-28T17:39:00Z")],
    ),
    (7, "discord", 1, {"recordings": [_recording("s7", "2026-10-28T17:32:08Z")]}, [("s7", "2026-10-28T17:31:17Z")]),
    (
        8,
        "google_meet",
        72,
        {"recordings": [_recording("s8", "2026-10-25T17:32:08Z")]},
        [("s8", "2026-10-25T17:31:17Z")],
    ),
]


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=object))
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    module = importlib.reload(importlib.import_module("recording_transcriber.transcribe_recordings"))
    schema = f"rt_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    conn = psycopg.connect(TEST_DATABASE_URL, options=f"-c search_path={schema}", autocommit=True)
    conn.execute(SCHEMA)
    for mid, platform, hours_ago, data, sessions in MEETINGS:
        conn.execute(
            "INSERT INTO meetings VALUES "
            "(%s, %s, 'completed', (now() AT TIME ZONE 'utc') - %s * interval '1 hour', %s)",
            (mid, platform, hours_ago, psycopg.types.json.Jsonb(data)),
        )
        for uid, start in sessions:
            conn.execute(
                "INSERT INTO meeting_sessions (meeting_id, session_uid, session_start_time) VALUES (%s, %s, %s)",
                (mid, uid, start),
            )
    conn.execute(
        "INSERT INTO transcriptions (meeting_id, start_time, end_time, text, segment_id) VALUES (5, 0, 1, 'live', 'x')"
    )
    yield module, conn
    conn.close()
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")


def _candidates(module, conn, meeting=None):
    params = {"platforms": module.PLATFORMS, "lookback": 48.0, "meeting": meeting}
    return conn.execute(module.CANDIDATES_SQL, params).fetchall()


def test_candidates_skip_bad_json_unfinished_transcribed_other_platform_and_old(db):
    module, conn = db
    rows = _candidates(module, conn)
    assert [(r[0], r[1]) for r in rows] == [(1, "s1"), (6, "s6a"), (6, "s6b")]
    assert round((rows[0][3] - rows[0][2]).total_seconds(), 2) == 50.12
    assert rows[0][4] == "recordings/1/1/s1/audio/master.webm"


def test_meeting_flag_ignores_the_lookback_but_not_the_other_rules(db):
    module, conn = db
    assert [r[0] for r in _candidates(module, conn, meeting=8)] == [8]
    assert _candidates(module, conn, meeting=2) == []
    assert _candidates(module, conn, meeting=5) == []


def test_insert_is_idempotent_and_takes_the_meeting_out_of_the_candidates(db):
    module, conn = db
    row = (1, 51.0, 52.5, "hello", "en", "s1", "rec:s1:0")
    with conn.transaction():
        conn.cursor().executemany(module.INSERT_SQL, [row, row])
    stored = conn.execute(
        "SELECT start_time, text, speaker, created_at IS NOT NULL FROM transcriptions WHERE meeting_id = 1"
    )
    assert stored.fetchall() == [(51.0, "hello", None, True)]
    assert [r[0] for r in _candidates(module, conn)] == [6, 6]
