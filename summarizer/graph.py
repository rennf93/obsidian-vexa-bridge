"""Graph mode: the bridge feeds transcripts to Vexa's agent workspace instead of summarizing.

Per completed meeting the bridge renders the transcript as a frontmattered markdown file and
uploads it to the user's Vexa workspace (it lands under uploads/). A standing Vexa routine
(cron, see ROUTINE_PROMPT) runs the workspace's agents/meeting-to-graph.md over the inbox and
commits OKF entities into kg/. The bridge then pushes the workspace to its git home whenever
local commits are ahead, and fast-forwards <VAULT_DIR>/<vault_folder> when VAULT_DIR is set.

Pure helpers first (rendering), then the three orchestration steps; HTTP goes through
summarizer.agent_api so tests fake one seam.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable
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
    """Create the standing fold-the-inbox routine unless one with the configured name exists.

    Idempotent by name (Vexa has no upsert). AgentApiError propagates so the caller can log a
    501 (scheduler not wired) once and keep uploading; the routine can be created later.
    """
    existing = await agent_api.list_routines(cfg)
    if any(r.get("name") == cfg.graph_routine_name for r in existing):
        return True
    await agent_api.create_routine(cfg, cfg.graph_routine_name, cfg.graph_routine_cron, ROUTINE_PROMPT, run_now=False)
    log.info("created Vexa routine %r (%s)", cfg.graph_routine_name, cfg.graph_routine_cron)
    return True


async def push_if_ahead(cfg: Config) -> bool:
    """Push the workspace to its git home when the agent has committed locally. Fast-forward only
    on Vexa's side; a diverged remote surfaces as AgentApiError(502) for the caller to log."""
    status = await agent_api.remote_status(cfg)
    if not status.get("tracked") or int(status.get("ahead") or 0) <= 0:
        return False
    out = await agent_api.push(cfg)
    log.info("pushed workspace %s -> %s", out.get("branch"), out.get("head_sha"))
    return True


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
