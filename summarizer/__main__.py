"""Long-running poll loop for the Vexa -> Obsidian sink adapter. Run as a container:
`python -m summarizer` (or `python -m summarizer --once` for a single validation pass).

Flow per pass: list Vexa completed meetings -> for each not done/poisoned -> fetch transcript
-> min-duration guard -> coverage guard -> summarize -> enabled sinks (Obsidian note via fs or
mcp, Vexa notes) -> mark_done. mark_done is the only commit and runs last, so a crash mid-pass
is harmless (next pass redoes it; the note sink's fail-if-exists is the backstop). Failures
record_failure; after 5 a meeting is poisoned and skipped until state.json is manually cleared.

The loop sleeps POLL_INTERVAL_SECONDS (default 180s) between passes. SIGTERM (docker stop)
cancels the sleep and exits cleanly within the interval — no mid-pass corruption. --once (or
ONCE=1) preserves the original one-shot behavior for DRY_RUN validation / manual reruns.

DRY_RUN runs the full pipeline (including the LLM call) but writes nothing and doesn't mark
done — safe to repeat for first-run validation.

In BRIDGE_MODE=graph the summarize and sink steps are replaced by an upload of the transcript
into the Vexa agent workspace (see summarizer/graph.py); once something is uploaded the bridge
triggers an immediate run of the fold routine, then the pass ends with a push of the workspace
and, when VAULT_DIR is set, a fast-forward pull of the vault folder.

WEBHOOK_ENABLED=true starts a second, event-driven path alongside the poll: an aiohttp server
(summarizer/webhook.py) that receives Vexa's meeting.completed webhook (and the same envelope
shape emitted by discord-vexa-bridge) and processes that meeting immediately via
process_event_meeting, instead of waiting up to POLL_INTERVAL_SECONDS. The poll stays the
fallback either way -- it is what eventually processes a meeting whose event never arrived or
arrived before its transcript was fully flushed. --once never starts the webhook server.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
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
    finalize_graph_pass,
    render_transcript,
    transcript_filename,
)
from summarizer.llm import summarize  # async; litellm lazy-imported inside
from summarizer.obsidian import assemble_note, create_note, note_path, write_note_fs
from summarizer.state import StateStore
from summarizer.types import Meeting, MeetingMeta, Utterance
from summarizer.vexa import get_transcript, list_completed_meetings, write_notes
from summarizer.webhook import pending_event_tasks, register_with_vexa, serve

log = logging.getLogger("vexa-summarizer")

_routine_ready: bool = False

# Below this, a meeting is too short to judge speech-vs-wall-clock coverage: a 90s call that's
# entirely a rejected upload looks identical to a 90s call with a slow start. Only meetings at
# least this long get the coverage gate; short ones only face the min-duration gate above it.
COVERAGE_GATE_MIN_WALL_CLOCK_SECONDS = 300.0


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
    """Process one meeting: done/poisoned check, transcript fetch, min-duration gate,
    coverage gate, then mode-specific handling (note: summarize + sink + notes + mark_done;
    graph: render + upload + mark_done). Shared by run_once's loop, _run_once_graph's loop,
    and the webhook path (process_event_meeting) so there is exactly one place that owns
    per-meeting error handling.

    mark_low_transcript=False skips mark_skipped on the min-duration and coverage gates: used
    by the webhook path, where an event can fire before Vexa's last transcript flush lands, so
    a meeting must stay eligible for the next poll rather than being permanently marked
    skipped.
    """
    key = meeting.id
    if store.is_done(key) or store.is_poisoned(key):
        result.idle += 1
        return
    try:
        utts = await get_transcript(cfg, meeting)
        # max(0.0, ...): belt and braces on top of vexa.get_transcript's own clamping, so one
        # malformed row (end_time before its own start_time) can't drag the total negative.
        duration = sum(max(0.0, u.end_time - u.start_time) for u in utts)
        if duration < cfg.min_transcript_seconds:
            if mark_low_transcript:
                store.mark_skipped(key, "low-transcript")
            result.skipped += 1
            log.info("meeting %s skipped: %.1fs < %.1fs min", key, duration, cfg.min_transcript_seconds)
            return

        # Speech seconds alone misses the case where Vexa's transcription worker rejected most
        # of the audio (e.g. HTTP 503 under load): duration clears the min-seconds gate above
        # even though the transcript covers a sliver of the actual call. Wall clock is only
        # trustworthy once a meeting has run long enough to judge -- a short call is never
        # gated on coverage.
        wall_clock = (meeting.end - meeting.start).total_seconds()
        if cfg.min_transcript_coverage > 0 and wall_clock >= COVERAGE_GATE_MIN_WALL_CLOCK_SECONDS:
            # Discord gives each speaker their own audio stream, so utterances legitimately
            # overlap and total speech can exceed wall clock; clamp at 1.0 rather than let
            # overlap read as coverage above 100%.
            coverage = min(duration / wall_clock, 1.0)
            if coverage < cfg.min_transcript_coverage:
                if mark_low_transcript:
                    store.mark_skipped(key, "low-coverage")
                result.skipped += 1
                log.info(
                    "meeting %s skipped: %.1fs speech / %.1fs wall clock = %.3f coverage < %.3f min",
                    key,
                    duration,
                    wall_clock,
                    coverage,
                    cfg.min_transcript_coverage,
                )
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
    """Graph mode pass: upload each new transcript to the workspace inbox, then finalize (trigger
    an immediate fold, push, pull) via finalize_graph_pass.

    mark_done commits at upload (the agent owns everything after that). The routine check runs
    once per process (module-level _routine_ready latch) rather than every pass, and never in
    DRY_RUN (a dry run must write nothing, on Vexa's side included); a failure leaves the latch
    unset so the next pass retries it. See finalize_graph_pass for the trigger/push/pull details
    (this path uses its defaults: no wait for the agent's commit before pushing).
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
        await finalize_graph_pass(cfg, result.uploaded)
    return result


async def process_event_meeting(cfg: Config, meeting: Meeting) -> PassResult:
    """Process one meeting delivered by a meeting.completed webhook, outside the poll loop.

    mark_low_transcript=False: the event can fire before Vexa's last transcript flush, so a
    below-minimum transcript is left for the next poll instead of being permanently skipped.

    In graph mode, finalize_graph_pass(..., wait_for_agent_commit=True) waits for the agent's
    commit before pushing, so the push goes out right away instead of waiting for the next poll
    pass. Note mode has nothing else to do once process_meeting returns.
    """
    store = StateStore(cfg.state_dir / "state.json")
    result = PassResult()
    await process_meeting(cfg, store, meeting, result, mark_low_transcript=False)

    if cfg.bridge_mode == "graph" and not cfg.dry_run:
        await finalize_graph_pass(cfg, result.uploaded, wait_for_agent_commit=True)

    return result


async def _loop(cfg: Config) -> None:
    """Run run_once on a fixed interval until SIGTERM (docker stop) cancels the sleep.

    Resilient: a run_once exception is caught and logged (run_once already catches per-meeting,
    but we guard the whole pass so the loop never dies). mark_done is the only commit, so a
    SIGTERM mid-pass either completes the pass or leaves it undone — never half-committed.

    WEBHOOK_ENABLED=true also starts the webhook receiver here (never under --once): it runs
    alongside the poll as a background task, and WEBHOOK_PUBLIC_URL (when set) registers it with
    Vexa once at startup, best-effort -- a failure there is logged and the poll still runs.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    except NotImplementedError:
        # Windows / non-Unix loops — no SIGTERM handler; loop runs until process kill.
        pass

    server_task: asyncio.Task[None] | None = None
    if cfg.webhook_enabled:
        if cfg.webhook_public_url:
            try:
                await register_with_vexa(cfg)
            except Exception as exc:
                log.warning("webhook registration with Vexa failed (register it manually, or retry later): %s", exc)

        async def _event_handler(meeting: Meeting) -> None:
            await process_event_meeting(cfg, meeting)

        server_task = asyncio.create_task(serve(cfg, _event_handler))

    try:
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
    finally:
        if server_task is not None:
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task
            await _drain_event_tasks(pending_event_tasks())

    log.info("received SIGTERM; exiting")


async def _drain_event_tasks(tasks: set[asyncio.Task[None]], timeout: float = 10.0) -> None:
    """Best-effort wait for in-flight webhook event tasks (a process_event_meeting run, possibly
    still inside wait_for_commit or a push) before the process exits, so a SIGTERM mid-event
    leaves a warning behind instead of silently dropping work. Never cancels anything; a task
    still running after the timeout keeps running until the process actually dies."""
    if not tasks:
        return
    _done, still_pending = await asyncio.wait(tasks, timeout=timeout)
    if still_pending:
        log.warning("%d webhook event task(s) still running at shutdown", len(still_pending))


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
