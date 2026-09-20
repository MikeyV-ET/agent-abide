"""Binary activity observation — adapt native → ActivityEvent → common machine.

Grok: binary_state.grok
Claude: binary_state.claude
"""
from binary_state.types import ActivityEvent, ActivityKind, ObserverState
from binary_state.machine import BinaryActivityMachine
from binary_state.grok import GrokBinaryStateObserver, map_grok_updates_frame
from binary_state.claude import (
    ClaudeBinaryStateObserver,
    map_claude_session_line,
    map_claude_session_lines,
)
from binary_state.tail import UpdatesJSONLTailer
from binary_state.data import load_known_types, load_silence_windows
from binary_state.service import (
    ClaudeInProcessObserver,
    InProcessObserver,
    ObserverService,
)

# Legacy alias — grok edge is the historical BinaryStateObserver
BinaryStateObserver = GrokBinaryStateObserver

__all__ = [
    "ActivityEvent",
    "ActivityKind",
    "ObserverState",
    "BinaryActivityMachine",
    "GrokBinaryStateObserver",
    "ClaudeBinaryStateObserver",
    "BinaryStateObserver",
    "map_grok_updates_frame",
    "map_claude_session_line",
    "map_claude_session_lines",
    "UpdatesJSONLTailer",
    "load_known_types",
    "load_silence_windows",
    "ObserverService",
    "InProcessObserver",
    "ClaudeInProcessObserver",
]
