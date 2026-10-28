"""recording_transcriber with psycopg and faster_whisper stubbed: no database, no model."""

from __future__ import annotations

import datetime as dt
import importlib
import sys
import types

import numpy as np
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
    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        types.SimpleNamespace(WhisperModel=object, decode_audio=lambda path, sampling_rate: np.zeros(16000)),
    )
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
    assert tr.inserted == [(14, 51.0, 52.5, "hello", None, "en", "uid", "rec:uid:0")]
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


def two_speaker_setup(count: int = 60):
    """Words 0.5 s apart in two long blocks; window embedding is the block's direction."""
    words = [(i * 0.5, i * 0.5 + 0.4, f" w{i}") for i in range(count)]
    return words


def fake_embedder(directions: dict[int, np.ndarray], half: int):
    """Embedding of a window = the direction of the block its start falls in (first `half` seconds: block 0)."""

    def embed(audio, windows, model_path, num_threads):
        return np.array([directions[0 if start < half else 1] for start, _ in windows], dtype=np.float32)

    return embed


def test_two_separated_clusters_give_two_speakers(tr):
    words = two_speaker_setup()
    embed = fake_embedder({0: np.array([1.0, 0.0]), 1: np.array([0.0, 1.0])}, half=15)
    speakers = tr.diarize.diarize(np.zeros(1), words, "m", embed=embed)
    assert set(speakers) == {0, 1}
    assert speakers[0] == 0 and speakers[-1] == 1


def test_one_cluster_gives_one_speaker(tr):
    words = two_speaker_setup()
    embed = fake_embedder({0: np.array([1.0, 0.0]), 1: np.array([1.0, 0.01])}, half=15)
    assert set(tr.diarize.diarize(np.zeros(1), words, "m", embed=embed)) == {0}


def test_tiny_third_cluster_is_merged_into_the_nearest(tr):
    words = two_speaker_setup(count=240)
    directions = {0: np.array([1.0, 0.0, 0.0]), 1: np.array([0.0, 1.0, 0.0])}
    third = np.array([0.0, 0.0, 1.0])

    def embed(audio, windows, model_path, num_threads):
        vectors = []
        for start, _ in windows:
            vectors.append(third if 50 <= start < 54 else directions[0 if start < 60 else 1])
        return np.array(vectors, dtype=np.float32)

    speakers = tr.diarize.diarize(np.zeros(1), words, "m", embed=embed)
    assert set(speakers) == {0, 1}


def test_no_words_or_one_window_is_speaker_zero_and_never_raises(tr):
    def boom(*args):
        raise AssertionError("embedder must not run")

    assert tr.diarize.diarize(np.zeros(1), [], "m", embed=boom) == []
    assert tr.diarize.diarize(np.zeros(1), [(0.0, 0.4, " hi"), (0.5, 0.9, " there")], "m", embed=boom) == [0, 0]


def test_smoothing_absorbs_a_short_run(tr):
    words = [(i * 0.2, i * 0.2 + 0.15, f" w{i}") for i in range(30)]
    labels = np.array([0] * 14 + [1] + [0] * 15)
    assert set(tr.diarize.smooth_labels(words, labels).tolist()) == {0}


def test_renumber_by_first_appearance(tr):
    assert tr.diarize.renumber_by_first_appearance([5, 5, 2, 5, 9, 2]) == [0, 0, 1, 0, 2, 1]


def test_utterances_split_on_speaker_gap_and_cap(tr):
    words = [
        (0.0, 0.5, " hello"),
        (0.6, 1.0, " there"),
        (1.2, 1.6, " yes"),
        (5.0, 5.4, " later"),
        (5.5, 5.9, " still"),
    ]
    out = tr.diarize.build_utterances(words, [0, 0, 1, 1, 1])
    assert out == [
        (0.0, 1.0, "Speaker 1", "hello there"),
        (1.2, 1.6, "Speaker 2", "yes"),
        (5.0, 5.9, "Speaker 2", "later still"),
    ]
    long_words = [(i * 1.0, i * 1.0 + 0.9, f" w{i}") for i in range(45)]
    capped = tr.diarize.build_utterances(long_words, [0] * 45)
    assert len(capped) == 2 and capped[0][1] - capped[0][0] <= 40.0


def test_diarization_failure_keeps_the_transcript(tr, monkeypatch, tmp_path):
    model_file = tmp_path / "m.onnx"
    model_file.write_bytes(b"x")
    monkeypatch.setattr(tr, "DIARIZATION_ENABLED", True)
    monkeypatch.setattr(tr, "DIARIZATION_MODEL", model_file)

    def boom(*args, **kwargs):
        raise RuntimeError("onnx failed")

    monkeypatch.setattr(tr.diarize, "diarize", boom)

    class WordModel(FakeModel):
        def transcribe(self, path, **kwargs):
            seg = types.SimpleNamespace(
                start=1.0, end=2.5, text=" hello ", words=[types.SimpleNamespace(start=1.0, end=2.5, word=" hello")]
            )
            return iter([seg]), types.SimpleNamespace(language="en", duration=5.0)

    tr.run_pass(WordModel())
    assert tr.inserted == [(14, 51.0, 52.5, "hello", None, "en", "uid", "rec:uid:0")]
    assert tr.load_state()["14"]["status"] == "done"


def test_diarized_rows_carry_speaker_labels(tr, monkeypatch, tmp_path):
    model_file = tmp_path / "m.onnx"
    model_file.write_bytes(b"x")
    monkeypatch.setattr(tr, "DIARIZATION_ENABLED", True)
    monkeypatch.setattr(tr, "DIARIZATION_MODEL", model_file)
    monkeypatch.setattr(tr.diarize, "diarize", lambda audio, words, path: [0, 1])

    class WordModel(FakeModel):
        def transcribe(self, path, **kwargs):
            words = [
                types.SimpleNamespace(start=1.0, end=1.5, word=" hi"),
                types.SimpleNamespace(start=1.6, end=2.0, word=" yo"),
            ]
            seg = types.SimpleNamespace(start=1.0, end=2.0, text=" hi yo", words=words)
            return iter([seg]), types.SimpleNamespace(language="en", duration=5.0)

    tr.run_pass(WordModel())
    assert [(r[4], r[7]) for r in tr.inserted] == [("Speaker 1", "rec:uid:0"), ("Speaker 2", "rec:uid:1")]


def test_empty_diarized_rows_fall_back_to_segments(tr, monkeypatch, tmp_path):
    model_file = tmp_path / "m.onnx"
    model_file.write_bytes(b"x")
    monkeypatch.setattr(tr, "DIARIZATION_ENABLED", True)
    monkeypatch.setattr(tr, "DIARIZATION_MODEL", model_file)

    class NoWordsModel(FakeModel):
        def transcribe(self, path, **kwargs):
            seg = types.SimpleNamespace(start=1.0, end=2.5, text=" hello ", words=None)
            return iter([seg]), types.SimpleNamespace(language="en", duration=5.0)

    tr.run_pass(NoWordsModel())
    assert tr.inserted == [(14, 51.0, 52.5, "hello", None, "en", "uid", "rec:uid:0")]


def test_second_session_continues_speaker_numbering(tr, monkeypatch, tmp_path):
    monkeypatch.setattr(tr.diarize, "diarize", lambda audio, words, path: [0, 1])
    words = [(1.0, 1.5, " hi"), (1.6, 2.0, " yo")]
    first = tr.diarized_rows(np.zeros(1), words, 0.0, "en", "a")
    top = max(int(r[3].split()[1]) for r in first)
    second = tr.diarized_rows(np.zeros(1), words, 0.0, "en", "b", top)
    assert [r[3] for r in first + second] == ["Speaker 1", "Speaker 2", "Speaker 3", "Speaker 4"]


def test_window_count_is_capped_and_short_meetings_keep_the_hop(tr):
    long_words = [(i * 0.5, i * 0.5 + 0.4, " w") for i in range(60_000)]
    assert len(tr.diarize.make_windows(long_words)) <= tr.diarize.MAX_WINDOWS
    short = tr.diarize.make_windows(two_speaker_setup())
    assert short[1][0] - short[0][0] == pytest.approx(0.75)
