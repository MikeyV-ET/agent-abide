"""Pure delay-command detection for [aa.control] chrome (tip fold + live)."""
from __future__ import annotations

import re
from typing import Optional


def delay_control_from_tool_blob(text: str) -> Optional[str]:
    """If tool payload registers a delay command, return [aa.control] line."""
    if not text or "delay" not in text:
        return None
    if not re.search(
        r"commands/cmd_[^\s\"']*\.json|commands/cmd_\$\{?date|commands/cmd_\$\(",
        text,
    ):
        return None
    if not re.search(
        r'["\']action["\']\s*:\s*["\']delay["\']|"action"\s*:\s*"delay"',
        text,
    ):
        return None
    writes = re.search(
        r"(cat|tee|printf|echo|open\().{0,200}commands/cmd_",
        text,
        re.I | re.S,
    )
    if re.search(r"\b(rg|grep|ripgrep)\b", text) and not writes:
        return None
    if not writes and not re.search(r"(>|>>).{0,40}commands/cmd_", text, re.S):
        return None
    sec = None
    m = re.search(
        r'["\']?seconds["\']?\s*:\s*["\']?(until_event|\d+(?:\.\d+)?)["\']?',
        text,
    )
    if m:
        sec = m.group(1)
    txt = None
    tm = re.search(r'["\']text["\']\s*:\s*["\']([^"\']{1,120})["\']', text)
    if tm:
        txt = tm.group(1)
    if sec == "until_event":
        detail = "until_event (standing by)"
    elif sec:
        detail = f"{sec}s before next continue"
    else:
        detail = "registered"
    if txt:
        detail = f"{detail} — {txt}"
    return f"[aa.control] delay: {detail}"


def tool_update_blob(update: dict) -> str:
    """Flatten tool update fields into searchable text."""
    parts = []
    for k in ("title", "kind", "command"):
        v = update.get(k)
        if v:
            parts.append(str(v))
    ri = update.get("rawInput")
    if isinstance(ri, dict):
        parts.append(str(ri.get("command") or ri.get("cmd") or ri))
    elif ri:
        parts.append(str(ri))
    content = update.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                inner = block.get("content") or {}
                if isinstance(inner, dict) and inner.get("text"):
                    parts.append(str(inner["text"]))
                t = block.get("text")
                if t:
                    parts.append(str(t))
    elif isinstance(content, str):
        parts.append(content)
    return "\n".join(parts)
