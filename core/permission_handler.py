"""
permission_handler.py -- File-based permission request/decision flow.

Used by asdaaas to route tool permission requests from an intern agent
to a mentor agent, and by mentor agents to approve/reject requests.

Permission files live under ~/agents/<Intern>/asdaaas/permissions/:
  pending/   -- requests waiting for decision
  decisions/ -- mentor decisions (matched by req_id)
  log/       -- completed requests (moved after processing)
"""

import json
import os
import time
import secrets
from pathlib import Path

try:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from asdaaas_config import config
except ImportError:
    config = None


def _permissions_dir(agent_name: str) -> Path:
    if config:
        return config.agent_permissions_dir(agent_name)
    return Path.home() / "agents" / agent_name / "asdaaas" / "permissions"


def write_permission_request(intern_agent: str, params: dict) -> str:
    """Write a pending permission request file. Returns req_id."""
    ts = int(time.time() * 1000)
    rand = secrets.token_hex(4)
    req_id = f"perm_{ts}_{rand}"

    pending_dir = _permissions_dir(intern_agent) / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    tool_call = params.get("toolCall", {})
    request = {
        "req_id": req_id,
        "rpc_id": params.get("_rpc_id"),  # injected by caller
        "session_id": params.get("sessionId"),
        "tool_call_id": tool_call.get("toolCallId"),
        "kind": tool_call.get("kind", "unknown"),
        "title": tool_call.get("title", ""),
        "status": tool_call.get("status", ""),
        "content": tool_call.get("content", []),
        "options": [o.get("optionId") for o in params.get("options", [])],
        "ts": time.time(),
    }

    path = pending_dir / f"{req_id}.json"
    with open(path, "w") as f:
        json.dump(request, f, indent=2)

    return req_id


def approve_permission(intern_agent: str, req_id: str, reason: str = "",
                       kind: str = "allow-once", decided_by: str = "unknown"):
    """Write a decision file approving a permission request."""
    _write_decision(intern_agent, req_id, kind, reason, decided_by)


def reject_permission(intern_agent: str, req_id: str, reason: str = "",
                      decided_by: str = "unknown"):
    """Write a decision file rejecting a permission request."""
    _write_decision(intern_agent, req_id, "reject-once", reason, decided_by)


def _write_decision(intern_agent: str, req_id: str, option_id: str,
                    reason: str, decided_by: str):
    decisions_dir = _permissions_dir(intern_agent) / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)

    decision = {
        "req_id": req_id,
        "decision": option_id,
        "decided_by": decided_by,
        "reason": reason,
        "ts": time.time(),
    }

    path = decisions_dir / f"{req_id}.json"
    with open(path, "w") as f:
        json.dump(decision, f, indent=2)


def read_decision(intern_agent: str, req_id: str) -> dict | None:
    """Read a decision file if it exists. Returns None if not yet decided."""
    path = _permissions_dir(intern_agent) / "decisions" / f"{req_id}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def list_pending(intern_agent: str) -> list[dict]:
    """List all pending permission requests for an intern agent."""
    pending_dir = _permissions_dir(intern_agent) / "pending"
    if not pending_dir.exists():
        return []
    results = []
    for path in sorted(pending_dir.glob("perm_*.json")):
        try:
            with open(path) as f:
                results.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    return results


def archive_request(intern_agent: str, req_id: str):
    """Move completed request+decision to log directory."""
    perms = _permissions_dir(intern_agent)
    log_dir = perms / "log"
    log_dir.mkdir(parents=True, exist_ok=True)

    for subdir in ("pending", "decisions"):
        src = perms / subdir / f"{req_id}.json"
        if src.exists():
            dst = log_dir / f"{req_id}_{subdir}.json"
            src.rename(dst)
