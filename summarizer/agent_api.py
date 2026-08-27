"""Client for the seven Vexa Agent API routes graph mode needs, via the gateway's /agent/* proxy.

Routes (Vexa 0.12.x, self-hosted compose only; hosted and Kubernetes answer 502 on /agent/*):
  POST   /agent/workspace/upload            multipart field "files" -> {"files": [{name, path}]}
                                            (live 0.12.22; an "uploaded" key or a bare list is accepted too)
  GET    /agent/routines                    -> {"routines": [...]} (a bare list is also accepted)
  POST   /agent/routines                    {name, cron, prompt, run_now} -> 201; 501 if no scheduler
  DELETE /agent/routines/{routine_id}       -> 200/204; 404 if the routine is already gone
  GET    /agent/workspace/git-remote-status -> {tracked, ahead, behind, remote, branch, ...}
  POST   /agent/workspace/push              {} -> uses the saved token; 400 none saved; 502 diverged
  GET    /agent/workspace/git              -> {branch, changes, commits: [{sha, msg, when, files}, ...]}

Vexa derives a routine's id from (subject, name, cron), so changing GRAPH_ROUTINE_CRON makes a
different id, not an update to the old one; summarizer.graph.ensure_routine deletes the stale
routine (by the "id" a GET /agent/routines card carries) before creating the new one.

Same X-API-Key auth as summarizer.vexa. HTTP is behind four async seams so tests fake them
without aiohttp.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from summarizer.config import Config


class AgentApiError(RuntimeError):
    """A non-2xx from the Agent API; .status carries the HTTP status for callers that branch on it."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def _headers(cfg: Config) -> dict[str, str]:
    return {"X-API-Key": cfg.vexa_api_key, "Accept": "application/json"}


def _check(status: int, data: Any, what: str) -> None:
    if status < 200 or status >= 300:
        raise AgentApiError(f"{what} -> HTTP {status}: {str(data)[:200]}", status)


async def upload(cfg: Config, filename: str, content: str) -> str:
    """Upload one text file into the workspace inbox; returns its workspace-relative path."""
    status, data = await _http_post_multipart(
        f"{cfg.vexa_api_url}/agent/workspace/upload", _headers(cfg), "files", filename, content
    )
    _check(status, data, "POST /agent/workspace/upload")
    # Live 0.12.22 answers {"files": [{name, path}]}; accept "uploaded" and a bare list too.
    rows = (data.get("files") or data.get("uploaded")) if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict) or "path" not in rows[0]:
        raise AgentApiError(f"unexpected upload response: {str(data)[:200]}", status)
    return str(rows[0]["path"])


async def list_routines(cfg: Config) -> list[dict[str, Any]]:
    status, data = await _http_get_json(f"{cfg.vexa_api_url}/agent/routines", _headers(cfg))
    _check(status, data, "GET /agent/routines")
    rows = data.get("routines") if isinstance(data, dict) else data
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


async def create_routine(cfg: Config, name: str, cron: str, prompt: str, run_now: bool = False) -> dict[str, Any]:
    body = {"name": name, "cron": cron, "prompt": prompt, "run_now": run_now}
    status, data = await _http_post_json(f"{cfg.vexa_api_url}/agent/routines", _headers(cfg), body)
    _check(status, data, "POST /agent/routines")
    return data if isinstance(data, dict) else {}


async def delete_routine(cfg: Config, routine_id: str) -> None:
    """Delete one routine by id, e.g. a stale entry left behind by a GRAPH_ROUTINE_CRON change."""
    status, data = await _http_delete_json(f"{cfg.vexa_api_url}/agent/routines/{routine_id}", _headers(cfg))
    _check(status, data, "DELETE /agent/routines")


async def remote_status(cfg: Config) -> dict[str, Any]:
    status, data = await _http_get_json(f"{cfg.vexa_api_url}/agent/workspace/git-remote-status", _headers(cfg))
    _check(status, data, "GET /agent/workspace/git-remote-status")
    return data if isinstance(data, dict) else {}


async def push(cfg: Config) -> dict[str, Any]:
    status, data = await _http_post_json(f"{cfg.vexa_api_url}/agent/workspace/push", _headers(cfg), {})
    _check(status, data, "POST /agent/workspace/push")
    return data if isinstance(data, dict) else {}


async def git_head(cfg: Config) -> str | None:
    """The workspace's current commit sha (data["commits"][0]["sha"]), or None when there are no
    commits yet or the response shape is unexpected. Used by summarizer.graph.wait_for_commit to
    detect the agent's commit landing on the event path."""
    status, data = await _http_get_json(f"{cfg.vexa_api_url}/agent/workspace/git", _headers(cfg))
    _check(status, data, "GET /agent/workspace/git")
    commits = data.get("commits") if isinstance(data, dict) else None
    if not isinstance(commits, list) or not commits or not isinstance(commits[0], dict):
        return None
    sha = commits[0].get("sha")
    return str(sha) if sha is not None else None


async def _http_get_json(url: str, headers: dict[str, str]) -> tuple[int, Any]:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            return resp.status, await _maybe_json(resp)


async def _http_delete_json(url: str, headers: dict[str, str]) -> tuple[int, Any]:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.delete(url, headers=headers) as resp:
            return resp.status, await _maybe_json(resp)


async def _http_post_json(url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers={**headers, "Content-Type": "application/json"}, json=body) as resp:
            return resp.status, await _maybe_json(resp)


async def _http_post_multipart(
    url: str, headers: dict[str, str], field: str, filename: str, content: str
) -> tuple[int, Any]:
    import aiohttp

    form = aiohttp.FormData()
    form.add_field(field, content.encode("utf-8"), filename=filename, content_type="text/markdown")
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=form) as resp:
            return resp.status, await _maybe_json(resp)


async def _maybe_json(resp: Any) -> Any:
    import json as _json

    text = await resp.text()
    try:
        return _json.loads(text)
    except (ValueError, _json.JSONDecodeError):
        return text
