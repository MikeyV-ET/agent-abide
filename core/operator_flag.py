"""Operator-facing agent flag: waiting / error, plus auto working / idle.

Written by asdaaas command ``flag``. Read by the TUI to color tab names.
Does not use asdaaas/attention/ (that is sibling expect-response).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

FLAG_NAME = "operator_flag.json"
AGENT_STATUSES = ("waiting", "error", "clear")
TAB_STATUSES = ("idle", "working", "waiting", "error")


def flag_path(asdaaas_dir: Path | str) -> Path:
    return Path(asdaaas_dir) / FLAG_NAME


def write_flag(
    asdaaas_dir: Path | str,
    status: str,
    note: str = "",
    *,
    agent: str = "",
) -> Optional[Path]:
    """Write or clear the operator flag. ``clear`` deletes the file."""
    status = (status or "").strip().lower()
    if status in ("clear", "idle", "none", "off"):
        p = flag_path(asdaaas_dir)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    if status not in ("waiting", "error"):
        raise ValueError(f"flag status must be waiting, error, or clear (got {status!r})")
    p = flag_path(asdaaas_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "note": note or "",
        "agent": agent,
        "ts": time.time(),
    }
    p.write_text(json.dumps(payload, indent=2) + "\n")
    return p


def read_flag(asdaaas_dir: Path | str) -> Optional[dict[str, Any]]:
    p = flag_path(asdaaas_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _auto_from_health(health: Optional[dict], bstate: Optional[dict]) -> str:
    """idle | working | error from asdaaas health + binary_state."""
    h = health or {}
    b = bstate or {}
    obs = h.get("observer") if isinstance(h.get("observer"), dict) else {}
    st = str(b.get("state") or obs.get("state") or "").upper()
    if b.get("doom_loop") or obs.get("doom_loop"):
        return "error"
    if st == "STUCK":
        return "error"
    hstatus = str(h.get("status") or "").lower()
    if hstatus in ("error", "fail", "failed", "dead"):
        return "error"
    if st == "BUSY" or hstatus in ("working", "busy"):
        return "working"
    return "idle"


def derive_tab_status(
    *,
    flag: Optional[dict] = None,
    health: Optional[dict] = None,
    binary_state: Optional[dict] = None,
) -> str:
    """error > waiting > working > idle."""
    auto = _auto_from_health(health, binary_state)
    fstatus = str((flag or {}).get("status") or "").lower()
    if fstatus == "error" or auto == "error":
        return "error"
    if fstatus == "waiting":
        return "waiting"
    if auto == "working":
        return "working"
    return "idle"


def tab_status_for(asdaaas_dir: Path | str) -> str:
    """Read flag + health + binary_state from an asdaaas dir."""
    d = Path(asdaaas_dir)
    flag = read_flag(d)
    health = None
    bstate = None
    hp = d / "health.json"
    if hp.is_file():
        try:
            health = json.loads(hp.read_text())
        except Exception:
            health = None
    bp = d / "binary_state.json"
    if bp.is_file():
        try:
            bstate = json.loads(bp.read_text())
        except Exception:
            bstate = None
    return derive_tab_status(flag=flag, health=health, binary_state=bstate)
