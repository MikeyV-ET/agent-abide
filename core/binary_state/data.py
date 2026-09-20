"""Observer data file loaders."""
from __future__ import annotations

import json
import os

def load_known_types(data_dir: str) -> set[str]:
    """Load known event types from data file."""
    path = os.path.join(data_dir, "known_types.json")
    with open(path) as f:
        data = json.load(f)
    return set(data["types"])


def load_silence_windows(data_dir: str) -> tuple[dict[str, float], dict[str, float], float, int]:
    """Load silence windows from data file.

    Returns (by_tool_name, by_preceding_event, default, p95_events_per_turn).
    """
    path = os.path.join(data_dir, "silence_windows.json")
    with open(path) as f:
        data = json.load(f)
    return (
        data.get("by_tool_name", {}),
        data.get("by_preceding_event", {}),
        data.get("default", 60.0),
        data.get("orientation", {}).get("p95_events_per_turn", 200),
    )


# ============================================================================
# ObserverService -- main event loop
# ============================================================================

