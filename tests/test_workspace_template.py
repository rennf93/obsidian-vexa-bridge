"""The workspace template is the conventions contract for Vexa's agent; keep it whole."""

import re
from pathlib import Path

from summarizer import workspace

ENTITY_TYPES = ["person", "company", "project", "meeting", "decision", "topic"]


def test_template_ships_every_reserved_file():
    t = workspace.TEMPLATE_DIR
    for rel in ["CLAUDE.md", "README.md", "agents/meeting-to-graph.md", "log.md", "kg/index.md"]:
        assert (t / rel).is_file(), rel
    for typ in ENTITY_TYPES:
        assert (t / "kg" / "entities" / typ / "index.md").is_file(), typ
    assert (t / "uploads" / ".gitkeep").is_file()
    assert (t / "uploads" / "processed" / ".gitkeep").is_file()
    assert any((t / "Dashboards").glob("*.md"))


def test_conventions_state_the_rules_the_agent_must_follow():
    text = (workspace.TEMPLATE_DIR / "CLAUDE.md").read_text()
    for needle in [
        "kg/entities/<type>/<slug>.md",
        "`type`, `id`, `title`",
        "aliases",
        "index.md",
        "log.md",
        "uploads/processed/",
        "never link",
        "update in place",
        "## TL;DR",
        "## Action items",
    ]:
        assert needle in text, needle


def test_meeting_to_graph_prompt_references_conventions_and_inbox():
    text = (workspace.TEMPLATE_DIR / "agents" / "meeting-to-graph.md").read_text()
    assert "CLAUDE.md" in text
    assert "uploads/processed/" in text
    assert "kg/entities/meeting/" in text


def test_no_typographic_dashes_in_template():
    for path in workspace.TEMPLATE_DIR.rglob("*.md"):
        assert not re.search("[–—]", path.read_text()), path


def test_init_workspace_copies_without_overwriting(tmp_path):
    written = workspace.init_workspace(tmp_path)
    assert (tmp_path / "CLAUDE.md").is_file()
    assert Path("CLAUDE.md") in [p.relative_to(tmp_path) for p in written]
    (tmp_path / "CLAUDE.md").write_text("mine")
    again = workspace.init_workspace(tmp_path)
    assert (tmp_path / "CLAUDE.md").read_text() == "mine"
    assert all(p.name != "CLAUDE.md" for p in again)


def test_setup_instructions_never_contain_a_secret():
    text = workspace.setup_instructions("http://vexa:8056", "https://github.com/me/vexa-kg.git")
    assert "/agent/workspace/git-token" in text
    assert "/agent/workspace/swap" in text
    assert "https://github.com/me/vexa-kg.git" in text
    assert "<PAT>" in text
    assert "ghp_" not in text
