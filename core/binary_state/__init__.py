"""Binary activity observation — adapt native → ActivityEvent → common machine.

Grok: binary_state.grok
Claude: binary_state.claude (stub for Opus)
"""
from binary_state.types import ActivityEvent, ActivityKind, ObserverState
from binary_state.machine import BinaryActivityMachine
from binary_state.grok import GrokBinaryStateObserver, map_grok_updates_frame
from binary_state.claude import ClaudeBinaryStateObserver, map_claude_session_line
from binary_state.tail import UpdatesJSONLTailer
from binary_state.data import load_known_types, load_silence_windows
from binary_state.service import ObserverService, InProcessObserver

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
    "UpdatesJSONLTailer",
    "load_known_types",
    "load_silence_windows",
    "ObserverService",
    "InProcessObserver",
]
