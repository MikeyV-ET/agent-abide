"""Shared binary-activity types (backend-agnostic).

Grok and Claude mappers emit ActivityEvent; BinaryActivityMachine consumes it.
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any, Optional


class ObserverState(enum.Enum):
    STARTING = "STARTING"
    IDLE = "IDLE"
    BUSY = "BUSY"
    RETRYING = "RETRYING"
    STUCK = "STUCK"
    GATE = "GATE"
    GONE = "GONE"
    UNKNOWN = "UNKNOWN"


class ActivityKind(enum.Enum):
    """Normalized activity vocabulary (not grok sessionUpdate names)."""

    TURN_START = "turn_start"  # user speech / turn begins
    TURN_END = "turn_end"  # binary finished a turn
    SPEECH = "speech"  # assistant text
    THOUGHT = "thought"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"  # completed or failed
    RETRYING = "retrying"
    RETRY_FAILED = "retry_failed"
    DOOM = "doom"
    KNOWN_OTHER = "known_other"  # recognized but no special transition
    UNKNOWN = "unknown"  # not in backend known-types
    # stdout / control plane (optional)
    MODEL_INFO = "model_info"
    SESSION_ACTIVITY = "session_activity"


# Interactive gates → GATE instead of STUCK when silence exceeded
GATE_TOOL_KINDS = frozenset({"exit_plan", "ask_user"})

DEFAULT_SILENCE_WINDOW = 60.0  # seconds; may be overwritten from silence_windows.json
TIMEOUT_BUFFER = 1.5
STATE_TTL = 1.0  # state file freshness


@dataclass
class ActivityEvent:
    """One normalized activity tick from any backend mapper.

    Mappers (GrokBinary… / ClaudeBinary…) produce these.
    BinaryActivityMachine is the only consumer of the state transitions.
    """

    kind: ActivityKind
    ts: float = field(default_factory=time.time)
    # Original type label for debugging / state_dict last_event_type
    source_type: str = ""
    known: bool = True

    tool_id: Optional[str] = None
    tool_kind: str = "unknown"
    tool_title: str = ""
    raw_input: Any = None
    # If set, machine uses this as expected silence after TOOL_START
    silence_s: Optional[float] = None

    retry_attempt: Optional[int] = None
    retry_reason: Optional[str] = None

    model_id: Optional[str] = None
    reasoning_effort: Optional[str] = None
    activity: Optional[str] = None  # session activity string

    # opaque breadcrumb (native frame ref) — never required by machine
    native: Any = None
