"""Graph mode: the bridge feeds transcripts to Vexa's agent workspace instead of summarizing.

Per completed meeting the bridge renders the transcript as a frontmattered markdown file and
uploads it to the user's Vexa workspace (it lands under uploads/). A standing Vexa routine
(cron, see ROUTINE_PROMPT) runs the workspace's agents/meeting-to-graph.md over the inbox and
commits OKF entities into kg/; that cron is the safety net. The fast path is
trigger_routine_now: right after a pass uploads something, the bridge fires one immediate run
of the same routine, so the fold usually happens within minutes instead of waiting for the
next cron tick. The bridge then pushes the workspace to its git home whenever local commits
are ahead, and fast-forwards <VAULT_DIR>/<vault_folder> when VAULT_DIR is set.

Pure helpers first (rendering), then the three orchestration steps; HTTP goes through
summarizer.agent_api so tests fake one seam.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from summarizer import agent_api
from summarizer.llm import _format_transcript

if TYPE_CHECKING:
    from summarizer.config import Config
    from summarizer.types import MeetingMeta, Utterance

log = logging.getLogger("vexa-summarizer")

ROUTINE_PROMPT = (
    "Follow agents/meeting-to-graph.md exactly. For every file directly under uploads/ (not under "
    "uploads/processed/), fold that transcript into the knowledge graph under kg/, then move the file "
    "to uploads/processed/. If uploads/ has no new files, do nothing and say so."
)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def transcript_filename(meta: MeetingMeta) -> str:
    """<date>-<platform>-<native id>.md with the native id reduced to a safe token."""
    safe = _UNSAFE.sub("-", meta.native_meeting_id).strip("-.")
    return f"{meta.date}-{meta.platform}-{safe or meta.meeting_id}.md"


def render_transcript(meta: MeetingMeta, transcript: list[Utterance]) -> str:
    """YAML frontmatter the agent can read as facts, then one [HH:MM:SS] Speaker: text line each."""
    head = [
        "---",
        "type: transcript",
        f"meeting_id: {meta.meeting_id}",
        f"native_meeting_id: {meta.native_meeting_id}",
        f"platform: {meta.platform}",
        f"date: {meta.date}",
        f"duration: {meta.duration}",
        "participants:",
        *[f"  - {p}" for p in meta.participants],
        "---",
        "",
    ]
    return "\n".join(head) + _format_transcript(transcript) + "\n"


async def ensure_routine(cfg: Config) -> bool:
    """Create the standing fold-the-inbox routine, or fix it up when it's out of date.

    Vexa derives a routine's id from (subject, name, cron), so a routine already found under
    cfg.graph_routine_name is only up to date when its cron also matches cfg.graph_routine_cron;
    a cron change makes a new id, not an update to the old one, so the stale entry has to be
    deleted (by the "id" its GET /agent/routines card carries) before the new one is created, or
    Vexa ends up running both crons forever. AgentApiError propagates so the caller can log a
    501 (scheduler not wired) once and keep uploading; the routine can be created later.
    """
    existing = await agent_api.list_routines(cfg)
    current = next((r for r in existing if r.get("name") == cfg.graph_routine_name), None)
    if current is not None:
        if current.get("cron") == cfg.graph_routine_cron:
            return True
        log.warning(
            "Vexa routine %r cron changed (%s -> %s); retiring the old entry",
            cfg.graph_routine_name,
            current.get("cron"),
            cfg.graph_routine_cron,
        )
        await agent_api.delete_routine(cfg, str(current["id"]))
    await agent_api.create_routine(cfg, cfg.graph_routine_name, cfg.graph_routine_cron, ROUTINE_PROMPT, run_now=False)
    log.info("created Vexa routine %r (%s)", cfg.graph_routine_name, cfg.graph_routine_cron)
    return True


async def trigger_routine_now(cfg: Config) -> None:
    """Fire one immediate run of the fold routine (POST /agent/routines with run_now=True; Vexa
    dedups the scheduled job on the routine's deterministic id, so this never creates a second
    cron entry as long as the cron matches the routine ensure_routine reconciled). Called right
    after a pass uploaded something, so a transcript is folded within minutes instead of waiting
    for the next cron tick."""
    await agent_api.create_routine(cfg, cfg.graph_routine_name, cfg.graph_routine_cron, ROUTINE_PROMPT, run_now=True)
    log.info("triggered an immediate run of Vexa routine %r", cfg.graph_routine_name)


async def push_if_ahead(cfg: Config) -> bool:
    """Push the workspace to its git home when the agent has committed locally. Fast-forward only
    on Vexa's side; a diverged remote surfaces as AgentApiError(502) for the caller to log."""
    status = await agent_api.remote_status(cfg)
    if not status.get("tracked") or int(status.get("ahead") or 0) <= 0:
        return False
    out = await agent_api.push(cfg)
    log.info("pushed workspace %s -> %s", out.get("branch"), out.get("head_sha"))
    return True


async def wait_for_commit(
    cfg: Config,
    base_sha: str | None,
    *,
    timeout_seconds: float,
    interval_seconds: float = 15.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Poll agent_api.git_head until it reports a sha different from base_sha (a None base counts
    as "any commit"), so the event path's push can follow the agent's commit instead of waiting
    for the next poll pass. An API error mid-poll (Vexa briefly unreachable) is logged and treated
    as "no change yet" rather than raised -- the caller falls back to the poll pass either way on
    a real timeout. sleep is injectable so tests don't wait in real time."""
    elapsed = 0.0
    while True:
        try:
            head = await agent_api.git_head(cfg)
        except Exception as exc:
            log.debug("git_head check failed while waiting for the agent commit (retrying): %s", exc)
        else:
            if head is not None and head != base_sha:
                log.info("agent commit %s landed after %.0fs", head, elapsed)
                return True
        if elapsed >= timeout_seconds:
            return False
        await sleep(min(interval_seconds, timeout_seconds - elapsed))
        elapsed += interval_seconds


async def finalize_graph_pass(cfg: Config, uploaded: int, *, wait_for_agent_commit: bool = False) -> None:
    """Shared graph-mode post-pass: trigger the fold routine, push, and pull. Called from both
    the poll pass (summarizer.__main__._run_once_graph) and the webhook event path
    (summarizer.__main__.process_event_meeting) right after their process_meeting loop, so there
    is exactly one place that owns this ordering. Callers gate the call on `not cfg.dry_run`
    themselves -- a dry run must call neither this nor anything it wraps.

    When uploaded > 0, this fires trigger_routine_now so the fold happens within minutes instead
    of waiting for the routine's cron; that trigger is best-effort like the push and the pull
    below, so a failure there is logged and the cron picks it up next tick.

    wait_for_agent_commit=True (the event path only) additionally waits for the agent's commit
    before pushing: it records the workspace's HEAD sha before triggering the fold (best-effort;
    a failure there just means "unknown base", not a hard stop), then after triggering polls for
    a new commit up to cfg.webhook_commit_wait_seconds. Seeing the commit lets the push go out
    right away instead of waiting for the next poll pass; a timeout logs a warning and still
    pushes once (harmless if nothing is ahead), so the poll pass remains the eventual backstop
    either way. The poll path (wait_for_agent_commit=False, the default) skips this wait and
    pushes on whatever push_if_ahead reports from git-remote-status.

    The push and the pull are always best-effort: an Agent API failure there is logged and the
    pass still counts, so a scheduler that is not wired yet or a diverged remote never blocks
    transcript delivery.
    """
    if uploaded > 0:
        base: str | None = None
        if wait_for_agent_commit:
            try:
                base = await agent_api.git_head(cfg)
            except Exception as exc:
                log.warning("git HEAD check before the agent run failed (waiting for any commit): %s", exc)
        try:
            await trigger_routine_now(cfg)
        except Exception as exc:
            log.warning("immediate routine run failed (the cron picks it up): %s", exc)
        if wait_for_agent_commit:
            seen = await wait_for_commit(
                cfg,
                base,
                timeout_seconds=cfg.webhook_commit_wait_seconds,
                interval_seconds=cfg.webhook_commit_poll_seconds,
            )
            if not seen:
                log.warning(
                    "agent commit not seen within %s s; the poll pass pushes it later",
                    cfg.webhook_commit_wait_seconds,
                )
    try:
        await push_if_ahead(cfg)
    except Exception as exc:
        log.warning("workspace push failed (resolve on the repo or via /agent/workspace/pull): %s", exc)
    pull_vault(cfg)


def pull_vault(cfg: Config, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> bool:
    """Fast-forward <VAULT_DIR>/<vault_folder> from its remote. Never merges, never raises: the vault
    folder is a read-only mirror, so a non-fast-forward means someone edited it locally and the
    operator has to resolve that deliberately."""
    if cfg.vault_dir is None:
        return False
    folder = cfg.vault_dir / cfg.vault_folder
    if not (folder / ".git").exists():
        log.warning("VAULT_DIR set but %s is not a git checkout; skipping pull", folder)
        return False
    try:
        proc = run(["git", "-C", str(folder), "pull", "--ff-only"], capture_output=True, text=True, check=False)
    except OSError as exc:
        log.error("vault pull failed in %s: could not run git: %s", folder, exc)
        return False
    if proc.returncode != 0:
        log.error(
            "vault pull failed in %s (the folder must stay a fast-forward mirror; do not edit it locally): %s",
            folder,
            (proc.stderr or proc.stdout).strip(),
        )
        return False
    return True
