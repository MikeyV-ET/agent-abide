"""Pure helpers for updates.jsonl event shapes. No Textual dependency."""
from __future__ import annotations
from typing import Any, Optional


def session_update(event: dict) -> str:
    return (event.get("params") or {}).get("update", {}).get("sessionUpdate", "")


def update_payload(event: dict) -> dict:
    return (event.get("params") or {}).get("update", {}) or {}


def chunk_text(event: dict) -> str:
    content = update_payload(event).get("content") or {}
    if isinstance(content, dict):
        return content.get("text") or ""
    return ""


def tool_call_id(event: dict) -> str:
    return update_payload(event).get("toolCallId") or ""


def is_stream_chunk(event: dict) -> bool:
    return session_update(event) in ("agent_message_chunk", "agent_thought_chunk")
