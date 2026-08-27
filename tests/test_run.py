"""End-to-end tests for summarizer.__main__.run_once — all clients faked via monkeypatch.

run_once is the one-pass orchestrator: list completed meetings -> for each not done/poisoned
-> transcript -> min-duration guard -> summarize -> enabled sinks -> mark_done (or
record_failure on any exception). DRY_RUN skips sink writes + mark_done.
"""

from datetime import UTC, datetime

from summarizer import __main__ as m
from summarizer import agent_api, config, graph
from summarizer.types import Meeting, Utterance


def _cfg(tmp_path, obsidian=True, vexa_notes=False, dry_run=False, min_sec=30.0, enabled=True):
    return config.Config(
        summarize_enabled=enabled,
        ai_model="anthropic/claude-sonnet-5",
        vexa_api_url="http://vexa:8056",
        vexa_api_key="k",
        min_transcript_seconds=min_sec,
        obsidian_enabled=obsidian,
        obsidian_sink="mcp",  # existing tests exercise the MCP create_note path
        obsidian_mcp_token="tok" if obsidian else None,
        vexa_notes_enabled=vexa_notes,
        dry_run=dry_run,
        state_dir=tmp_path,
    )


def _meeting(mid=7, native="d7"):
    return Meeting(
        id=mid,
        platform="discord",
        native_meeting_id=native,
        start=datetime(2026, 7, 6, 13, 20, tzinfo=UTC),
        end=datetime(2026, 7, 6, 13, 33, tzinfo=UTC),
    )


def _utts(seconds_each=20.0):
    # two utterances -> total duration 2*seconds_each
    return [
        Utterance("David", 0.0, seconds_each, "We should ship it."),
        Utterance("Renzo", seconds_each, 2 * seconds_each, "Agreed. I'll wire the poller."),
    ]


_GOOD_MD = "\n".join(
    [
        "## TL;DR",
        "x",
        "## Key points",
        "- a",
        "## Talking points",
        "- b",
        "## Decisions",
        "- c",
        "## Action items",
        "- [ ] t — @Renzo",
        "## Open questions",
        "- q",
        "## Full breakdown",
        "stuff",
        "",
    ]
)


def _patch_clients(monkeypatch, meetings, utts=None, summarize_fn=None, create_note_fn=None, write_notes_fn=None):
    async def fake_list(cfg, platforms):
        return meetings

    async def fake_transcript(cfg, meeting):
        return utts if utts is not None else _utts()

    async def fake_summarize(transcript, meta, cfg, call_api=None):
        if summarize_fn is not None:
            return summarize_fn(transcript, meta, cfg)
        return _GOOD_MD

    async def fake_create_note(cfg, path, content):
        if create_note_fn is not None:
            await create_note_fn(cfg, path, content)

    async def fake_write_notes(cfg, meeting, markdown):
        if write_notes_fn is not None:
            await write_notes_fn(cfg, meeting, markdown)

    monkeypatch.setattr(m, "list_completed_meetings", fake_list)
    monkeypatch.setattr(m, "get_transcript", fake_transcript)
    monkeypatch.setattr(m, "summarize", fake_summarize)
    monkeypatch.setattr(m, "create_note", fake_create_note)
    monkeypatch.setattr(m, "write_notes", fake_write_notes)


async def test_run_once_summarizes_writes_note_and_marks_done(tmp_path, monkeypatch):
    created = []

    async def create_note(cfg, path, content):
        created.append((path, content))

    _patch_clients(monkeypatch, [_meeting()], create_note_fn=create_note)
    result = await m.run_once(_cfg(tmp_path))
    assert result.summarized == 1
    assert len(created) == 1
    path, content = created[0]
    assert path == "Meetings/2026-07-06 — David & Renzo (13-20).md"
    assert content.startswith("---\n")
    assert "## TL;DR" in content
    # state marked done
    from summarizer.state import StateStore

    store = StateStore(tmp_path / "state.json")
    assert store.is_done(7) is True


async def test_second_run_is_noop(tmp_path, monkeypatch):
    created = []

    async def create_note(cfg, path, content):
        created.append(1)

    _patch_clients(monkeypatch, [_meeting()], create_note_fn=create_note)
    await m.run_once(_cfg(tmp_path))
    result = await m.run_once(_cfg(tmp_path))
    assert result.summarized == 0
    assert result.idle == 1
    assert len(created) == 1  # not re-created


async def test_low_transcript_meeting_is_skipped_not_summarized(tmp_path, monkeypatch):
    summarized = []
    _patch_clients(
        monkeypatch,
        [_meeting()],
        utts=[Utterance("David", 0.0, 5.0, "hi")],
        summarize_fn=lambda *a: summarized.append(1) or _GOOD_MD,
    )
    result = await m.run_once(_cfg(tmp_path, min_sec=30.0))
    assert result.skipped == 1
    assert result.summarized == 0
    assert summarized == []  # no LLM run
    from summarizer.state import StateStore

    assert StateStore(tmp_path / "state.json").is_done(7) is True  # skipped = done, not retried


async def test_failure_increments_attempts_and_poisons_after_five(tmp_path, monkeypatch):
    async def boom(transcript, meta, cfg, call_api=None):
        raise RuntimeError("llm down")

    _patch_clients(monkeypatch, [_meeting()], summarize_fn=None)
    monkeypatch.setattr(m, "summarize", boom)
    for _ in range(4):
        result = await m.run_once(_cfg(tmp_path))
        assert result.failed == 1
    from summarizer.state import StateStore

    assert StateStore(tmp_path / "state.json").is_poisoned(7) is False
    await m.run_once(_cfg(tmp_path))  # 5th failure poisons
    store = StateStore(tmp_path / "state.json")
    assert store.is_poisoned(7) is True


async def test_dry_run_writes_nothing_and_does_not_mark(tmp_path, monkeypatch):
    created = []

    async def create_note(cfg, path, content):
        created.append(1)

    _patch_clients(monkeypatch, [_meeting()], create_note_fn=create_note)
    result = await m.run_once(_cfg(tmp_path, dry_run=True))
    assert result.summarized == 1
    assert created == []  # no sink write
    from summarizer.state import StateStore

    assert StateStore(tmp_path / "state.json").is_done(7) is False  # not marked


async def test_disabled_obsidian_skips_create_note_but_writes_vexa_notes(tmp_path, monkeypatch):
    created = []
    written = []

    async def create_note(cfg, path, content):
        created.append(1)

    async def write_notes(cfg, meeting, markdown):
        written.append(markdown)

    _patch_clients(monkeypatch, [_meeting()], create_note_fn=create_note, write_notes_fn=write_notes)
    result = await m.run_once(_cfg(tmp_path, obsidian=False, vexa_notes=True))
    assert result.summarized == 1
    assert created == []
    assert len(written) == 1
    assert "## TL;DR" in written[0]
    from summarizer.state import StateStore

    assert StateStore(tmp_path / "state.json").is_done(7) is True


async def test_summarize_disabled_short_circuits_no_vexa_call(tmp_path, monkeypatch):
    async def must_not_be_called(*a, **k):
        raise AssertionError("list_completed_meetings should not be called when disabled")

    monkeypatch.setattr(m, "list_completed_meetings", must_not_be_called)
    result = await m.run_once(_cfg(tmp_path, enabled=False))
    assert result.summarized == 0


async def test_run_once_fs_sink_routes_to_write_note_fs(tmp_path, monkeypatch):
    """When obsidian_sink=fs, run_once dispatches to write_note_fs (not create_note)
    and state.mark_done records the note path."""
    fs_calls = []
    mcp_calls = []

    async def fake_write_note_fs(cfg, path, content):
        fs_calls.append((path, content))

    async def fake_create_note(cfg, path, content):
        mcp_calls.append((path, content))

    _patch_clients(monkeypatch, [_meeting()], create_note_fn=fake_create_note)
    monkeypatch.setattr(m, "write_note_fs", fake_write_note_fs)

    cfg = _cfg(tmp_path)
    cfg.obsidian_sink = "fs"
    cfg.vault_dir = tmp_path  # fs sink target
    result = await m.run_once(cfg)
    assert result.summarized == 1
    assert len(fs_calls) == 1
    assert mcp_calls == []  # MCP sink not invoked
    path, content = fs_calls[0]
    assert path == "Meetings/2026-07-06 — David & Renzo (13-20).md"
    assert content.startswith("---\n")
    # state.mark_done recorded the path
    from summarizer.state import StateStore

    assert StateStore(tmp_path / "state.json").is_done(7) is True


def _graph_cfg(tmp_path, dry_run=False, vault=False):
    cfg = config.Config(
        bridge_mode="graph",
        vexa_api_url="http://vexa:8056",
        vexa_api_key="k",
        min_transcript_seconds=30.0,
        dry_run=dry_run,
        state_dir=tmp_path,
    )
    if vault:
        cfg.vault_dir = tmp_path
    return cfg


def _patch_graph(
    monkeypatch,
    meetings,
    utts=None,
    upload_fn=None,
    ensure_fn=None,
    push_fn=None,
    pull_fn=None,
    trigger_fn=None,
    head_fn=None,
    wait_fn=None,
):
    async def fake_list(cfg, platforms):
        return meetings

    async def fake_transcript(cfg, meeting):
        return utts if utts is not None else _utts()

    async def fake_upload(cfg, filename, content):
        if upload_fn is not None:
            return await upload_fn(cfg, filename, content)
        return f"uploads/abc-{filename}"

    async def fake_ensure(cfg):
        if ensure_fn is not None:
            return await ensure_fn(cfg)
        return True

    async def fake_push(cfg):
        if push_fn is not None:
            return await push_fn(cfg)
        return False

    def fake_pull(cfg, run=None):
        if pull_fn is not None:
            return pull_fn(cfg)
        return False

    async def fake_trigger(cfg):
        if trigger_fn is not None:
            return await trigger_fn(cfg)
        return None

    async def fake_head(cfg):
        if head_fn is not None:
            return await head_fn(cfg)
        return "abc"

    async def fake_wait(cfg, base, *, timeout_seconds, interval_seconds=15.0, sleep=None):
        if wait_fn is not None:
            return await wait_fn(cfg, base)
        return True

    async def must_not_summarize(*a, **k):
        raise AssertionError("graph mode must not call the LLM")

    monkeypatch.setattr(m, "list_completed_meetings", fake_list)
    monkeypatch.setattr(m, "get_transcript", fake_transcript)
    monkeypatch.setattr(m, "upload", fake_upload)
    monkeypatch.setattr(m, "ensure_routine", fake_ensure)
    # push_if_ahead / pull_vault / trigger_routine_now / wait_for_commit are now called from
    # inside graph.finalize_graph_pass, not from __main__ directly, so patch them where the
    # calls actually happen (mirrors summarizer.graph's own test module).
    monkeypatch.setattr(graph, "push_if_ahead", fake_push)
    monkeypatch.setattr(graph, "pull_vault", fake_pull)
    monkeypatch.setattr(graph, "trigger_routine_now", fake_trigger)
    monkeypatch.setattr(agent_api, "git_head", fake_head)
    monkeypatch.setattr(graph, "wait_for_commit", fake_wait)
    monkeypatch.setattr(m, "summarize", must_not_summarize)
    monkeypatch.setattr(m, "_routine_ready", False)


async def test_graph_mode_uploads_transcript_and_marks_done(tmp_path, monkeypatch):
    uploads = []

    async def upload(cfg, filename, content):
        uploads.append((filename, content))
        return "uploads/abc-" + filename

    _patch_graph(monkeypatch, [_meeting()], upload_fn=upload)
    result = await m.run_once(_graph_cfg(tmp_path))
    assert result.uploaded == 1
    filename, content = uploads[0]
    assert filename == "2026-07-06-discord-d7.md"
    assert "type: transcript" in content
    assert "[00:00:00] David: We should ship it." in content
    from summarizer.state import StateStore

    store = StateStore(tmp_path / "state.json")
    assert store.is_done(7) is True
    assert store.get(7).note_path == "uploads/abc-2026-07-06-discord-d7.md"


async def test_graph_mode_second_run_is_idle(tmp_path, monkeypatch):
    uploads = []

    async def upload(cfg, filename, content):
        uploads.append(1)
        return "uploads/x"

    _patch_graph(monkeypatch, [_meeting()], upload_fn=upload)
    await m.run_once(_graph_cfg(tmp_path))
    result = await m.run_once(_graph_cfg(tmp_path))
    assert result.idle == 1
    assert uploads == [1]


async def test_graph_mode_upload_failure_records_failure_not_done(tmp_path, monkeypatch):
    from summarizer.agent_api import AgentApiError

    async def upload(cfg, filename, content):
        raise AgentApiError("POST upload -> HTTP 502", 502)

    _patch_graph(monkeypatch, [_meeting()], upload_fn=upload)
    result = await m.run_once(_graph_cfg(tmp_path))
    assert result.failed == 1
    from summarizer.state import StateStore

    assert StateStore(tmp_path / "state.json").is_done(7) is False


async def test_graph_mode_low_transcript_is_skipped(tmp_path, monkeypatch):
    uploads = []

    async def upload(cfg, filename, content):
        uploads.append(1)
        return "uploads/x"

    _patch_graph(monkeypatch, [_meeting()], utts=[Utterance("David", 0.0, 5.0, "hi")], upload_fn=upload)
    result = await m.run_once(_graph_cfg(tmp_path))
    assert result.skipped == 1
    assert uploads == []


async def test_graph_mode_dry_run_uploads_nothing(tmp_path, monkeypatch):
    uploads = []

    async def upload(cfg, filename, content):
        uploads.append(1)
        return "uploads/x"

    _patch_graph(monkeypatch, [_meeting()], upload_fn=upload)
    result = await m.run_once(_graph_cfg(tmp_path, dry_run=True))
    assert result.uploaded == 1
    assert uploads == []
    from summarizer.state import StateStore

    assert StateStore(tmp_path / "state.json").is_done(7) is False


async def test_graph_mode_ensures_routine_pushes_and_pulls_each_pass(tmp_path, monkeypatch):
    order = []

    async def ensure(cfg):
        order.append("ensure")
        return True

    async def push(cfg):
        order.append("push")
        return True

    def pull(cfg):
        order.append("pull")
        return True

    _patch_graph(monkeypatch, [], ensure_fn=ensure, push_fn=push, pull_fn=pull)
    await m.run_once(_graph_cfg(tmp_path, vault=True))
    assert order == ["ensure", "push", "pull"]


async def test_graph_mode_triggers_routine_after_uploads(tmp_path, monkeypatch):
    order = []

    async def upload(cfg, filename, content):
        return "uploads/x"

    async def trigger(cfg):
        order.append("trigger")

    async def push(cfg):
        order.append("push")
        return True

    _patch_graph(monkeypatch, [_meeting()], upload_fn=upload, trigger_fn=trigger, push_fn=push)
    result = await m.run_once(_graph_cfg(tmp_path))
    assert result.uploaded == 1
    assert order == ["trigger", "push"]


async def test_graph_mode_no_trigger_when_nothing_uploaded(tmp_path, monkeypatch):
    triggered = []

    async def trigger(cfg):
        triggered.append(1)

    _patch_graph(monkeypatch, [], trigger_fn=trigger)
    result = await m.run_once(_graph_cfg(tmp_path))
    assert result.uploaded == 0
    assert triggered == []


async def test_graph_mode_no_trigger_on_dry_run(tmp_path, monkeypatch):
    triggered = []

    async def trigger(cfg):
        triggered.append(1)

    _patch_graph(monkeypatch, [_meeting()], trigger_fn=trigger)
    result = await m.run_once(_graph_cfg(tmp_path, dry_run=True))
    assert result.uploaded == 1
    assert triggered == []


async def test_graph_mode_trigger_failure_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    async def trigger(cfg):
        raise RuntimeError("connection refused")

    _patch_graph(monkeypatch, [_meeting()], trigger_fn=trigger)
    with caplog.at_level("WARNING", logger="vexa-summarizer"):
        result = await m.run_once(_graph_cfg(tmp_path))
    assert result.failed == 0
    assert "routine" in caplog.text


async def test_graph_mode_dry_run_does_not_create_the_routine(tmp_path, monkeypatch):
    async def ensure(cfg):
        raise AssertionError("DRY_RUN must not touch Vexa's routines")

    _patch_graph(monkeypatch, [_meeting()], ensure_fn=ensure)
    result = await m.run_once(_graph_cfg(tmp_path, dry_run=True))
    assert result.uploaded == 1


async def test_graph_mode_ensures_routine_once_per_process(tmp_path, monkeypatch):
    calls = []

    async def ensure(cfg):
        calls.append(1)
        return True

    _patch_graph(monkeypatch, [_meeting()], ensure_fn=ensure)
    cfg = _graph_cfg(tmp_path)
    await m.run_once(cfg)
    await m.run_once(cfg)
    assert calls == [1]


async def test_graph_mode_routine_failure_retries_next_pass(tmp_path, monkeypatch):
    calls = []

    async def ensure(cfg):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("connection refused")
        return True

    upload_calls = []

    async def upload(cfg, filename, content):
        upload_calls.append(1)
        return "uploads/x"

    _patch_graph(monkeypatch, [_meeting()], ensure_fn=ensure, upload_fn=upload)
    cfg = _graph_cfg(tmp_path)
    result1 = await m.run_once(cfg)
    # second pass: meeting 7 is already marked done, so use a fresh meeting id
    _patch_graph(monkeypatch, [_meeting(mid=8, native="d8")], ensure_fn=ensure, upload_fn=upload)
    result2 = await m.run_once(cfg)
    assert calls == [1, 1]
    assert result1.uploaded == 1
    assert result2.uploaded == 1


async def test_graph_mode_routine_501_is_logged_and_uploads_continue(tmp_path, monkeypatch, caplog):
    from summarizer.agent_api import AgentApiError

    async def ensure(cfg):
        raise AgentApiError("POST /agent/routines -> HTTP 501", 501)

    _patch_graph(monkeypatch, [_meeting()], ensure_fn=ensure)
    with caplog.at_level("WARNING", logger="vexa-summarizer"):
        result = await m.run_once(_graph_cfg(tmp_path))
    assert result.uploaded == 1
    assert "routine" in caplog.text


async def test_graph_mode_routine_transport_error_is_logged_and_uploads_continue(tmp_path, monkeypatch, caplog):
    async def ensure(cfg):
        raise RuntimeError("connection refused")

    _patch_graph(monkeypatch, [_meeting()], ensure_fn=ensure)
    with caplog.at_level("WARNING", logger="vexa-summarizer"):
        result = await m.run_once(_graph_cfg(tmp_path))
    assert result.uploaded == 1
    assert "routine" in caplog.text


async def test_graph_mode_push_error_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    from summarizer.agent_api import AgentApiError

    async def push(cfg):
        raise AgentApiError("POST /agent/workspace/push -> HTTP 502: diverged", 502)

    _patch_graph(monkeypatch, [], push_fn=push)
    with caplog.at_level("WARNING", logger="vexa-summarizer"):
        result = await m.run_once(_graph_cfg(tmp_path))
    assert result.failed == 0
    assert "push" in caplog.text


async def test_graph_mode_push_transport_error_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    async def push(cfg):
        raise RuntimeError("timeout")

    _patch_graph(monkeypatch, [], push_fn=push)
    with caplog.at_level("WARNING", logger="vexa-summarizer"):
        result = await m.run_once(_graph_cfg(tmp_path))
    assert result.failed == 0
    assert "push" in caplog.text


# --- process_event_meeting (webhook path) -------------------------------


async def test_process_event_meeting_note_mode_marks_done(tmp_path, monkeypatch):
    created = []

    async def create_note(cfg, path, content):
        created.append((path, content))

    _patch_clients(monkeypatch, [], create_note_fn=create_note)
    result = await m.process_event_meeting(_cfg(tmp_path), _meeting())
    assert result.summarized == 1
    assert len(created) == 1
    from summarizer.state import StateStore

    assert StateStore(tmp_path / "state.json").is_done(7) is True


async def test_process_event_meeting_low_transcript_leaves_meeting_for_poll(tmp_path, monkeypatch):
    """A webhook can fire before the last transcript flush; a below-minimum transcript must not
    be permanently marked skipped, so the next poll pass still picks the meeting up."""
    _patch_clients(monkeypatch, [], utts=[Utterance("David", 0.0, 5.0, "hi")])
    result = await m.process_event_meeting(_cfg(tmp_path, min_sec=30.0), _meeting())
    assert result.skipped == 1
    from summarizer.state import StateStore

    store = StateStore(tmp_path / "state.json")
    assert store.get(7) is None
    assert store.is_done(7) is False


async def test_process_event_meeting_idle_when_already_done(tmp_path, monkeypatch):
    _patch_clients(monkeypatch, [_meeting()])
    cfg = _cfg(tmp_path)
    await m.run_once(cfg)  # the poll gets there first
    result = await m.process_event_meeting(cfg, _meeting())
    assert result.idle == 1


async def test_process_event_meeting_graph_mode_uploads_triggers_pushes_and_pulls(tmp_path, monkeypatch):
    order = []

    async def upload(cfg, filename, content):
        return "uploads/x"

    async def trigger(cfg):
        order.append("trigger")

    async def wait(cfg, base):
        order.append("wait")
        return True

    async def push(cfg):
        order.append("push")
        return True

    def pull(cfg):
        order.append("pull")
        return True

    _patch_graph(monkeypatch, [], upload_fn=upload, trigger_fn=trigger, wait_fn=wait, push_fn=push, pull_fn=pull)
    cfg = _graph_cfg(tmp_path, vault=True)
    result = await m.process_event_meeting(cfg, _meeting())
    assert result.uploaded == 1
    assert order == ["trigger", "wait", "push", "pull"]
    from summarizer.state import StateStore

    assert StateStore(tmp_path / "state.json").is_done(7) is True


async def test_process_event_meeting_wait_timeout_still_pushes_once_and_warns(tmp_path, monkeypatch, caplog):
    order = []

    async def upload(cfg, filename, content):
        return "uploads/x"

    async def trigger(cfg):
        order.append("trigger")

    async def wait(cfg, base):
        order.append("wait")
        return False

    async def push(cfg):
        order.append("push")
        return True

    def pull(cfg):
        order.append("pull")
        return True

    _patch_graph(monkeypatch, [], upload_fn=upload, trigger_fn=trigger, wait_fn=wait, push_fn=push, pull_fn=pull)
    cfg = _graph_cfg(tmp_path, vault=True)
    with caplog.at_level("WARNING", logger="vexa-summarizer"):
        result = await m.process_event_meeting(cfg, _meeting())
    assert result.uploaded == 1
    assert order == ["trigger", "wait", "push", "pull"]  # push still attempted once, even on timeout
    assert "commit not seen" in caplog.text


async def test_process_event_meeting_wait_not_called_when_nothing_uploaded(tmp_path, monkeypatch):
    waited = []

    async def wait(cfg, base):
        waited.append(1)
        return True

    _patch_graph(monkeypatch, [], utts=[Utterance("David", 0.0, 5.0, "hi")], wait_fn=wait)
    result = await m.process_event_meeting(_graph_cfg(tmp_path), _meeting())
    assert result.uploaded == 0
    assert waited == []


async def test_process_event_meeting_wait_not_called_on_dry_run(tmp_path, monkeypatch):
    waited = []

    async def wait(cfg, base):
        waited.append(1)
        return True

    _patch_graph(monkeypatch, [], wait_fn=wait)
    result = await m.process_event_meeting(_graph_cfg(tmp_path, dry_run=True), _meeting())
    assert result.uploaded == 1
    assert waited == []


async def test_process_event_meeting_passes_base_sha_and_cfg_timeouts_to_wait(tmp_path, monkeypatch):
    captured = {}

    async def head(cfg):
        return "base-sha"

    async def fake_wait(cfg, base, *, timeout_seconds, interval_seconds, sleep=None):
        captured["base"] = base
        captured["timeout"] = timeout_seconds
        captured["interval"] = interval_seconds
        return True

    _patch_graph(monkeypatch, [], head_fn=head)
    monkeypatch.setattr(graph, "wait_for_commit", fake_wait)
    cfg = _graph_cfg(tmp_path)
    cfg.webhook_commit_wait_seconds = 42.0
    cfg.webhook_commit_poll_seconds = 3.0
    await m.process_event_meeting(cfg, _meeting())
    assert captured == {"base": "base-sha", "timeout": 42.0, "interval": 3.0}


async def test_process_event_meeting_git_head_error_before_trigger_is_best_effort(tmp_path, monkeypatch, caplog):
    captured = {}

    async def head(cfg):
        raise RuntimeError("gateway down")

    async def fake_wait(cfg, base, *, timeout_seconds, interval_seconds, sleep=None):
        captured["base"] = base
        return True

    _patch_graph(monkeypatch, [], head_fn=head)
    monkeypatch.setattr(graph, "wait_for_commit", fake_wait)
    with caplog.at_level("WARNING", logger="vexa-summarizer"):
        result = await m.process_event_meeting(_graph_cfg(tmp_path), _meeting())
    assert result.uploaded == 1
    assert captured["base"] is None


async def test_process_event_meeting_graph_mode_dry_run_skips_post_steps(tmp_path, monkeypatch):
    triggered = []

    async def trigger(cfg):
        triggered.append(1)

    _patch_graph(monkeypatch, [], trigger_fn=trigger)
    result = await m.process_event_meeting(_graph_cfg(tmp_path, dry_run=True), _meeting())
    assert result.uploaded == 1
    assert triggered == []


async def test_process_event_meeting_graph_mode_no_trigger_when_nothing_uploaded(tmp_path, monkeypatch):
    triggered = []

    async def trigger(cfg):
        triggered.append(1)

    _patch_graph(monkeypatch, [], utts=[Utterance("David", 0.0, 5.0, "hi")], trigger_fn=trigger)
    result = await m.process_event_meeting(_graph_cfg(tmp_path), _meeting())
    assert result.uploaded == 0
    assert result.skipped == 1
    assert triggered == []


# --- both graph-mode call sites route through the shared finalize helper ------------------


async def test_poll_and_event_paths_both_call_the_shared_finalize_helper(tmp_path, monkeypatch):
    """The poll pass (_run_once_graph, via run_once) and the webhook event path
    (process_event_meeting) must both delegate the trigger/wait/push/pull sequence to
    graph.finalize_graph_pass rather than duplicating it, with the poll path taking the
    defaults (no wait for the agent's commit) and the event path opting into the wait."""
    calls = []

    async def fake_finalize(cfg, uploaded, *, wait_for_agent_commit=False):
        calls.append((uploaded, wait_for_agent_commit))

    async def upload(cfg, filename, content):
        return "uploads/x"

    _patch_graph(monkeypatch, [_meeting()], upload_fn=upload)
    monkeypatch.setattr(m, "finalize_graph_pass", fake_finalize)

    result = await m.run_once(_graph_cfg(tmp_path))
    assert result.uploaded == 1
    assert calls == [(1, False)]

    calls.clear()
    result = await m.process_event_meeting(_graph_cfg(tmp_path), _meeting(mid=8, native="d8"))
    assert result.uploaded == 1
    assert calls == [(1, True)]
