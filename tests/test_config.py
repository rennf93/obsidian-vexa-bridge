"""Tests for summarizer.config — env -> typed Config, per-sink required-var enforcement."""

from pathlib import Path

import pytest

from summarizer import config


def _base_env():
    # OBSIDIAN_SINK=mcp keeps the existing MCP-token assertions meaningful. The fs sink
    # has its own dedicated tests below.
    return {
        "SUMMARIZE_ENABLED": "true",
        "AI_MODEL": "anthropic/claude-sonnet-5",
        "AI_API_KEY": "sk-test",
        "VEXA_API_URL": "http://192.168.50.111:8056",
        "VEXA_API_KEY": "vexa-key",
        "SUMMARIZE_PLATFORMS": "discord,google_meet",
        "MIN_TRANSCRIPT_SECONDS": "30",
        "OBSIDIAN_ENABLED": "true",
        "OBSIDIAN_SINK": "mcp",
        "OBSIDIAN_MCP_URL": "http://localhost:8765/mcp",
        "OBSIDIAN_MCP_TOKEN": "tok",
        "OBSIDIAN_NOTE_FOLDER": "Meetings",
        "INCLUDE_TRANSCRIPT": "true",
        "VEXA_NOTES_ENABLED": "false",
        "DRY_RUN": "false",
        "STATE_DIR": "/tmp/state",
    }


def test_load_config_parses_all_fields():
    cfg = config.load_config(_base_env())
    assert cfg.summarize_enabled is True
    assert cfg.ai_model == "anthropic/claude-sonnet-5"
    assert cfg.ai_api_key == "sk-test"
    assert cfg.vexa_api_url == "http://192.168.50.111:8056"
    assert cfg.vexa_api_key == "vexa-key"
    assert cfg.summarize_platforms == ["discord", "google_meet"]
    assert cfg.min_transcript_seconds == 30.0
    assert cfg.obsidian_enabled is True
    assert cfg.obsidian_sink == "mcp"
    assert cfg.obsidian_mcp_url == "http://localhost:8765/mcp"
    assert cfg.obsidian_mcp_token == "tok"
    assert cfg.obsidian_note_folder == "Meetings"
    assert cfg.include_transcript is True
    assert cfg.vexa_notes_enabled is False
    assert cfg.dry_run is False
    assert cfg.state_dir == Path("/tmp/state")


def test_bool_parsing_accepts_common_truthy():
    env = _base_env()
    for val in ("true", "1", "yes", "TRUE", "Yes"):
        env["OBSIDIAN_ENABLED"] = val
        assert config.load_config(env).obsidian_enabled is True, val
    for val in ("false", "0", "no", "", "anything-else"):
        env["OBSIDIAN_ENABLED"] = val
        assert config.load_config(env).obsidian_enabled is False, val


def test_summarize_disabled_short_circuits_required_vars():
    """When the master switch is off, missing Vexa/Obsidian vars must NOT raise."""
    env = {"SUMMARIZE_ENABLED": "false"}
    cfg = config.load_config(env)
    assert cfg.summarize_enabled is False


def test_missing_vexa_url_raises():
    env = _base_env()
    del env["VEXA_API_URL"]
    with pytest.raises(config.ConfigError, match="VEXA_API_URL"):
        config.load_config(env)


def test_missing_vexa_key_raises():
    env = _base_env()
    del env["VEXA_API_KEY"]
    with pytest.raises(config.ConfigError, match="VEXA_API_KEY"):
        config.load_config(env)


def test_obsidian_enabled_requires_token():
    env = _base_env()
    del env["OBSIDIAN_MCP_TOKEN"]
    with pytest.raises(config.ConfigError, match="OBSIDIAN_MCP_TOKEN"):
        config.load_config(env)


def test_obsidian_disabled_does_not_require_token():
    env = _base_env()
    env["OBSIDIAN_ENABLED"] = "false"
    del env["OBSIDIAN_MCP_TOKEN"]
    cfg = config.load_config(env)  # must not raise
    assert cfg.obsidian_enabled is False


def test_defaults_when_optional_vars_missing():
    env = _base_env()
    del env["AI_MODEL"]
    del env["SUMMARIZE_PLATFORMS"]
    del env["MIN_TRANSCRIPT_SECONDS"]
    del env["OBSIDIAN_MCP_URL"]
    del env["OBSIDIAN_NOTE_FOLDER"]
    del env["INCLUDE_TRANSCRIPT"]
    del env["VEXA_NOTES_ENABLED"]
    del env["DRY_RUN"]
    del env["STATE_DIR"]
    cfg = config.load_config(env)
    assert cfg.ai_model == "anthropic/claude-sonnet-5"
    assert cfg.summarize_platforms == ["discord"]
    assert cfg.min_transcript_seconds == 30.0
    assert cfg.obsidian_mcp_url == "http://localhost:8765/mcp"
    assert cfg.obsidian_note_folder == "Meetings"
    assert cfg.include_transcript is True
    assert cfg.vexa_notes_enabled is False
    assert cfg.dry_run is False
    assert cfg.state_dir == Path.home() / ".local" / "share" / "vexa-summarizer"


# --- per-sink required-var enforcement -------------------------------------


def test_fs_sink_requires_vault_dir():
    env = _base_env()
    env["OBSIDIAN_SINK"] = "fs"
    del env["OBSIDIAN_MCP_TOKEN"]  # not required under fs
    # no VAULT_DIR set
    with pytest.raises(config.ConfigError, match="VAULT_DIR"):
        config.load_config(env)


def test_fs_sink_does_not_require_mcp_token():
    env = _base_env()
    env["OBSIDIAN_SINK"] = "fs"
    env["VAULT_DIR"] = "/vault"
    del env["OBSIDIAN_MCP_TOKEN"]  # not required under fs
    cfg = config.load_config(env)
    assert cfg.obsidian_sink == "fs"
    assert cfg.vault_dir == Path("/vault")
    assert cfg.obsidian_mcp_token is None


def test_mcp_sink_requires_mcp_token_not_vault_dir():
    # mcp sink loads fine without VAULT_DIR...
    env = _base_env()
    env["OBSIDIAN_SINK"] = "mcp"
    assert "VAULT_DIR" not in env
    cfg = config.load_config(env)
    assert cfg.obsidian_sink == "mcp"
    assert cfg.vault_dir is None
    # ...but missing OBSIDIAN_MCP_TOKEN raises
    del env["OBSIDIAN_MCP_TOKEN"]
    with pytest.raises(config.ConfigError, match="OBSIDIAN_MCP_TOKEN"):
        config.load_config(env)


def test_poll_interval_parses():
    env = _base_env()
    env["POLL_INTERVAL_SECONDS"] = "60"
    cfg = config.load_config(env)
    assert cfg.poll_interval_seconds == 60
    # default 180 when absent
    del env["POLL_INTERVAL_SECONDS"]
    cfg = config.load_config(env)
    assert cfg.poll_interval_seconds == 180


def test_default_sink_is_fs_when_obsidian_sink_absent():
    """The package default sink is 'fs' — asserted separately from the
    _base_env()-backed defaults test (which pins OBSIDIAN_SINK=mcp so the
    MCP-token path stays exercised). Here we drop OBSIDIAN_SINK entirely and
    set VAULT_DIR so the fs required-var enforcement passes."""
    env = _base_env()
    del env["OBSIDIAN_SINK"]
    env["VAULT_DIR"] = "/vault"
    cfg = config.load_config(env)
    assert cfg.obsidian_sink == "fs"
    assert cfg.vault_dir == Path("/vault")


# --- graph mode --------------------------------------------------------


def test_graph_mode_requires_only_vexa_vars():
    cfg = config.load_config({"BRIDGE_MODE": "graph", "VEXA_API_URL": "http://v", "VEXA_API_KEY": "k"})
    assert cfg.bridge_mode == "graph"
    assert cfg.vault_dir is None
    assert cfg.vault_folder == "Vexa"
    assert cfg.graph_routine_name == "meeting-to-graph"
    assert cfg.graph_routine_cron == "0 * * * *"


def test_graph_mode_reads_vault_dir_and_routine_overrides():
    cfg = config.load_config(
        {
            "BRIDGE_MODE": "graph",
            "VEXA_API_URL": "http://v",
            "VEXA_API_KEY": "k",
            "VAULT_DIR": "/vault",
            "VEXA_VAULT_FOLDER": "Knowledge",
            "GRAPH_ROUTINE_NAME": "fold",
            "GRAPH_ROUTINE_CRON": "0 * * * *",
        }
    )
    assert str(cfg.vault_dir) == "/vault"
    assert cfg.vault_folder == "Knowledge"
    assert cfg.graph_routine_name == "fold"
    assert cfg.graph_routine_cron == "0 * * * *"


def test_graph_mode_ignores_obsidian_sink_requirements():
    # In note mode the mcp sink would demand OBSIDIAN_MCP_TOKEN; graph mode never reads it.
    cfg = config.load_config(
        {"BRIDGE_MODE": "graph", "VEXA_API_URL": "http://v", "VEXA_API_KEY": "k", "OBSIDIAN_SINK": "mcp"}
    )
    assert cfg.bridge_mode == "graph"


def test_unknown_bridge_mode_is_a_config_error():
    with pytest.raises(config.ConfigError):
        config.load_config({"BRIDGE_MODE": "vault", "VEXA_API_URL": "http://v", "VEXA_API_KEY": "k"})


def test_default_mode_is_note_and_unchanged():
    cfg = config.load_config({"VEXA_API_URL": "http://v", "VEXA_API_KEY": "k", "VAULT_DIR": "/vault"})
    assert cfg.bridge_mode == "note"
    assert cfg.obsidian_sink == "fs"
