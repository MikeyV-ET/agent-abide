"""Pure reads of agent status files for TUI header/telemetry. No Textual."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass
class AgentTelemetry:
    agent_name: str
    health_status: str = "unknown"
    is_generating: bool = False
    context_pct: int = 0
    code_version: str = ""
    model_name: str = ""
    gaze_target: str = "unknown"
    total_tokens: Optional[int] = None
    context_window: Optional[int] = None


def read_health(path: Path) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def read_gaze(path: Path) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def telemetry_from_files(
    agent_name: str,
    health_path: Path,
    gaze_path: Path,
    abide_head: str = "",
) -> AgentTelemetry:
    """Build header telemetry from health.json + gaze.json paths."""
    t = AgentTelemetry(agent_name=agent_name)
    try:
        health = read_health(health_path)
        status = health.get("status", "unknown")
        t.health_status = status
        t.is_generating = status == "working"
        tokens = health.get("totalTokens")
        window = health.get("contextWindow")
        t.total_tokens = tokens if isinstance(tokens, int) else None
        t.context_window = window if isinstance(window, int) else None
        if isinstance(tokens, int) and isinstance(window, int) and window > 0:
            t.context_pct = int(tokens / window * 100)
        t.code_version = health.get("code_version", "") or ""
        t.model_name = health.get("model", "") or ""
    except Exception:
        t.health_status = "unknown"
        t.is_generating = False

    try:
        gaze = read_gaze(gaze_path)
        speech = gaze.get("speech", {})
        target = speech.get("target", "?")
        params = speech.get("params", {})
        room = params.get("room", "") or params.get("pm", "")
        t.gaze_target = f"{target}/{room}" if room else target
    except Exception:
        t.gaze_target = "unknown"
    return t


def code_version_stale(code_version: str, abide_head: str) -> bool:
    return bool(abide_head and code_version and code_version != abide_head)
