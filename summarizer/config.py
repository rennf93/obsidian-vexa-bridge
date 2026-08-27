"""Env -> typed Config for the meeting summarizer.

BRIDGE_MODE=note (default, one summary note per meeting via an LLM) or BRIDGE_MODE=graph
(transcripts into Vexa's agent workspace, the agent maintains an OKF knowledge graph, no LLM
here).

Pure: reads a mapping (defaults to os.environ). Exits with a clear ConfigError when a
required var is missing AND the feature that needs it is enabled. Mirrors Vexa's dashboard
LLM convention (AI_MODEL=provider/model + AI_API_KEY + AI_BASE_URL) so the same dialect works
for Anthropic, Ollama, vLLM, any OpenAI-compatible endpoint.

Two note sinks: OBSIDIAN_SINK=fs (default) writes notes to VAULT_DIR via the filesystem;
OBSIDIAN_SINK=mcp keeps the original Mac vault-as-mcp HTTP sink. Runs as a long-running
poll loop (POLL_INTERVAL_SECONDS, default 180s) — see __main__.py.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_AI_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_PLATFORMS = ["discord"]
DEFAULT_MIN_SECONDS = 30.0
DEFAULT_OBSIDIAN_URL = "http://localhost:8765/mcp"
DEFAULT_NOTE_FOLDER = "Meetings"
DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / "vexa-summarizer"
DEFAULT_OBSIDIAN_SINK = "fs"
DEFAULT_BRIDGE_MODE = "note"
BRIDGE_MODES = {"note", "graph"}
DEFAULT_VAULT_FOLDER = "Vexa"
DEFAULT_ROUTINE_NAME = "meeting-to-graph"
DEFAULT_ROUTINE_CRON = "0 * * * *"


class ConfigError(ValueError):
    """Raised when required env is missing for an enabled feature."""


@dataclass
class Config:
    summarize_enabled: bool = True
    ai_model: str = DEFAULT_AI_MODEL
    ai_api_key: str | None = None
    ai_base_url: str | None = None
    vexa_api_url: str = ""
    vexa_api_key: str = ""
    vexa_database_url: str | None = None  # direct-Postgres fallback for transcripts
    summarize_platforms: list[str] = field(default_factory=lambda: list(DEFAULT_PLATFORMS))
    min_transcript_seconds: float = DEFAULT_MIN_SECONDS
    obsidian_enabled: bool = True
    obsidian_mcp_url: str = DEFAULT_OBSIDIAN_URL
    obsidian_mcp_token: str | None = None
    obsidian_note_folder: str = DEFAULT_NOTE_FOLDER
    include_transcript: bool = True
    vexa_notes_enabled: bool = False
    dry_run: bool = False
    state_dir: Path = field(default_factory=lambda: DEFAULT_STATE_DIR)
    obsidian_sink: str = DEFAULT_OBSIDIAN_SINK
    vault_dir: Path | None = None
    poll_interval_seconds: int = 180
    bridge_mode: str = DEFAULT_BRIDGE_MODE
    vault_folder: str = DEFAULT_VAULT_FOLDER
    graph_routine_name: str = DEFAULT_ROUTINE_NAME
    graph_routine_cron: str = DEFAULT_ROUTINE_CRON


def _bool(val: str | None) -> bool:
    return str(val or "").strip().lower() in {"true", "1", "yes"}


def _req(env: Mapping[str, str], name: str) -> str:
    val = env.get(name, "").strip()
    if not val:
        raise ConfigError(f"{name} is required")
    return val


def _split_csv(val: str | None) -> list[str]:
    return [p.strip() for p in (val or "").split(",") if p.strip()]


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    cfg = Config()

    cfg.summarize_enabled = _bool(env.get("SUMMARIZE_ENABLED", "true"))
    if not cfg.summarize_enabled:
        # Master switch off: don't enforce anything else — short-circuit the whole pass.
        return cfg

    cfg.bridge_mode = env.get("BRIDGE_MODE", DEFAULT_BRIDGE_MODE).strip().lower() or DEFAULT_BRIDGE_MODE
    if cfg.bridge_mode not in BRIDGE_MODES:
        raise ConfigError(f"BRIDGE_MODE must be one of {sorted(BRIDGE_MODES)}, got {cfg.bridge_mode!r}")

    # Shared by both modes.
    cfg.vexa_api_url = _req(env, "VEXA_API_URL")
    cfg.vexa_api_key = _req(env, "VEXA_API_KEY")
    cfg.vexa_database_url = (env.get("VEXA_DATABASE_URL") or "").strip() or None
    cfg.summarize_platforms = _split_csv(env.get("SUMMARIZE_PLATFORMS")) or list(DEFAULT_PLATFORMS)
    cfg.min_transcript_seconds = float(env.get("MIN_TRANSCRIPT_SECONDS", str(DEFAULT_MIN_SECONDS)))
    cfg.dry_run = _bool(env.get("DRY_RUN", "false"))
    state_dir = (env.get("STATE_DIR") or "").strip()
    cfg.state_dir = Path(state_dir) if state_dir else DEFAULT_STATE_DIR
    cfg.poll_interval_seconds = int(env.get("POLL_INTERVAL_SECONDS", "180"))

    if cfg.bridge_mode == "graph":
        # No LLM and no note sink: the agent on the Vexa side does the writing. VAULT_DIR is
        # optional here; when set, the bridge fast-forwards <VAULT_DIR>/<VEXA_VAULT_FOLDER>.
        vault_dir = (env.get("VAULT_DIR") or "").strip()
        cfg.vault_dir = Path(vault_dir) if vault_dir else None
        cfg.vault_folder = env.get("VEXA_VAULT_FOLDER", DEFAULT_VAULT_FOLDER).strip() or DEFAULT_VAULT_FOLDER
        cfg.graph_routine_name = env.get("GRAPH_ROUTINE_NAME", DEFAULT_ROUTINE_NAME).strip() or DEFAULT_ROUTINE_NAME
        cfg.graph_routine_cron = env.get("GRAPH_ROUTINE_CRON", DEFAULT_ROUTINE_CRON).strip() or DEFAULT_ROUTINE_CRON
        return cfg

    cfg.ai_model = env.get("AI_MODEL", DEFAULT_AI_MODEL).strip() or DEFAULT_AI_MODEL
    cfg.ai_api_key = (env.get("AI_API_KEY") or "").strip() or None  # "not-needed" for local
    cfg.ai_base_url = (env.get("AI_BASE_URL") or "").strip() or None
    cfg.obsidian_enabled = _bool(env.get("OBSIDIAN_ENABLED", "true"))
    cfg.obsidian_sink = env.get("OBSIDIAN_SINK", DEFAULT_OBSIDIAN_SINK).strip() or DEFAULT_OBSIDIAN_SINK
    cfg.obsidian_mcp_url = env.get("OBSIDIAN_MCP_URL", DEFAULT_OBSIDIAN_URL).strip() or DEFAULT_OBSIDIAN_URL
    if cfg.obsidian_enabled and cfg.obsidian_sink == "mcp":
        cfg.obsidian_mcp_token = _req(env, "OBSIDIAN_MCP_TOKEN")
    if cfg.obsidian_enabled and cfg.obsidian_sink == "fs":
        cfg.vault_dir = Path(_req(env, "VAULT_DIR"))
    cfg.obsidian_note_folder = env.get("OBSIDIAN_NOTE_FOLDER", DEFAULT_NOTE_FOLDER).strip() or DEFAULT_NOTE_FOLDER
    cfg.include_transcript = _bool(env.get("INCLUDE_TRANSCRIPT", "true"))
    cfg.vexa_notes_enabled = _bool(env.get("VEXA_NOTES_ENABLED", "false"))

    return cfg
