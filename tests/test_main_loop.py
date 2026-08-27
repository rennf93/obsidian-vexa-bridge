"""Tests for summarizer.__main__ loop control — --once single pass + cancellation exit.

These tests exercise only the loop scaffolding (main / _loop / --once flag). run_once is
monkeypatched so no aiohttp/litellm/Vexa dependency is needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

import pytest

from summarizer import __main__ as m
from summarizer import config
from summarizer.__main__ import PassResult


def _set_required_env(monkeypatch, *, sink="mcp"):
    monkeypatch.setenv("VEXA_API_URL", "http://vexa:8056")
    monkeypatch.setenv("VEXA_API_KEY", "k")
    monkeypatch.setenv("AI_API_KEY", "sk-test")
    monkeypatch.setenv("OBSIDIAN_ENABLED", "true")
    if sink == "mcp":
        monkeypatch.setenv("OBSIDIAN_SINK", "mcp")
        monkeypatch.setenv("OBSIDIAN_MCP_TOKEN", "tok")
    else:
        monkeypatch.setenv("OBSIDIAN_SINK", "fs")
        monkeypatch.setenv("VAULT_DIR", "/tmp/ovb-vault")


def test_once_flag_runs_single_pass_and_exits(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["summarizer", "--once"])

    calls = {"n": 0}

    async def fake_run_once(cfg):
        calls["n"] += 1
        return PassResult(summarized=1)

    monkeypatch.setattr(m, "run_once", fake_run_once)

    rc = m.main()
    assert rc == 0
    assert calls["n"] == 1  # exactly one pass, no loop


def test_loop_exits_on_cancellation(monkeypatch):
    """_loop runs a pass, then exits cleanly when the task is cancelled (SIGTERM stand-in)."""
    cfg = config.Config(
        summarize_enabled=True,
        vexa_api_url="http://vexa:8056",
        vexa_api_key="k",
        obsidian_enabled=False,
        poll_interval_seconds=180,
    )

    calls = {"n": 0}

    async def fake_run_once(_cfg):
        calls["n"] += 1
        return PassResult()

    monkeypatch.setattr(m, "run_once", fake_run_once)

    async def driver():
        task = asyncio.create_task(m._loop(cfg))
        await asyncio.sleep(0)  # let the loop start and call run_once once
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(driver())
    assert calls["n"] >= 1  # at least one pass ran before cancellation


def test_loop_starts_webhook_server_and_registers_when_enabled(monkeypatch):
    cfg = config.Config(
        summarize_enabled=True,
        vexa_api_url="http://vexa:8056",
        vexa_api_key="k",
        obsidian_enabled=False,
        poll_interval_seconds=180,
        webhook_enabled=True,
        webhook_secret="s",
        webhook_public_url="http://bridge:8080/webhook",
    )

    async def fake_run_once(_cfg):
        return PassResult()

    served = {"n": 0}

    async def fake_serve(_cfg, _handler):
        served["n"] += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    registered = {"n": 0}

    async def fake_register(_cfg):
        registered["n"] += 1

    monkeypatch.setattr(m, "run_once", fake_run_once)
    monkeypatch.setattr(m, "serve", fake_serve)
    monkeypatch.setattr(m, "register_with_vexa", fake_register)

    async def driver():
        task = asyncio.create_task(m._loop(cfg))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(driver())
    assert served["n"] == 1
    assert registered["n"] == 1


def test_loop_does_not_start_webhook_server_when_disabled(monkeypatch):
    cfg = config.Config(
        summarize_enabled=True,
        vexa_api_url="http://vexa:8056",
        vexa_api_key="k",
        obsidian_enabled=False,
        poll_interval_seconds=180,
    )
    assert cfg.webhook_enabled is False

    async def fake_run_once(_cfg):
        return PassResult()

    def fake_serve(*_a, **_k):
        raise AssertionError("serve must not be called when WEBHOOK_ENABLED is false")

    async def fake_register(_cfg):
        raise AssertionError("register_with_vexa must not be called when WEBHOOK_ENABLED is false")

    monkeypatch.setattr(m, "run_once", fake_run_once)
    monkeypatch.setattr(m, "serve", fake_serve)
    monkeypatch.setattr(m, "register_with_vexa", fake_register)

    async def driver():
        task = asyncio.create_task(m._loop(cfg))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(driver())  # no assertion errors raised from within _loop


def test_loop_skips_registration_when_no_public_url(monkeypatch):
    cfg = config.Config(
        summarize_enabled=True,
        vexa_api_url="http://vexa:8056",
        vexa_api_key="k",
        obsidian_enabled=False,
        poll_interval_seconds=180,
        webhook_enabled=True,
        webhook_secret="s",
        webhook_public_url=None,
    )

    async def fake_run_once(_cfg):
        return PassResult()

    async def fake_serve(_cfg, _handler):
        await asyncio.Event().wait()

    async def fake_register(_cfg):
        raise AssertionError("register_with_vexa must not be called without WEBHOOK_PUBLIC_URL")

    monkeypatch.setattr(m, "run_once", fake_run_once)
    monkeypatch.setattr(m, "serve", fake_serve)
    monkeypatch.setattr(m, "register_with_vexa", fake_register)

    async def driver():
        task = asyncio.create_task(m._loop(cfg))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(driver())


# --- _drain_event_tasks / shutdown draining ------------------------------


async def test_drain_event_tasks_warns_when_a_task_is_still_running(caplog):
    task = asyncio.create_task(asyncio.sleep(10))
    try:
        with caplog.at_level("WARNING", logger="vexa-summarizer"):
            await m._drain_event_tasks({task}, timeout=0.05)
        assert "1 webhook event task" in caplog.text
        assert "still running" in caplog.text
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_drain_event_tasks_no_warning_when_task_finishes_in_time(caplog):
    task = asyncio.create_task(asyncio.sleep(0))
    with caplog.at_level("WARNING", logger="vexa-summarizer"):
        await m._drain_event_tasks({task}, timeout=1.0)
    assert "still running" not in caplog.text


async def test_drain_event_tasks_noop_on_empty_set():
    await m._drain_event_tasks(set(), timeout=1.0)  # must not raise or hang


def test_loop_drains_pending_event_tasks_on_shutdown(monkeypatch):
    cfg = config.Config(
        summarize_enabled=True,
        vexa_api_url="http://vexa:8056",
        vexa_api_key="k",
        obsidian_enabled=False,
        poll_interval_seconds=180,
        webhook_enabled=True,
        webhook_secret="s",
    )

    calls = {"n": 0}

    async def fake_run_once(_cfg):
        return PassResult()

    async def fake_serve(_cfg, _handler):
        await asyncio.Event().wait()

    def fake_pending():
        calls["n"] += 1
        return set()

    monkeypatch.setattr(m, "run_once", fake_run_once)
    monkeypatch.setattr(m, "serve", fake_serve)
    monkeypatch.setattr(m, "pending_event_tasks", fake_pending)

    async def driver():
        task = asyncio.create_task(m._loop(cfg))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(driver())
    assert calls["n"] == 1
