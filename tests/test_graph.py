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
    assert created == [("meeting-to-graph", "0 * * * *", graph.ROUTINE_PROMPT, False)]


async def test_ensure_routine_is_a_noop_when_present(monkeypatch):
    async def fake_list(cfg):
        return [{"name": "meeting-to-graph", "cron": "*/5 * * * *"}]

    async def fake_create(cfg, name, cron, prompt, run_now=False):
        raise AssertionError("must not create a duplicate")

    monkeypatch.setattr(agent_api, "list_routines", fake_list)
    monkeypatch.setattr(agent_api, "create_routine", fake_create)
    cfg = _cfg()
    cfg.graph_routine_cron = "*/5 * * * *"
    assert await graph.ensure_routine(cfg) is True


async def test_ensure_routine_replaces_routine_with_stale_cron(monkeypatch):
    calls = []

    async def fake_list(cfg):
        return [{"id": "rt_old", "name": "meeting-to-graph", "cron": "*/15 * * * *"}]

    async def fake_delete(cfg, routine_id):
        calls.append(("delete", routine_id))

    async def fake_create(cfg, name, cron, prompt, run_now=False):
        calls.append(("create", name, cron, prompt, run_now))
        return {"job_id": "job_2"}

    monkeypatch.setattr(agent_api, "list_routines", fake_list)
    monkeypatch.setattr(agent_api, "delete_routine", fake_delete)
    monkeypatch.setattr(agent_api, "create_routine", fake_create)
    assert await graph.ensure_routine(_cfg()) is True
    assert calls == [
        ("delete", "rt_old"),
        ("create", "meeting-to-graph", "0 * * * *", graph.ROUTINE_PROMPT, False),
    ]


async def test_ensure_routine_keeps_matching_cron(monkeypatch):
    async def fake_list(cfg):
        return [{"id": "rt_1", "name": "meeting-to-graph", "cron": "0 * * * *"}]

    async def fake_delete(cfg, routine_id):
        raise AssertionError("must not delete when cron matches")

    async def fake_create(cfg, name, cron, prompt, run_now=False):
        raise AssertionError("must not create when cron matches")

    monkeypatch.setattr(agent_api, "list_routines", fake_list)
    monkeypatch.setattr(agent_api, "delete_routine", fake_delete)
    monkeypatch.setattr(agent_api, "create_routine", fake_create)
    assert await graph.ensure_routine(_cfg()) is True


async def test_ensure_routine_propagates_api_errors(monkeypatch):
    async def fake_list(cfg):
        raise agent_api.AgentApiError("GET /agent/routines -> HTTP 501", 501)

    monkeypatch.setattr(agent_api, "list_routines", fake_list)
    with pytest.raises(agent_api.AgentApiError):
        await graph.ensure_routine(_cfg())


async def test_trigger_routine_now_posts_run_now(monkeypatch):
    created = []

    async def fake_create(cfg, name, cron, prompt, run_now=False):
        created.append((name, cron, prompt, run_now))
        return {"job_id": "job_1"}

    monkeypatch.setattr(agent_api, "create_routine", fake_create)
    await graph.trigger_routine_now(_cfg())
    assert created == [("meeting-to-graph", "0 * * * *", graph.ROUTINE_PROMPT, True)]


async def test_trigger_routine_now_propagates_api_errors(monkeypatch):
    async def fake_create(cfg, name, cron, prompt, run_now=False):
        raise agent_api.AgentApiError("POST /agent/routines -> HTTP 502", 502)

    monkeypatch.setattr(agent_api, "create_routine", fake_create)
    with pytest.raises(agent_api.AgentApiError):
        await graph.trigger_routine_now(_cfg())


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


def test_pull_vault_reports_missing_git_without_raising(tmp_path, caplog):
    (tmp_path / "Vexa" / ".git").mkdir(parents=True)

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("git")

    with caplog.at_level("ERROR", logger="vexa-summarizer"):
        assert graph.pull_vault(_cfg(tmp_path), run=fake_run) is False
    assert "git" in caplog.text


# --- wait_for_commit -----------------------------------------------------


async def _fake_sleep(seconds):
    pass


async def test_wait_for_commit_returns_true_when_sha_changes(monkeypatch):
    heads = iter(["base", "base", "new"])

    async def fake_head(cfg):
        return next(heads)

    monkeypatch.setattr(agent_api, "git_head", fake_head)
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    result = await graph.wait_for_commit(_cfg(), "base", timeout_seconds=60, interval_seconds=10, sleep=fake_sleep)
    assert result is True
    assert sleeps == [10, 10]


async def test_wait_for_commit_treats_none_base_as_any_commit(monkeypatch):
    heads = iter([None, "abc"])

    async def fake_head(cfg):
        return next(heads)

    monkeypatch.setattr(agent_api, "git_head", fake_head)
    result = await graph.wait_for_commit(_cfg(), None, timeout_seconds=60, interval_seconds=15, sleep=_fake_sleep)
    assert result is True


async def test_wait_for_commit_returns_false_on_timeout(monkeypatch):
    async def fake_head(cfg):
        return "base"

    monkeypatch.setattr(agent_api, "git_head", fake_head)
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    result = await graph.wait_for_commit(_cfg(), "base", timeout_seconds=30, interval_seconds=10, sleep=fake_sleep)
    assert result is False
    assert sleeps == [10, 10, 10]


async def test_wait_for_commit_zero_timeout_returns_immediately(monkeypatch):
    async def fake_head(cfg):
        return "base"

    monkeypatch.setattr(agent_api, "git_head", fake_head)
    assert await graph.wait_for_commit(_cfg(), "base", timeout_seconds=0) is False


async def test_wait_for_commit_treats_api_errors_as_no_change_and_keeps_polling(monkeypatch):
    calls = {"n": 0}

    async def fake_head(cfg):
        calls["n"] += 1
        if calls["n"] < 3:
            raise agent_api.AgentApiError("GET /agent/workspace/git -> HTTP 502", 502)
        return "new"

    monkeypatch.setattr(agent_api, "git_head", fake_head)
    result = await graph.wait_for_commit(_cfg(), "base", timeout_seconds=60, interval_seconds=10, sleep=_fake_sleep)
    assert result is True
    assert calls["n"] == 3


async def test_wait_for_commit_logs_info_when_commit_seen(monkeypatch, caplog):
    heads = iter(["base", "new"])

    async def fake_head(cfg):
        return next(heads)

    monkeypatch.setattr(agent_api, "git_head", fake_head)
    with caplog.at_level("INFO", logger="vexa-summarizer"):
        result = await graph.wait_for_commit(_cfg(), "base", timeout_seconds=60, interval_seconds=10, sleep=_fake_sleep)
    assert result is True
    assert "commit" in caplog.text
