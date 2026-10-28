"""Post-meeting transcription for Vexa bots that join recording-only (TRANSCRIBE_ENABLED=false).

Live transcription on a CPU box rejects most of the bot's audio (HTTP 503 once the worker falls
behind), so the bots only record and this service transcribes each finished recording instead.
Each pass finds completed meetings that have a finalized master audio file and no transcript
rows, transcribes the recording with the worker's own faster-whisper model and weights, and
inserts every segment in one transaction, so a reader (the Obsidian bridge) never sees half a
transcript. Done and failed meetings are kept in STATE_PATH, so a silent recording (0 segments)
is not transcribed again on every pass and a broken one stops after MAX_ATTEMPTS.

usage: python transcribe_recordings.py                      poll loop
       python transcribe_recordings.py --once               one pass
       python transcribe_recordings.py --meeting 14 --dry-run   transcribe one meeting, write nothing
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import time
from pathlib import Path

import psycopg
from faster_whisper import WhisperModel

DATABASE_URL = os.environ["DATABASE_URL"]
RECORDINGS_ROOT = Path(os.environ.get("RECORDINGS_ROOT", "/storage/data/vexa"))
STATE_PATH = Path(os.environ.get("STATE_PATH", "/state/state.json"))
PLATFORMS = [p.strip() for p in os.environ.get("PLATFORMS", "google_meet,zoom,teams").split(",") if p.strip()]
LOOKBACK_HOURS = float(os.environ.get("LOOKBACK_HOURS", "48"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "120"))
MAX_ATTEMPTS = 3

log = logging.getLogger("recording-transcriber")

# One row per finalized master audio file of a completed meeting that has no transcript yet.
# meetings.end_time is a naive UTC timestamp, hence the explicit UTC now.
CANDIDATES_SQL = """
SELECT m.id, ms.session_uid, ms.session_start_time, (rec->>'created_at')::timestamptz, mf->>'storage_path'
FROM meetings m
JOIN meeting_sessions ms ON ms.meeting_id = m.id
CROSS JOIN LATERAL jsonb_array_elements(COALESCE(m.data->'recordings', '[]'::jsonb)) rec
CROSS JOIN LATERAL jsonb_array_elements(COALESCE(rec->'media_files', '[]'::jsonb)) mf
WHERE m.status = 'completed'
  AND m.platform = ANY(%(platforms)s)
  AND (%(meeting)s::int IS NOT NULL OR m.end_time > (now() AT TIME ZONE 'utc') - %(lookback)s * interval '1 hour')
  AND (%(meeting)s::int IS NULL OR m.id = %(meeting)s::int)
  AND rec->>'session_uid' = ms.session_uid
  AND mf->>'type' = 'audio'
  AND (mf->>'is_final')::boolean
  AND mf->>'storage_path' LIKE '%%/master.webm'
  AND NOT EXISTS (SELECT 1 FROM transcriptions t WHERE t.meeting_id = m.id)
ORDER BY m.id, ms.session_start_time
"""

INSERT_SQL = """
INSERT INTO transcriptions
    (meeting_id, start_time, end_time, text, speaker, language, created_at, session_uid, segment_id)
VALUES (%s, %s, %s, %s, NULL, %s, now() AT TIME ZONE 'utc', %s, %s)
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


def transcribe_session(model: WhisperModel, audio: Path, offset_s: float, session_uid: str) -> list[tuple]:
    """Rows for one session's recording; times are relative to the session start, as Vexa stores them."""
    started = time.monotonic()
    segments, info = model.transcribe(str(audio), vad_filter=True, beam_size=5, condition_on_previous_text=False)
    rows = []
    for i, seg in enumerate(segments):
        text = seg.text.strip()
        if text:
            rows.append(
                (offset_s + seg.start, offset_s + seg.end, text, info.language, session_uid, f"rec:{session_uid}:{i}")
            )
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
            rows = []
            for session_uid, session_start, recording_start, storage_path in recs:
                offset_s = max(0.0, (recording_start - session_start).total_seconds())
                for row in transcribe_session(model, RECORDINGS_ROOT / storage_path, offset_s, session_uid):
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
