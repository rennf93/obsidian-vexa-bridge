"""Tests for summarizer.__main__ loop control — --once single pass + cancellation exit.

These tests exercise only the loop scaffolding (main / _loop / --once flag). run_once is
monkeypatched so no aiohttp/litellm/Vexa dependency is needed.
"""

from __future__ import annotations

import asyncio
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
