"""Event-driven receiver for Vexa's `meeting.completed` webhook (and the same envelope shape
emitted by discord-vexa-bridge for meetings it writes straight into Vexa's Postgres).

Verified against Vexa's webhook delivery source (meeting_api/webhooks/delivery.py): POST,
`Content-Type: application/json`; when a secret is configured, `X-Webhook-Timestamp: <unix
seconds>` and `X-Webhook-Signature: sha256=<hex hmac_sha256(secret, "<timestamp>." + body)>`,
plus a legacy `Authorization: Bearer <secret>`. Body is a webhook.v1 Envelope:
`{"event_id", "event_type", "api_version", "created_at", "data": {"meeting": {...}}}`.
`event_id` is deterministic per transition, so redeliveries repeat it -- deduped here in a
bounded in-memory set. Other event types (`meeting.status_change`, `meeting.started`,
`bot.failed`) are acknowledged with 200 and otherwise ignored.

Registration is a Vexa quirk: `PUT {VEXA_API_URL}/user/webhook` returns the whole user record,
not a dedicated ack, so any 2xx counts as success.

The poll (summarizer.__main__.run_once) stays the fallback either way; a meeting whose event
fires before its transcript is fully flushed is deliberately not marked skipped here (see
summarizer.__main__.process_meeting's mark_low_transcript), so the next poll pass still gets it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from summarizer.types import Meeting
from summarizer.vexa import VexaError, _maybe_json, _parse_dt

if TYPE_CHECKING:
    from summarizer.config import Config

log = logging.getLogger("vexa-summarizer")

EventHandler = Callable[[Meeting], Awaitable[Any]]

_DEDUP_MAXLEN = 2048

_CFG_KEY: web.AppKey[Config] = web.AppKey("cfg")
_HANDLER_KEY: web.AppKey[EventHandler] = web.AppKey("handler")
_DEDUP_KEY: web.AppKey[_EventDedup] = web.AppKey("dedup")
_TASKS_KEY: web.AppKey[set[asyncio.Task[None]]] = web.AppKey("tasks")


def verify_signature(secret: str, timestamp: str, body: bytes, header_value: str | None) -> bool:
    """Verify either delivery form Vexa sends.

    `header_value` is whichever of the two auth headers the caller found: the new
    `X-Webhook-Signature: sha256=<hex>` form (verified via HMAC over "<timestamp>." + body), or
    the legacy `Authorization: Bearer <secret>` form (a direct constant-time compare). A missing
    or unrecognized header is rejected.
    """
    if not header_value:
        return False
    if header_value.startswith("sha256="):
        provided = header_value[len("sha256=") :]
        expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided, expected)
    if header_value.startswith("Bearer "):
        token = header_value[len("Bearer ") :]
        return hmac.compare_digest(token, secret)
    return False


def parse_envelope(payload: dict[str, Any]) -> Meeting | None:
    """Extract the Meeting a `meeting.completed` envelope carries, or None for any other event
    type or a malformed body (missing/invalid data.meeting fields)."""
    if payload.get("event_type") != "meeting.completed":
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    meeting = data.get("meeting")
    if not isinstance(meeting, dict):
        return None
    try:
        return Meeting(
            id=int(meeting["id"]),
            platform=str(meeting["platform"]),
            native_meeting_id=str(meeting["native_meeting_id"]),
            start=_parse_dt(meeting["start_time"]),
            end=_parse_dt(meeting.get("end_time") or meeting["start_time"]),
        )
    except (KeyError, ValueError, TypeError):
        return None


class _EventDedup:
    """Bounded in-memory set of seen event_ids.

    ponytail: per-process, not persisted -- a restart forgets recent ids and could reprocess one
    in-flight redelivery, but process_meeting's is_done/is_poisoned check makes that harmless.
    Upgrade to a shared store only if the receiver ever runs with more than one replica.
    """

    def __init__(self, maxlen: int = _DEDUP_MAXLEN) -> None:
        self._order: deque[str] = deque(maxlen=maxlen)
        self._seen: set[str] = set()

    def seen(self, event_id: str) -> bool:
        return event_id in self._seen

    def add(self, event_id: str) -> None:
        if len(self._order) == self._order.maxlen:
            oldest = self._order.popleft()
            self._seen.discard(oldest)
        self._order.append(event_id)
        self._seen.add(event_id)


async def _delayed_call(handler: EventHandler, meeting: Meeting, delay: float) -> None:
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        await handler(meeting)
    except Exception:
        log.exception("webhook handler failed for meeting %s", meeting.id)


async def _handle_webhook(request: web.Request) -> web.Response:
    cfg = request.app[_CFG_KEY]
    handler = request.app[_HANDLER_KEY]
    dedup = request.app[_DEDUP_KEY]
    tasks = request.app[_TASKS_KEY]

    body = await request.read()
    timestamp = request.headers.get("X-Webhook-Timestamp", "")
    header_value = request.headers.get("X-Webhook-Signature") or request.headers.get("Authorization")
    secret = cfg.webhook_secret
    if not secret or not verify_signature(secret, timestamp, body, header_value):
        return web.json_response({"ok": False, "error": "invalid signature"}, status=401)

    try:
        payload = json.loads(body)
    except ValueError:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

    if payload.get("event_type") != "meeting.completed":
        return web.json_response({"ok": True, "ignored": True})

    event_id = str(payload.get("event_id") or "")
    if event_id and dedup.seen(event_id):
        return web.json_response({"ok": True, "duplicate": True})

    meeting = parse_envelope(payload)
    if meeting is None:
        log.warning("webhook: meeting.completed event with an unparseable data.meeting; ignoring")
        return web.json_response({"ok": True, "ignored": True})

    if event_id:
        dedup.add(event_id)

    task = asyncio.create_task(_delayed_call(handler, meeting, cfg.webhook_delay_seconds))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return web.json_response({"ok": True, "meeting_id": meeting.id}, status=202)


async def _healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def make_app(cfg: Config, handler: EventHandler) -> web.Application:
    app = web.Application()
    app[_CFG_KEY] = cfg
    app[_HANDLER_KEY] = handler
    app[_DEDUP_KEY] = _EventDedup()
    app[_TASKS_KEY] = set()
    app.router.add_post(cfg.webhook_path, _handle_webhook)
    app.router.add_get("/healthz", _healthz)
    return app


async def serve(cfg: Config, handler: EventHandler) -> None:
    """Run the webhook receiver until cancelled (mirrors the poll loop's SIGTERM handling)."""
    app = make_app(cfg, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.webhook_host, cfg.webhook_port)
    await site.start()
    log.info("webhook receiver listening on %s:%d%s", cfg.webhook_host, cfg.webhook_port, cfg.webhook_path)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def register_with_vexa(cfg: Config) -> None:
    """PUT /user/webhook so Vexa delivers meeting.completed to WEBHOOK_PUBLIC_URL. The response
    is the whole user record (a Vexa quirk); any 2xx counts as success. Never logs the secret."""
    url = f"{cfg.vexa_api_url}/user/webhook"
    headers = {"X-API-Key": cfg.vexa_api_key, "Accept": "application/json", "Content-Type": "application/json"}
    body = {
        "webhook_url": cfg.webhook_public_url,
        "webhook_secret": cfg.webhook_secret,
        "webhook_events": {"meeting.completed": True},
    }
    status, data = await _http_put_json(url, headers, body)
    if status < 200 or status >= 300:
        raise VexaError(f"PUT /user/webhook -> HTTP {status}: {str(data)[:200]}")
    log.info("registered webhook with Vexa: %s", cfg.webhook_public_url)


async def _http_put_json(url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.put(url, headers=headers, json=body) as resp:
            return resp.status, await _maybe_json(resp)
