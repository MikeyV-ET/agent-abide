"""Coalesce high-frequency updates.jsonl events before TUI main-thread apply.

Latest-wins for tool_call_update per toolCallId within a batch.
Preserves order of non-mergeable events. Pure functions — unit tested.
"""
from __future__ import annotations

from typing import Any


def _session_update(event: dict) -> str:
    return (event.get("params") or {}).get("update", {}).get("sessionUpdate", "")


def _tool_id(event: dict) -> str:
    return (event.get("params") or {}).get("update", {}).get("toolCallId", "")


def coalesce_events(events: list[dict]) -> list[dict]:
    """Merge a batch of events for cheaper UI apply.

    Rules:
    - Consecutive agent_message_chunk / agent_thought_chunk: concatenate text
      into one event of the same type (same sessionUpdate).
    - tool_call_update with same toolCallId: keep only the latest in the batch
      (still in the position of the first occurrence of that id).
    - Other events: pass through in order; flush pending merges before them.
    """
    if not events:
        return []

    out: list[dict] = []
    # tool_id -> index in out
    tool_index: dict[str, int] = {}
    pending_chunks: list[dict] = []
    pending_type: str | None = None

    def flush_chunks():
        nonlocal pending_chunks, pending_type
        if not pending_chunks:
            return
        if len(pending_chunks) == 1:
            out.append(pending_chunks[0])
        else:
            # Merge content text
            texts = []
            base = pending_chunks[0]
            for e in pending_chunks:
                u = (e.get("params") or {}).get("update", {})
                c = u.get("content") or {}
                texts.append(c.get("text") or "")
            merged = {
                **base,
                "params": {
                    **(base.get("params") or {}),
                    "update": {
                        **((base.get("params") or {}).get("update") or {}),
                        "content": {"text": "".join(texts)},
                    },
                },
            }
            # shallow-safe: content may need type field
            orig_c = ((base.get("params") or {}).get("update") or {}).get("content") or {}
            if isinstance(orig_c, dict):
                merged["params"]["update"]["content"] = {**orig_c, "text": "".join(texts)}
            out.append(merged)
        pending_chunks = []
        pending_type = None

    for e in events:
        su = _session_update(e)
        if su in ("agent_message_chunk", "agent_thought_chunk"):
            if pending_type == su:
                pending_chunks.append(e)
            else:
                flush_chunks()
                pending_type = su
                pending_chunks = [e]
            continue

        flush_chunks()

        if su == "tool_call_update":
            tid = _tool_id(e)
            if tid and tid in tool_index:
                out[tool_index[tid]] = e
            elif tid:
                tool_index[tid] = len(out)
                out.append(e)
            else:
                out.append(e)
            continue

        # tool_call and others — clear tool_index for that id if new tool_call?
        if su == "tool_call":
            tid = _tool_id(e)
            if tid and tid in tool_index:
                del tool_index[tid]
            out.append(e)
            continue

        out.append(e)

    flush_chunks()
    return out
