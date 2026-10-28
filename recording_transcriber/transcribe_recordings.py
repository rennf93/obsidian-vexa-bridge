"""Post-meeting transcription for Vexa bots that join recording-only (TRANSCRIBE_ENABLED=false).

Live transcription on a CPU box rejects most of the bot's audio (HTTP 503 once the worker falls
behind), so the bots only record and this service transcribes each finished recording instead.
Each pass finds completed meetings that have a finalized master audio file and no transcript
rows, transcribes the recording with the worker's own faster-whisper model and weights, and
inserts every segment in one transaction, so a reader (the Obsidian bridge) never sees half a
transcript. Done and failed meetings are kept in STATE_PATH, so a silent recording (0 segments)
is not transcribed again on every pass and a broken one stops after MAX_ATTEMPTS.

usage: python -m recording_transcriber.transcribe_recordings                      poll loop
       python -m recording_transcriber.transcribe_recordings --once               one pass
       python -m recording_transcriber.transcribe_recordings --meeting 14 --dry-run   one meeting, write nothing
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import time
from pathlib import Path

import numpy as np
import psycopg
from faster_whisper import WhisperModel, decode_audio

from recording_transcriber import diarize

DATABASE_URL = os.environ["DATABASE_URL"]
RECORDINGS_ROOT = Path(os.environ.get("RECORDINGS_ROOT", "/storage/data/vexa"))
STATE_PATH = Path(os.environ.get("STATE_PATH", "/state/state.json"))
PLATFORMS = [p.strip() for p in os.environ.get("PLATFORMS", "google_meet,zoom,teams").split(",") if p.strip()]
LOOKBACK_HOURS = float(os.environ.get("LOOKBACK_HOURS", "48"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "120"))
DIARIZATION_ENABLED = os.environ.get("DIARIZATION_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
DIARIZATION_MODEL = Path(os.environ.get("DIARIZATION_MODEL", "/app/diarization/titanet_small.onnx"))
MAX_ATTEMPTS = 3

log = logging.getLogger("recording-transcriber")

# One row per finalized master audio file of a completed meeting that has no transcript yet.
# meetings.end_time is a naive UTC timestamp, hence the explicit UTC now. `recordings` and
# `media_files` can hold a JSON null, which jsonb_array_elements rejects for the whole query, so
# anything that is not an array counts as empty. A meeting is only taken once every master audio
# recording in it is final, so a session that is still finalizing cannot be left out of the transcript.
CANDIDATES_SQL = """
WITH recs AS (
    SELECT m.id, rec, mf
    FROM meetings m
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(m.data->'recordings') = 'array' THEN m.data->'recordings' ELSE '[]' END) rec
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(rec->'media_files') = 'array' THEN rec->'media_files' ELSE '[]' END) mf
    WHERE mf->>'type' = 'audio' AND mf->>'storage_path' LIKE '%%/master.webm'
)
SELECT m.id, ms.session_uid, ms.session_start_time, (r.rec->>'created_at')::timestamptz, r.mf->>'storage_path'
FROM meetings m
JOIN meeting_sessions ms ON ms.meeting_id = m.id
JOIN recs r ON r.id = m.id AND r.rec->>'session_uid' = ms.session_uid
WHERE m.status = 'completed'
  AND m.platform = ANY(%(platforms)s)
  AND (%(meeting)s::int IS NOT NULL OR m.end_time > (now() AT TIME ZONE 'utc') - %(lookback)s * interval '1 hour')
  AND (%(meeting)s::int IS NULL OR m.id = %(meeting)s::int)
  AND NOT EXISTS (
      SELECT 1 FROM recs pending WHERE pending.id = m.id AND pending.mf->>'is_final' IS DISTINCT FROM 'true')
  AND NOT EXISTS (SELECT 1 FROM transcriptions t WHERE t.meeting_id = m.id)
ORDER BY m.id, ms.session_start_time
"""

INSERT_SQL = """
INSERT INTO transcriptions
    (meeting_id, start_time, end_time, text, speaker, language, created_at, session_uid, segment_id)
VALUES (%s, %s, %s, %s, %s, %s, now() AT TIME ZONE 'utc', %s, %s)
ON CONFLICT (meeting_id, segment_id) WHERE segment_id IS NOT NULL DO NOTHING
"""


def load_state() -> dict[str, dict]:
    try:
        return json.loads(STATE_PATH.read_text())
    except FileNotFoundError:
        return {}


def save_state(state: dict[str, dict]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def load_model() -> WhisperModel:
    # The worker's weights volume is mounted read-only; local_files_only keeps HF Hub out of it.
    return WhisperModel(
        os.environ.get("MODEL_SIZE", "large-v3-turbo"),
        device="cpu",
        compute_type=os.environ.get("COMPUTE_TYPE", "int8"),
        cpu_threads=int(os.environ.get("CPU_THREADS", "8")),
        download_root=os.environ.get("MODEL_DIR", "/app/models"),
        local_files_only=True,
    )


def diarized_rows(
    audio: np.ndarray,
    words: list[diarize.Word],
    offset_s: float,
    language: str,
    session_uid: str,
    first_label: int = 0,
) -> list[tuple]:
    """One row per utterance with a `Speaker N` label.

    Numbering starts after first_label, the highest label used by earlier sessions of the same meeting, so a
    label never stands for two sessions' speakers (the bridge's speaker naming maps labels meeting-wide).
    """
    speakers = diarize.diarize(audio, words, str(DIARIZATION_MODEL))
    utterances = diarize.build_utterances(words, speakers, first_label=first_label)
    return [
        (offset_s + start, offset_s + end, text, label, language, session_uid, f"rec:{session_uid}:{i}")
        for i, (start, end, label, text) in enumerate(utterances)
    ]


def segment_rows(segments: list, offset_s: float, language: str, session_uid: str) -> list[tuple]:
    """One row per whisper segment, speaker unknown (the behavior without diarization)."""
    rows = []
    for i, seg in enumerate(segments):
        text = seg.text.strip()
        if text:
            rows.append(
                (offset_s + seg.start, offset_s + seg.end, text, None, language, session_uid, f"rec:{session_uid}:{i}")
            )
    return rows


def transcribe_session(
    model: WhisperModel, audio: Path, offset_s: float, session_uid: str, first_label: int = 0
) -> list[tuple]:
    """Rows for one session's recording; times are relative to the session start, as Vexa stores them."""
    started = time.monotonic()
    samples = decode_audio(str(audio), sampling_rate=diarize.SAMPLE_RATE)
    segment_iter, info = model.transcribe(
        samples, vad_filter=True, beam_size=5, condition_on_previous_text=False, word_timestamps=True
    )
    segments = list(segment_iter)
    rows = None
    if DIARIZATION_ENABLED and DIARIZATION_MODEL.is_file():
        try:
            words = [(w.start, w.end, w.word) for seg in segments for w in (seg.words or [])]
            rows = diarized_rows(samples, words, offset_s, info.language, session_uid, first_label) or None
            if rows is None and any(seg.text.strip() for seg in segments):
                log.warning("%s: no word timestamps to diarize, keeping one row per segment", audio.name)
        except Exception:
            # The transcript must not be lost or delayed by the speaker split.
            log.exception("%s: diarization failed, keeping one row per segment", audio.name)
    else:
        log.info("%s: diarization off or model missing at %s", audio.name, DIARIZATION_MODEL)
    if rows is None:
        rows = segment_rows(segments, offset_s, info.language, session_uid)
    log.info(
        "%s: %d segments, language=%s, audio %.0fs in %.0fs",
        audio.name,
        len(rows),
        info.language,
        info.duration,
        time.monotonic() - started,
    )
    return rows


def run_pass(model: WhisperModel, meeting: int | None = None, dry_run: bool = False) -> None:
    state = load_state()
    with psycopg.connect(DATABASE_URL) as conn:
        found = conn.execute(
            CANDIDATES_SQL, {"platforms": PLATFORMS, "lookback": LOOKBACK_HOURS, "meeting": meeting}
        ).fetchall()
    sessions: dict[int, list[tuple]] = {}
    for meeting_id, session_uid, session_start, recording_start, storage_path in found:
        sessions.setdefault(meeting_id, []).append((session_uid, session_start, recording_start, storage_path))

    for meeting_id, recs in sessions.items():
        key = str(meeting_id)
        entry = state.get(key, {})
        if not dry_run and (entry.get("status") == "done" or entry.get("attempts", 0) >= MAX_ATTEMPTS):
            continue
        log.info("meeting %s: transcribing %d recording(s)", meeting_id, len(recs))
        attempts = entry.get("attempts", 0) + 1
        if not dry_run:
            # Counted before the work starts, so a crash (OOM, kill) also uses up an attempt
            # instead of restarting into the same meeting forever.
            state[key] = {"status": "running", "attempts": attempts, "at": dt.datetime.now(dt.UTC).isoformat()}
            save_state(state)
        try:
            rows: list[tuple] = []
            for session_uid, session_start, recording_start, storage_path in recs:
                offset_s = max(0.0, (recording_start - session_start).total_seconds())
                first_label = max((int(r[4].split()[1]) for r in rows if r[4]), default=0)
                for row in transcribe_session(
                    model, RECORDINGS_ROOT / storage_path, offset_s, session_uid, first_label
                ):
                    rows.append((meeting_id, *row))
            if dry_run:
                log.info("[dry-run] meeting %s: %d rows, first: %s", meeting_id, len(rows), rows[:3])
                continue
            with psycopg.connect(DATABASE_URL) as conn, conn.transaction():
                conn.cursor().executemany(INSERT_SQL, rows)
            state[key] = {"status": "done", "segments": len(rows), "at": dt.datetime.now(dt.UTC).isoformat()}
            log.info("meeting %s: inserted %d segments", meeting_id, len(rows))
        except Exception:
            log.exception("meeting %s: failed", meeting_id)
            if dry_run:
                continue
            state[key] = {"status": "failed", "attempts": attempts, "at": dt.datetime.now(dt.UTC).isoformat()}
        save_state(state)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--meeting", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.nice(10)  # the live worker still serves Discord calls; this batch job yields to it
    model = load_model()
    log.info("ready: platforms=%s lookback=%sh root=%s", PLATFORMS, LOOKBACK_HOURS, RECORDINGS_ROOT)
    if args.once or args.meeting is not None:
        run_pass(model, args.meeting, args.dry_run)
        return
    while True:
        try:
            run_pass(model)
        except Exception:
            log.exception("pass failed")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
