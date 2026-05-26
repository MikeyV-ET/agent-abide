#!/usr/bin/env python3
"""
Context Adapter (Phase 6.1) — Token threshold doorbell notifications.
=====================================================================
Notify adapter type. Watches agent health files for totalTokens and sends
doorbell notifications when context usage crosses configurable thresholds.

This is how agents learn they're approaching compaction without having to
check themselves. The adapter fires once per threshold crossing per direction
(up only — crossing back down after compaction resets the tracker).

Thresholds (default):
  45% — informational, "you're approaching half"
  65% — advisory, "start wrapping up large tasks"  
  80% — warning, "flush state to disk now"
  88% — critical, "compaction imminent, stop new work"

Doorbell format:
  {
    "adapter": "context",
    "priority": <varies>,
    "text": "Context at 65% (130000/200000 tokens). Advisory: start wrapping up large tasks.",
    "threshold": 65,
    "totalTokens": 130000,
    "contextWindow": 200000,
    "ts": "..."
  }

Usage:
    python3 context_adapter.py                          # watch all agents
    python3 context_adapter.py --agents Trip,Q,Cinco    # watch specific agents
    python3 context_adapter.py --poll-interval 5        # check every 5s (default 5)
"""

import json
import os
import sys
import time
import tempfile
import argparse
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))
import adapter_api

# ============================================================================
# PATHS
# ============================================================================

try:
    from asdaaas_config import config
except ModuleNotFoundError:
    import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'core'))
    from asdaaas_config import config
HUB_DIR = config.hub_dir
AGENTS_DIR = HUB_DIR / "agents"  # legacy
AGENTS_HOME_DIR = config.agents_home
# Legacy aliases for test monkeypatching
HEALTH_DIR = AGENTS_DIR
AWARENESS_DIR = AGENTS_DIR
DOORBELL_DIR = AGENTS_DIR

ALL_AGENTS = ["Sr", "Jr", "Trip", "Q", "Cinco"]

# ============================================================================
# THRESHOLD CONFIG
# ============================================================================

DEFAULT_THRESHOLDS = [
    {"pct": 80, "priority": 3, "level": "reminder", "advice": "ensure current work is committed and documented (before/after). Keep working"},
]


# ============================================================================
# THRESHOLD TRACKER
# ============================================================================

class ThresholdTracker:
    """Track which thresholds have been crossed for each agent.
    
    Fires once per threshold per upward crossing. Resets when usage drops
    (e.g., after compaction reduces tokens).
    """
    
    def __init__(self, thresholds=None):
        self.default_thresholds = thresholds or DEFAULT_THRESHOLDS
        # {agent: set of threshold pcts that have been fired}
        self.fired = {}
        # {agent: last known pct} -- for detecting downward crossings (reset)
        self.last_pct = {}
    
    def check(self, agent, total_tokens, context_window, thresholds=None):
        """Check if any thresholds need to fire for this agent.
        
        Args:
            agent: Agent name
            total_tokens: Current token count
            context_window: Total context window size
            thresholds: Per-agent threshold list override (list of dicts with pct/priority/level/advice)
        
        Returns list of threshold dicts that should fire (newly crossed).
        """
        if context_window <= 0:
            return []
        
        active_thresholds = thresholds if thresholds is not None else self.default_thresholds
        
        pct = (total_tokens / context_window) * 100
        
        # Initialize tracking for new agents
        if agent not in self.fired:
            self.fired[agent] = set()
            self.last_pct[agent] = 0
        
        # Detect downward crossing (compaction happened) -- reset fired thresholds
        # If usage dropped by more than 20 percentage points, assume compaction
        if pct < self.last_pct[agent] - 20:
            old_pct = self.last_pct[agent]
            self.fired[agent] = set()
            print(f"[context] {agent}: usage dropped {old_pct:.0f}% -> {pct:.0f}%, resetting thresholds")
        
        self.last_pct[agent] = pct
        
        # Check which thresholds to fire
        to_fire = []
        for t in active_thresholds:
            if pct >= t["pct"] and t["pct"] not in self.fired[agent]:
                self.fired[agent].add(t["pct"])
                to_fire.append(t)
        
        return to_fire


# ============================================================================
# DOORBELL WRITING
# ============================================================================

def ring_context_doorbell(agent, threshold, total_tokens, context_window):
    """Write a context threshold doorbell for an agent."""
    bell_dir = AGENTS_HOME_DIR / agent / "asdaaas" / "doorbells"
    bell_dir.mkdir(parents=True, exist_ok=True)
    
    pct = (total_tokens / context_window) * 100 if context_window > 0 else 0
    
    bell = {
        "adapter": "context",
        "priority": threshold["priority"],
        "text": f"Context at {pct:.0f}% ({total_tokens}/{context_window} tokens). "
                f"{threshold['level'].upper()}: {threshold['advice']}.",
        "threshold": threshold["pct"],
        "level": threshold["level"],
        "totalTokens": total_tokens,
        "contextWindow": context_window,
        "ts": time.time(),
    }
    
    fd, tmp_path = tempfile.mkstemp(dir=str(bell_dir), suffix=".tmp", prefix="ctx_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(bell, f)
        final = tmp_path.replace(".tmp", ".json")
        os.rename(tmp_path, final)
        print(f"[context] DOORBELL: {agent} at {pct:.0f}% — {threshold['level']}")
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ============================================================================
# HEALTH + AWARENESS FILE READING
# ============================================================================

def read_agent_health(agent):
    """Read an agent's health file. Returns (totalTokens, contextWindow) or None."""
    health_file = AGENTS_HOME_DIR / agent / "asdaaas" / "health.json"
    if not health_file.exists():
        return None
    try:
        with open(health_file) as f:
            health = json.load(f)
        total = health.get("totalTokens", 0)
        window = health.get("contextWindow", 200000)
        return total, window
    except (json.JSONDecodeError, OSError):
        return None


def read_agent_awareness(agent):
    """Read an agent's awareness file. Returns dict or empty dict."""
    awareness_file = AGENTS_HOME_DIR / agent / "asdaaas" / "awareness.json"
    try:
        with open(awareness_file) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


# Default advice text for thresholds generated from awareness percentages
_LEVEL_MAP = [
    (75, 3, "reminder",  "ensure current work is committed and documented (before/after). Keep working"),
    (0,  5, "info",      "informational checkpoint"),
]


def _level_for_pct(pct):
    """Return (priority, level, advice) for a given threshold percentage."""
    for cutoff, priority, level, advice in _LEVEL_MAP:
        if pct >= cutoff:
            return priority, level, advice
    return 5, "info", "informational checkpoint"


def get_context_thresholds(awareness):
    """Extract context threshold preferences from awareness dict.
    
    Awareness file format:
        {
            "context_thresholds": [30, 50, 75, 90]
        }
    
    Returns list of threshold dicts (same format as DEFAULT_THRESHOLDS), or None if not set.
    Each percentage gets auto-assigned a priority/level/advice based on how high it is.
    """
    raw = awareness.get("context_thresholds")
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    
    thresholds = []
    for pct in raw:
        if not isinstance(pct, (int, float)) or pct <= 0 or pct > 100:
            continue
        priority, level, advice = _level_for_pct(pct)
        thresholds.append({
            "pct": pct,
            "priority": priority,
            "level": level,
            "advice": advice,
        })
    
    if not thresholds:
        return None
    
    # Sort by percentage ascending
    thresholds.sort(key=lambda t: t["pct"])
    return thresholds


# ============================================================================
# WATCH LOOP
# ============================================================================

def watch_loop(agents, poll_interval=5.0):
    """Main loop: watch health files, fire threshold doorbells.
    
    Per-agent threshold preferences from awareness files override defaults.
    """
    print(f"[context] Context adapter starting")
    print(f"[context] Watching agents: {', '.join(agents)}")
    print(f"[context] Default thresholds: {[t['pct'] for t in DEFAULT_THRESHOLDS]}%")
    print(f"[context] Per-agent overrides read from awareness files")
    print(f"[context] Poll interval: {poll_interval}s")
    
    # Register adapter
    adapter_api.register_adapter(
        name="context",
        capabilities=["notify"],
        config={
            "type": "notify",
            "agents": agents,
            "default_thresholds": [t["pct"] for t in DEFAULT_THRESHOLDS],
            "per_agent": True,
        },
    )
    
    tracker = ThresholdTracker()
    heartbeat_interval = 30
    last_heartbeat = time.time()
    
    while True:
        try:
            for agent in agents:
                result = read_agent_health(agent)
                if result is None:
                    continue
                
                total_tokens, context_window = result
                
                # Read per-agent threshold preferences from awareness file
                awareness = read_agent_awareness(agent)
                agent_thresholds = get_context_thresholds(awareness)
                
                to_fire = tracker.check(agent, total_tokens, context_window, thresholds=agent_thresholds)
                
                for threshold in to_fire:
                    ring_context_doorbell(agent, threshold, total_tokens, context_window)
            
            # Heartbeat
            now = time.time()
            if now - last_heartbeat >= heartbeat_interval:
                adapter_api.update_heartbeat("context")
                last_heartbeat = now
        
        except Exception as e:
            print(f"[context] Error: {e}")
            import traceback
            traceback.print_exc()
        
        time.sleep(poll_interval)


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="MikeyV Context Adapter (Phase 6.1)")
    parser.add_argument("--agents", default=None, help="Comma-separated agent list (default: all)")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="Poll interval in seconds")
    args = parser.parse_args()
    
    if args.agents:
        agents = [a.strip() for a in args.agents.split(",")]
    else:
        agents = list(ALL_AGENTS)
    
    try:
        watch_loop(agents, args.poll_interval)
    except KeyboardInterrupt:
        print("\n[context] Shutting down.")
        adapter_api.deregister_adapter("context")


if __name__ == "__main__":
    main()
