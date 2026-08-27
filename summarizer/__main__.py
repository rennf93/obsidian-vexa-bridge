"""Long-running poll loop for the Vexa -> Obsidian sink adapter. Run as a container:
`python -m summarizer` (or `python -m summarizer --once` for a single validation pass).

Flow per pass: list Vexa completed meetings -> for each not done/poisoned -> fetch transcript
-> min-duration guard -> summarize -> enabled sinks (Obsidian note via fs or mcp, Vexa notes)
-> mark_done. mark_done is the only commit and runs last, so a crash mid-pass is harmless (next
pass redoes it; the note sink's fail-if-exists is the backstop). Failures record_failure; after
5 a meeting is poisoned and skipped until state.json is manually cleared.

The loop sleeps POLL_INTERVAL_SECONDS (default 180s) between passes. SIGTERM (docker stop)
cancels the sleep and exits cleanly within the interval — no mid-pass corruption. --once (or
ONCE=1) preserves the original one-shot behavior for DRY_RUN validation / manual reruns.

DRY_RUN runs the full pipeline (including the LLM call) but writes nothing and doesn't mark
done — safe to repeat for first-run validation.

In BRIDGE_MODE=graph the summarize and sink steps are replaced by an upload of the transcript
into the Vexa agent workspace (see summarizer/graph.py); once something is uploaded the bridge
triggers an immediate run of the fold routine, then the pass ends with a push of the workspace
and, when VAULT_DIR is set, a fast-forward pull of the vault folder.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass

from summarizer import config as _config_mod
from summarizer.agent_api import upload
from summarizer.config import Config, ConfigError
from summarizer.graph import (
    ensure_routine,
    pull_vault,
    push_if_ahead,
    render_transcript,
    transcript_filename,
    trigger_routine_now,
)
from summarizer.llm import summarize  # async; litellm lazy-imported inside
from summarizer.obsidian import assemble_note, create_note, note_path, write_note_fs
from summarizer.state import StateStore
from summarizer.types import Meeting, MeetingMeta, Utterance
from summarizer.vexa import get_transcript, list_completed_meetings, write_notes

log = logging.getLogger("vexa-summarizer")

_routine_ready: bool = False


@dataclass
class PassResult:
    summarized: int = 0
    uploaded: int = 0
    skipped: int = 0
    failed: int = 0
    idle: int = 0


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _meta_from(meeting: Meeting, utts: list[Utterance], duration: float) -> MeetingMeta:
    speakers = sorted({u.speaker for u in utts})
    return MeetingMeta(
        participants=speakers,
        date=meeting.start.strftime("%Y-%m-%d"),
        duration=_fmt_duration(duration),
        platform=meeting.platform,
        meeting_id=meeting.id,
        native_meeting_id=meeting.native_meeting_id,
    )


async def process_meeting(
    cfg: Config,
    store: StateStore,
    meeting: Meeting,
    result: PassResult,
    *,
    mark_low_transcript: bool = True,
) -> None:
    """Process one meeting: done/poisoned check, transcript fetch, min-duration gate, then
    mode-specific handling (note: summarize + sink + notes + mark_done; graph: render + upload
    + mark_done). Shared by run_once's loop, _run_once_graph's loop, and the webhook path
    (process_event_meeting) so there is exactly one place that owns per-meeting error handling.

    mark_low_transcript=False skips mark_skipped on the min-duration gate: used by the webhook
    path, where an event can fire before Vexa's last transcript flush lands, so a meeting must
    stay eligible for the next poll rather than being permanently marked skipped.
    """
    key = meeting.id
    if store.is_done(key) or store.is_poisoned(key):
        result.idle += 1
        return
    try:
        utts = await get_transcript(cfg, meeting)
        duration = sum(u.end_time - u.start_time for u in utts)
        if duration < cfg.min_transcript_seconds:
            if mark_low_transcript:
                store.mark_skipped(key, "low-transcript")
            result.skipped += 1
            log.info("meeting %s skipped: %.1fs < %.1fs min", key, duration, cfg.min_transcript_seconds)
            return

        meta = _meta_from(meeting, utts, duration)

        if cfg.bridge_mode == "graph":
            filename = transcript_filename(meta)
            content = render_transcript(meta, utts)
            if cfg.dry_run:
                result.uploaded += 1
                log.info("[DRY_RUN] meeting %s -> would upload %s", key, filename)
                return
            uploaded_path = await upload(cfg, filename, content)
            store.mark_done(key, uploaded_path)
            result.uploaded += 1
            log.info("meeting %s uploaded -> %s", key, uploaded_path)
            return

        summary_md = await summarize(utts, meta, cfg)
        note_md = assemble_note(meta, summary_md, utts, cfg)
        path = note_path(meeting, meta.participants, cfg) if cfg.obsidian_enabled else None

        if cfg.dry_run:
            result.summarized += 1
            log.info("[DRY_RUN] meeting %s -> would write %s", key, path)
            return

        if cfg.obsidian_enabled:
            if cfg.obsidian_sink == "fs":
                await write_note_fs(cfg, path, note_md)  # type: ignore[arg-type]
            else:
                await create_note(cfg, path, note_md)  # type: ignore[arg-type]
        if cfg.vexa_notes_enabled:
            await write_notes(cfg, meeting, note_md)
        store.mark_done(key, path)
        result.summarized += 1
        log.info("meeting %s summarized -> %s", key, path)
    except Exception as exc:
        store.record_failure(key)
        result.failed += 1
        log.warning("meeting %s failed: %s", key, exc)


async def run_once(cfg: Config) -> PassResult:
    if not cfg.summarize_enabled:
        log.info("SUMMARIZE_ENABLED=false; skipping pass")
        return PassResult()

    store = StateStore(cfg.state_dir / "state.json")
    result = PassResult()

    try:
        meetings = await list_completed_meetings(cfg, cfg.summarize_platforms)
    except Exception as exc:  # Vexa unreachable / 5xx — leave everything un-marked; retry next tick.
        log.warning("listing meetings failed: %s", exc)
        return result

    if cfg.bridge_mode == "graph":
        return await _run_once_graph(cfg, store, meetings)

    for meeting in meetings:
        await process_meeting(cfg, store, meeting, result)

    return result


async def _run_once_graph(cfg: Config, store: StateStore, meetings: list[Meeting]) -> PassResult:
    """Graph mode pass: upload each new transcript to the workspace inbox, trigger an immediate
    fold, then push and pull.

    mark_done commits at upload (the agent owns everything after that). The routine check runs
    once per process (module-level _routine_ready latch) rather than every pass, and never in
    DRY_RUN (a dry run must write nothing, on Vexa's side included); a failure
    leaves the latch unset so the next pass retries it. When the pass uploaded anything (and
    it's not a dry run), it also fires trigger_routine_now so the fold happens within minutes
    instead of waiting for the routine's cron; that trigger is best-effort like the push and the
    pull below, so a failure there is logged and the cron picks it up next tick. The push and
    the pull are best-effort per pass: an Agent API failure there is logged and the pass still
    counts, so a scheduler that is not wired yet or a diverged remote never blocks transcript
    delivery.
    """
    global _routine_ready
    result = PassResult()
    if not _routine_ready and not cfg.dry_run:
        try:
            await ensure_routine(cfg)
        except Exception as exc:
            log.warning("routine check failed (uploads continue; create the routine later): %s", exc)
        else:
            _routine_ready = True

    for meeting in meetings:
        await process_meeting(cfg, store, meeting, result)

    if not cfg.dry_run:
        if result.uploaded > 0:
            try:
                await trigger_routine_now(cfg)
            except Exception as exc:
                log.warning("immediate routine run failed (the cron picks it up): %s", exc)
        try:
            await push_if_ahead(cfg)
        except Exception as exc:
            log.warning("workspace push failed (resolve on the repo or via /agent/workspace/pull): %s", exc)
        pull_vault(cfg)
    return result


async def process_event_meeting(cfg: Config, meeting: Meeting) -> PassResult:
    """Process one meeting delivered by a meeting.completed webhook, outside the poll loop.

    mark_low_transcript=False: the event can fire before Vexa's last transcript flush, so a
    below-minimum transcript is left for the next poll instead of being permanently skipped.
    In graph mode this also runs the same best-effort post-steps as a pass that uploaded
    something (trigger_routine_now, push_if_ahead, pull_vault); note mode has nothing else to
    do once process_meeting returns.
    """
    store = StateStore(cfg.state_dir / "state.json")
    result = PassResult()
    await process_meeting(cfg, store, meeting, result, mark_low_transcript=False)

    if cfg.bridge_mode == "graph" and not cfg.dry_run:
        if result.uploaded > 0:
            try:
                await trigger_routine_now(cfg)
            except Exception as exc:
                log.warning("immediate routine run failed (the cron picks it up): %s", exc)
        try:
            await push_if_ahead(cfg)
        except Exception as exc:
            log.warning("workspace push failed (resolve on the repo or via /agent/workspace/pull): %s", exc)
        pull_vault(cfg)

    return result


async def _loop(cfg: Config) -> None:
    """Run run_once on a fixed interval until SIGTERM (docker stop) cancels the sleep.

    Resilient: a run_once exception is caught and logged (run_once already catches per-meeting,
    but we guard the whole pass so the loop never dies). mark_done is the only commit, so a
    SIGTERM mid-pass either completes the pass or leaves it undone — never half-committed.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    except NotImplementedError:
        # Windows / non-Unix loops — no SIGTERM handler; loop runs until process kill.
        pass

    while not stop.is_set():
        try:
            result = await run_once(cfg)
        except Exception as exc:  # run_once is defensive, but never kill the loop
            log.warning("run_once crashed: %s", exc)
        else:
            log.info(
                "pass complete: %d summarized, %d uploaded, %d skipped, %d failed, %d idle",
                result.summarized,
                result.uploaded,
                result.skipped,
                result.failed,
                result.idle,
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=cfg.poll_interval_seconds)
        except TimeoutError:
            pass  # interval elapsed — loop again

    log.info("received SIGTERM; exiting")


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="summarizer", description="Vexa -> Obsidian sink adapter")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    sub = parser.add_subparsers(dest="command")
    init = sub.add_parser("init-workspace", help="write the knowledge-workspace template into a local repo clone")
    init.add_argument("target", help="path to your local clone of the workspace repo")
    init.add_argument("--repo-url", required=True, help="HTTPS URL of that repo (what Vexa will clone)")
    init.add_argument("--api-base", default=os.getenv("VEXA_API_URL", "http://localhost:18056"))
    return parser.parse_args(argv)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    args = _parse(sys.argv[1:])
    if args.command == "init-workspace":
        from pathlib import Path

        from summarizer.workspace import init_workspace, setup_instructions

        written = init_workspace(Path(args.target))
        for p in written:
            print(f"wrote {p}")
        print(setup_instructions(args.api_base, args.repo_url))
        return 0
    once = bool(args.once or os.getenv("ONCE", "").strip().lower() in {"1", "true", "yes"})
    try:
        cfg = _config_mod.load_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    if once:
        result = asyncio.run(run_once(cfg))
        log.info(
            "pass complete: %d summarized, %d uploaded, %d skipped, %d failed, %d idle",
            result.summarized,
            result.uploaded,
            result.skipped,
            result.failed,
            result.idle,
        )
        return 0
    asyncio.run(_loop(cfg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
