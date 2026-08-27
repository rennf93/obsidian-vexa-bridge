"""Graph mode: pure transcript rendering plus the ensure/push/pull orchestration steps."""

from summarizer import graph
from summarizer.types import MeetingMeta, Utterance


def _meta(native="1517719641449955361", platform="discord"):
    return MeetingMeta(
        participants=["David", "Renzo"],
        date="2026-07-06",
        duration="00:13:00",
        platform=platform,
        meeting_id=7,
        native_meeting_id=native,
    )


def test_transcript_filename_is_date_platform_native():
    assert graph.transcript_filename(_meta()) == "2026-07-06-discord-1517719641449955361.md"


def test_transcript_filename_sanitizes_native_id():
    assert graph.transcript_filename(_meta(native="abc/def:ghi jkl")) == "2026-07-06-discord-abc-def-ghi-jkl.md"


def test_transcript_filename_falls_back_to_meeting_id_when_native_is_all_junk():
    assert graph.transcript_filename(_meta(native="///")) == "2026-07-06-discord-7.md"


def test_render_transcript_has_frontmatter_and_timestamped_lines():
    utts = [Utterance("David", 0.0, 20.0, "We should ship it."), Utterance("Renzo", 65.0, 80.0, "Agreed.")]
    out = graph.render_transcript(_meta(), utts)
    head, body = out.split("---\n", 2)[1], out.split("---\n", 2)[2]
    assert "type: transcript" in head
    assert "meeting_id: 7" in head
    assert "native_meeting_id: 1517719641449955361" in head
    assert "platform: discord" in head
    assert "date: 2026-07-06" in head
    assert "duration: 00:13:00" in head
    assert "participants:\n  - David\n  - Renzo" in head
    assert body.strip().splitlines() == ["[00:00:00] David: We should ship it.", "[00:01:05] Renzo: Agreed."]
    assert out.endswith("\n")
