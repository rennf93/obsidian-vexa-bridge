"""Graph mode: pure transcript rendering plus the ensure/push/pull orchestration steps."""

import subprocess

import pytest

from summarizer import agent_api, graph
from summarizer.config import Config
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


def _cfg(tmp_path=None):
    cfg = Config(vexa_api_url="http://vexa:8056", vexa_api_key="k", bridge_mode="graph")
    if tmp_path is not None:
        cfg.vault_dir = tmp_path
    return cfg


async def test_ensure_routine_creates_when_missing(monkeypatch):
    created = []

    async def fake_list(cfg):
        return [{"name": "other"}]

    async def fake_create(cfg, name, cron, prompt, run_now=False):
        created.append((name, cron, prompt, run_now))
        return {"job_id": "job_1"}

    monkeypatch.setattr(agent_api, "list_routines", fake_list)
    monkeypatch.setattr(agent_api, "create_routine", fake_create)
    assert await graph.ensure_routine(_cfg()) is True
    assert created == [("meeting-to-graph", "*/15 * * * *", graph.ROUTINE_PROMPT, False)]


async def test_ensure_routine_is_a_noop_when_present(monkeypatch):
    async def fake_list(cfg):
        return [{"name": "meeting-to-graph", "cron": "*/15 * * * *"}]

    async def fake_create(cfg, name, cron, prompt, run_now=False):
        raise AssertionError("must not create a duplicate")

    monkeypatch.setattr(agent_api, "list_routines", fake_list)
    monkeypatch.setattr(agent_api, "create_routine", fake_create)
    assert await graph.ensure_routine(_cfg()) is True


async def test_ensure_routine_propagates_api_errors(monkeypatch):
    async def fake_list(cfg):
        raise agent_api.AgentApiError("GET /agent/routines -> HTTP 501", 501)

    monkeypatch.setattr(agent_api, "list_routines", fake_list)
    with pytest.raises(agent_api.AgentApiError):
        await graph.ensure_routine(_cfg())


async def test_push_if_ahead_pushes_only_when_ahead(monkeypatch):
    pushes = []
    state = {"tracked": True, "ahead": 0, "behind": 0}

    async def fake_status(cfg):
        return state

    async def fake_push(cfg):
        pushes.append(1)
        return {"head_sha": "x"}

    monkeypatch.setattr(agent_api, "remote_status", fake_status)
    monkeypatch.setattr(agent_api, "push", fake_push)
    assert await graph.push_if_ahead(_cfg()) is False
    state["ahead"] = 3
    assert await graph.push_if_ahead(_cfg()) is True
    assert pushes == [1]


async def test_push_if_ahead_skips_untracked_remote(monkeypatch):
    async def fake_status(cfg):
        return {"tracked": False, "ahead": 5}

    async def fake_push(cfg):
        raise AssertionError("no push without a tracking ref")

    monkeypatch.setattr(agent_api, "remote_status", fake_status)
    monkeypatch.setattr(agent_api, "push", fake_push)
    assert await graph.push_if_ahead(_cfg()) is False


def test_pull_vault_noop_without_vault_dir():
    calls = []
    assert graph.pull_vault(_cfg(), run=lambda *a, **k: calls.append(a)) is False
    assert calls == []


def test_pull_vault_noop_when_folder_is_not_a_checkout(tmp_path):
    calls = []
    (tmp_path / "Vexa").mkdir()
    assert graph.pull_vault(_cfg(tmp_path), run=lambda *a, **k: calls.append(a)) is False
    assert calls == []


def test_pull_vault_runs_ff_only_pull_in_the_folder(tmp_path):
    (tmp_path / "Vexa" / ".git").mkdir(parents=True)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="Already up to date.\n", stderr="")

    assert graph.pull_vault(_cfg(tmp_path), run=fake_run) is True
    assert calls == [["git", "-C", str(tmp_path / "Vexa"), "pull", "--ff-only"]]


def test_pull_vault_reports_non_ff_failure_without_raising(tmp_path, caplog):
    (tmp_path / "Vexa" / ".git").mkdir(parents=True)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: Not possible to fast-forward")

    with caplog.at_level("ERROR", logger="vexa-summarizer"):
        assert graph.pull_vault(_cfg(tmp_path), run=fake_run) is False
    assert "fast-forward" in caplog.text
