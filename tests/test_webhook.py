"""Webhook receiver: signature verification, envelope parsing, the aiohttp app, and
registration with Vexa. HTTP is faked either via the aiohttp test_utils client (the real app
wiring) or via the _http_put_json seam (registration), so no real socket is used."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from summarizer import webhook
from summarizer.config import Config

SECRET = "s3cr3t"


def _cfg(**overrides):
    base = dict(
        vexa_api_url="http://vexa:8056",
        vexa_api_key="k",
        webhook_enabled=True,
        webhook_secret=SECRET,
        webhook_path="/webhook",
        webhook_delay_seconds=0.0,
    )
    base.update(overrides)
    return Config(**base)


def _sig_header(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def _envelope(event_type="meeting.completed", event_id="evt_1", meeting=None):
    return {
        "event_id": event_id,
        "event_type": event_type,
        "api_version": "2026-03-01",
        "created_at": "2026-06-18T10:42:00.000Z",
        "data": {
            "meeting": meeting
            if meeting is not None
            else {
                "id": 11367,
                "platform": "google_meet",
                "native_meeting_id": "abc-defg-hij",
                "status": "completed",
                "start_time": "2026-06-18T10:00:00.000Z",
                "end_time": "2026-06-18T10:42:00.000Z",
            }
        },
    }


# --- verify_signature ----------------------------------------------------


def test_verify_signature_valid():
    body = b'{"a":1}'
    timestamp = "1700000000"
    header = _sig_header(SECRET, timestamp, body)
    assert webhook.verify_signature(SECRET, timestamp, body, header) is True


def test_verify_signature_wrong_secret():
    body = b'{"a":1}'
    timestamp = "1700000000"
    header = _sig_header("other-secret", timestamp, body)
    assert webhook.verify_signature(SECRET, timestamp, body, header) is False


def test_verify_signature_missing_header():
    assert webhook.verify_signature(SECRET, "1700000000", b"{}", None) is False


def test_verify_signature_garbage_header():
    assert webhook.verify_signature(SECRET, "1700000000", b"{}", "not-a-real-header") is False


def test_verify_signature_legacy_bearer_accepted():
    assert webhook.verify_signature(SECRET, "", b"{}", f"Bearer {SECRET}") is True


def test_verify_signature_legacy_bearer_wrong_token():
    assert webhook.verify_signature(SECRET, "", b"{}", "Bearer wrong") is False


# --- parse_envelope --------------------------------------------------------


def test_parse_envelope_completed_returns_meeting():
    meeting = webhook.parse_envelope(_envelope())
    assert meeting is not None
    assert meeting.id == 11367
    assert meeting.platform == "google_meet"
    assert meeting.native_meeting_id == "abc-defg-hij"
    assert meeting.start.year == 2026
    assert meeting.end.hour == 10 and meeting.end.minute == 42


def test_parse_envelope_missing_end_time_falls_back_to_start():
    env = _envelope(
        meeting={
            "id": 1,
            "platform": "discord",
            "native_meeting_id": "d1",
            "start_time": "2026-06-18T10:00:00.000Z",
        }
    )
    meeting = webhook.parse_envelope(env)
    assert meeting is not None
    assert meeting.end == meeting.start


def test_parse_envelope_ignores_other_event_types():
    env = _envelope(event_type="meeting.status_change")
    assert webhook.parse_envelope(env) is None


def test_parse_envelope_malformed_body_returns_none():
    assert webhook.parse_envelope({"event_type": "meeting.completed"}) is None
    assert webhook.parse_envelope({"event_type": "meeting.completed", "data": {}}) is None
    assert webhook.parse_envelope({"event_type": "meeting.completed", "data": {"meeting": {}}}) is None
    assert webhook.parse_envelope({}) is None


# --- make_app --------------------------------------------------------------


async def _post(client, path, body: dict, secret: str = SECRET, timestamp: str = "1700000000"):
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "X-Webhook-Timestamp": timestamp}
    headers["X-Webhook-Signature"] = _sig_header(secret, timestamp, raw)
    return await client.post(path, data=raw, headers=headers)


async def test_unsigned_request_is_rejected():
    cfg = _cfg()

    async def handler(meeting):
        raise AssertionError("handler must not run for an unsigned request")

    app = webhook.make_app(cfg, handler)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(cfg.webhook_path, data=json.dumps(_envelope()).encode())
        assert resp.status == 401


async def test_signed_completed_event_returns_202_and_schedules_handler():
    cfg = _cfg(webhook_delay_seconds=0.0)
    recorded = []

    async def handler(meeting):
        recorded.append(meeting)

    app = webhook.make_app(cfg, handler)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(client, cfg.webhook_path, _envelope())
        assert resp.status == 202
        data = await resp.json()
        assert data == {"ok": True, "meeting_id": 11367}

        for _ in range(200):
            if recorded:
                break
            await asyncio.sleep(0.01)
        assert len(recorded) == 1
        assert recorded[0].id == 11367


async def test_legacy_bearer_auth_is_accepted():
    cfg = _cfg()

    async def handler(meeting):
        pass

    app = webhook.make_app(cfg, handler)
    async with TestClient(TestServer(app)) as client:
        raw = json.dumps(_envelope()).encode()
        resp = await client.post(
            cfg.webhook_path,
            data=raw,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {SECRET}"},
        )
        assert resp.status == 202


async def test_non_completed_event_is_ignored_with_200():
    cfg = _cfg()

    async def handler(meeting):
        raise AssertionError("handler must not run for a non-completed event")

    app = webhook.make_app(cfg, handler)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(client, cfg.webhook_path, _envelope(event_type="meeting.status_change"))
        assert resp.status == 200
        assert await resp.json() == {"ok": True, "ignored": True}


async def test_duplicate_event_id_is_not_reprocessed():
    cfg = _cfg()
    calls = []

    async def handler(meeting):
        calls.append(meeting)

    app = webhook.make_app(cfg, handler)
    async with TestClient(TestServer(app)) as client:
        env = _envelope(event_id="evt_dup")
        first = await _post(client, cfg.webhook_path, env)
        assert first.status == 202
        second = await _post(client, cfg.webhook_path, env)
        assert second.status == 200
        assert await second.json() == {"ok": True, "duplicate": True}


async def test_bad_json_returns_400():
    cfg = _cfg()

    async def handler(meeting):
        raise AssertionError("handler must not run")

    app = webhook.make_app(cfg, handler)
    async with TestClient(TestServer(app)) as client:
        timestamp = "1700000000"
        raw = b"{not json"
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Signature": _sig_header(SECRET, timestamp, raw),
        }
        resp = await client.post(cfg.webhook_path, data=raw, headers=headers)
        assert resp.status == 400


async def test_healthz_returns_200():
    cfg = _cfg()

    async def handler(meeting):
        pass

    app = webhook.make_app(cfg, handler)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200


# --- register_with_vexa -----------------------------------------------------


async def test_register_with_vexa_puts_expected_shape(monkeypatch):
    calls = []

    async def fake_put(url, headers, body):
        calls.append((url, headers, body))
        return 200, {"id": "user-1"}

    monkeypatch.setattr(webhook, "_http_put_json", fake_put)
    cfg = _cfg(webhook_public_url="http://bridge:8080/webhook")
    await webhook.register_with_vexa(cfg)

    url, headers, body = calls[0]
    assert url == "http://vexa:8056/user/webhook"
    assert headers["X-API-Key"] == "k"
    assert body == {
        "webhook_url": "http://bridge:8080/webhook",
        "webhook_secret": SECRET,
        "webhook_events": {"meeting.completed": True},
    }


async def test_register_with_vexa_raises_on_non_2xx(monkeypatch):
    async def fake_put(url, headers, body):
        return 422, {"detail": "bad url"}

    monkeypatch.setattr(webhook, "_http_put_json", fake_put)
    cfg = _cfg(webhook_public_url="http://bridge:8080/webhook")
    with pytest.raises(webhook.VexaError):
        await webhook.register_with_vexa(cfg)
