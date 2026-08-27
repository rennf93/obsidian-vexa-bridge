"""Agent API client: request shapes and error mapping, HTTP seams faked."""

import pytest

from summarizer import agent_api
from summarizer.config import Config


def _cfg():
    return Config(vexa_api_url="http://vexa:8056", vexa_api_key="k")


async def test_upload_posts_multipart_and_returns_workspace_path(monkeypatch):
    calls = []

    async def fake_multipart(url, headers, field, filename, content):
        calls.append((url, headers, field, filename, content))
        return 200, {
            "uploaded": [{"name": "abc-2026-07-06-discord-x.md", "path": "uploads/abc-2026-07-06-discord-x.md"}]
        }

    monkeypatch.setattr(agent_api, "_http_post_multipart", fake_multipart)
    path = await agent_api.upload(_cfg(), "2026-07-06-discord-x.md", "---\ntype: transcript\n---\n")
    assert path == "uploads/abc-2026-07-06-discord-x.md"
    url, headers, field, filename, content = calls[0]
    assert url == "http://vexa:8056/agent/workspace/upload"
    assert headers["X-API-Key"] == "k"
    assert field == "files"
    assert filename == "2026-07-06-discord-x.md"
    assert content.startswith("---\n")


async def test_upload_accepts_live_files_key(monkeypatch):
    # The shape Vexa 0.12.22 actually returns (verified live 2026-08-27).
    async def fake_multipart(url, headers, field, filename, content):
        return 200, {"files": [{"name": "abc-n.md", "path": "uploads/abc-n.md"}]}

    monkeypatch.setattr(agent_api, "_http_post_multipart", fake_multipart)
    assert await agent_api.upload(_cfg(), "n.md", "x") == "uploads/abc-n.md"


async def test_upload_accepts_bare_list_response(monkeypatch):
    async def fake_multipart(url, headers, field, filename, content):
        return 200, [{"name": "n", "path": "uploads/n"}]

    monkeypatch.setattr(agent_api, "_http_post_multipart", fake_multipart)
    assert await agent_api.upload(_cfg(), "n", "x") == "uploads/n"


async def test_upload_non_2xx_raises_with_status(monkeypatch):
    async def fake_multipart(url, headers, field, filename, content):
        return 413, {"detail": "exceeds 25MB"}

    monkeypatch.setattr(agent_api, "_http_post_multipart", fake_multipart)
    with pytest.raises(agent_api.AgentApiError) as exc:
        await agent_api.upload(_cfg(), "n", "x")
    assert exc.value.status == 413


async def test_list_routines_reads_routines_key_or_list(monkeypatch):
    async def fake_get(url, headers):
        assert url == "http://vexa:8056/agent/routines"
        return 200, {"routines": [{"name": "meeting-to-graph", "cron": "*/5 * * * *"}]}

    monkeypatch.setattr(agent_api, "_http_get_json", fake_get)
    assert await agent_api.list_routines(_cfg()) == [{"name": "meeting-to-graph", "cron": "*/5 * * * *"}]

    async def fake_get_list(url, headers):
        return 200, [{"name": "x"}]

    monkeypatch.setattr(agent_api, "_http_get_json", fake_get_list)
    assert await agent_api.list_routines(_cfg()) == [{"name": "x"}]


async def test_create_routine_posts_documented_body(monkeypatch):
    calls = []

    async def fake_post(url, headers, body):
        calls.append((url, body))
        return 201, {"routine": {"name": body["name"]}, "job_id": "job_1", "ran_now": body["run_now"]}

    monkeypatch.setattr(agent_api, "_http_post_json", fake_post)
    out = await agent_api.create_routine(_cfg(), "meeting-to-graph", "*/5 * * * *", "do it", run_now=False)
    assert out["job_id"] == "job_1"
    url, body = calls[0]
    assert url == "http://vexa:8056/agent/routines"
    assert body == {"name": "meeting-to-graph", "cron": "*/5 * * * *", "prompt": "do it", "run_now": False}


async def test_create_routine_501_maps_to_error_with_status(monkeypatch):
    async def fake_post(url, headers, body):
        return 501, {"detail": "scheduler not wired"}

    monkeypatch.setattr(agent_api, "_http_post_json", fake_post)
    with pytest.raises(agent_api.AgentApiError) as exc:
        await agent_api.create_routine(_cfg(), "n", "* * * * *", "p")
    assert exc.value.status == 501


async def test_remote_status_and_push_hit_documented_routes(monkeypatch):
    seen = []

    async def fake_get(url, headers):
        seen.append(("GET", url))
        return 200, {"tracked": True, "ahead": 2, "behind": 0, "remote": "origin", "branch": "main"}

    async def fake_post(url, headers, body):
        seen.append(("POST", url, body))
        return 200, {"remote": "origin", "branch": "main", "head_sha": "deadbeef"}

    monkeypatch.setattr(agent_api, "_http_get_json", fake_get)
    monkeypatch.setattr(agent_api, "_http_post_json", fake_post)
    st = await agent_api.remote_status(_cfg())
    assert st["ahead"] == 2
    out = await agent_api.push(_cfg())
    assert out["head_sha"] == "deadbeef"
    assert seen == [
        ("GET", "http://vexa:8056/agent/workspace/git-remote-status"),
        ("POST", "http://vexa:8056/agent/workspace/push", {}),
    ]


async def test_push_502_is_an_error_with_status(monkeypatch):
    async def fake_post(url, headers, body):
        return 502, {"detail": "diverged"}

    monkeypatch.setattr(agent_api, "_http_post_json", fake_post)
    with pytest.raises(agent_api.AgentApiError) as exc:
        await agent_api.push(_cfg())
    assert exc.value.status == 502


async def test_delete_routine_hits_documented_route_and_maps_404(monkeypatch):
    seen = []

    async def fake_delete(url, headers):
        seen.append(url)
        return 404, {"detail": "not found"}

    monkeypatch.setattr(agent_api, "_http_delete_json", fake_delete)
    with pytest.raises(agent_api.AgentApiError) as exc:
        await agent_api.delete_routine(_cfg(), "rt_old")
    assert exc.value.status == 404
    assert seen == ["http://vexa:8056/agent/routines/rt_old"]
