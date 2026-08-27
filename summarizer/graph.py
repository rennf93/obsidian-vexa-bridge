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
import subprocess  # noqa: F401  # used by Task 4's push/pull orchestration
from collections.abc import Callable  # noqa: F401  # used by Task 4's orchestration signatures
from typing import TYPE_CHECKING, Any  # noqa: F401  # Any used by Task 4's orchestration signatures

from summarizer import agent_api  # noqa: F401  # used by Task 4; import now pins the Task 3 dependency
from summarizer.llm import _format_transcript

if TYPE_CHECKING:
    from summarizer.config import Config  # noqa: F401  # used by Task 4's orchestration signatures
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
