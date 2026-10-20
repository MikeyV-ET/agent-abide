"""Pure delay-command detection for [aa.control] chrome (tip fold + live)."""
from __future__ import annotations

import re
from typing import Optional, Tuple

_ACTION_DELAY = re.compile(
    r"""["']action["']\s*:\s*["']delay["']""",
    re.I,
)
_SECONDS = re.compile(
    r"""["']seconds["']\s*:\s*["']?(until_event|\d+(?:\.\d+)?)["']?""",
    re.I,
)
_TEXT = re.compile(
    r"""["']text["']\s*:\s*["']([^"']{1,120})["']""",
    re.I,
)


def _seconds_and_text_from_delay_objects(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse seconds/text from delay *command objects*, not stray seconds: in notes.

    Last matching delay object wins (the cmd file write is usually last in the blob).
    """
    found: list[Tuple[str, Optional[str]]] = []
    for m in _ACTION_DELAY.finditer(text):
        window = text[max(0, m.start() - 400) : m.end() + 500]
        sm = _SECONDS.search(window)
        if not sm:
            continue
        tm = _TEXT.search(window)
        found.append((sm.group(1), tm.group(1) if tm else None))
    if not found:
        return None, None
    return found[-1]


def delay_control_from_tool_blob(text: str) -> Optional[str]:
    """If tool payload registers a delay command, return [aa.control] line."""
    if not text or "delay" not in text:
        return None
    literal_cmd = re.search(
        r"commands/cmd_[^\s\"']*\.json|commands/cmd_\$\{?date|commands/cmd_\$\(",
        text,
    )
    # Claude/Astro: dir in a variable, then open(f'{d}/cmd_{ts}_….json')
    split_cmd = (
        "asdaaas/commands" in text
        and re.search(r"cmd_[\$\{\w]", text)
        and re.search(r"json\.dump|open\(", text)
    )
    if not literal_cmd and not split_cmd:
        return None
    if not _ACTION_DELAY.search(text):
        return None
    writes = re.search(
        r"(cat|tee|printf|echo|open\().{0,200}commands/cmd_",
        text,
        re.I | re.S,
    )
    if re.search(r"\b(rg|grep|ripgrep)\b", text) and not writes and not split_cmd:
        return None
    if (
        not writes
        and not re.search(r"(>|>>).{0,40}commands/cmd_", text, re.S)
        and not split_cmd
    ):
        return None
    sec, txt = _seconds_and_text_from_delay_objects(text)
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
