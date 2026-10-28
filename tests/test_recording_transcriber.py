"""recording_transcriber with psycopg and faster_whisper stubbed: no database, no model."""

from __future__ import annotations

import datetime as dt
import importlib
import sys
import types

import pytest

SESSION_START = dt.datetime(2026, 10, 28, 17, 31, 17, tzinfo=dt.UTC)
CANDIDATE = (14, "uid", SESSION_START, SESSION_START + dt.timedelta(seconds=50), "rec/master.webm")


class FakeConn:
    def __init__(self, inserted: list[tuple]) -> None:
        self.inserted = inserted

    def __enter__(self) -> FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: dict) -> types.SimpleNamespace:
        return types.SimpleNamespace(fetchall=lambda: [CANDIDATE])

    def transaction(self) -> FakeConn:
        return self

    def cursor(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(executemany=lambda sql, rows: self.inserted.extend(rows))


class FakeModel:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def transcribe(self, path: str, **kwargs: object) -> tuple:
        if self.fail:
            raise RuntimeError("decode failed")
        segments = [
            types.SimpleNamespace(start=1.0, end=2.5, text=" hello "),
            types.SimpleNamespace(start=3.0, end=4.0, text="   "),
        ]
        return iter(segments), types.SimpleNamespace(language="en", duration=5.0)


@pytest.fixture
def tr(monkeypatch, tmp_path):
    inserted: list[tuple] = []
    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=lambda url: FakeConn(inserted)))
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=object))
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    module = importlib.reload(importlib.import_module("recording_transcriber.transcribe_recordings"))
    module.inserted = inserted
    return module


def test_dry_run_writes_nothing(tr):
    tr.run_pass(FakeModel(), dry_run=True)
    assert tr.inserted == []
    assert tr.load_state() == {}


def test_rows_are_relative_to_session_start_and_blank_text_is_dropped(tr):
    tr.run_pass(FakeModel())
    assert tr.inserted == [(14, 51.0, 52.5, "hello", "en", "uid", "rec:uid:0")]
    assert tr.load_state()["14"]["status"] == "done"


def test_done_meeting_is_not_transcribed_again(tr):
    tr.run_pass(FakeModel())
    tr.run_pass(FakeModel())
    assert len(tr.inserted) == 1


def test_failures_stop_after_max_attempts(tr):
    for _ in range(tr.MAX_ATTEMPTS + 2):
        tr.run_pass(FakeModel(fail=True))
    state = tr.load_state()["14"]
    assert state["status"] == "failed"
    assert state["attempts"] == tr.MAX_ATTEMPTS
    assert tr.inserted == []


def test_crash_mid_transcription_still_counts_an_attempt(tr):
    class CrashingModel(FakeModel):
        def transcribe(self, path: str, **kwargs: object) -> tuple:
            assert tr.load_state()["14"] == {**tr.load_state()["14"], "status": "running", "attempts": 1}
            raise SystemExit("killed")

    with pytest.raises(SystemExit):
        tr.run_pass(CrashingModel())
    assert tr.load_state()["14"]["attempts"] == 1
