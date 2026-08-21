#!/usr/bin/env python3
"""
Heartbeat Adapter (Phase 6.3) — Idle nudge doorbells for agents.
================================================================
Notify adapter type. Watches agent health files for idle time and sends
periodic nudge doorbells to keep agents aware of time passing.

This addresses the "spontaneous initiative" problem — agents on asdaaas
have no sense of time passing when idle. Without external prompting, they
sit silently forever.

Behavior:
  - Watches last_activity timestamp in health files
  - After idle_threshold seconds of no activity, sends a nudge doorbell
  - Continues sending nudges at nudge_interval until agent becomes active
  - Nudge text includes idle duration and a gentle prompt

Doorbell format:
  {
    "adapter": "heartbeat",
    "priority": 5,
    "text": "You've been idle for 15 minutes. Last activity: 2026-04-07T14:00:00. Any tasks pending?",
    "idle_seconds": 900,
    "ts": "..."
  }

Usage:
    python3 heartbeat_adapter.py                                    # defaults
    python3 heartbeat_adapter.py --agents Trip,Q,Cinco              # specific agents
    python3 heartbeat_adapter.py --idle-threshold 600 --nudge-interval 300
"""

import json
import os
import sys
import time
import tempfile
import argparse
from pathlib import Path

def tprint(msg):
    """Timestamped print."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

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

# Default values (used when agent has no preference in awareness file)
DEFAULT_IDLE_THRESHOLD = 900   # 15 min before first nudge
DEFAULT_NUDGE_INTERVAL = 600   # 10 min between subsequent nudges

# ============================================================================
# IDLE TRACKER
# ============================================================================

class IdleTracker:
    """Track idle time for each agent and decide when to nudge."""
    
    def __init__(self, idle_threshold=None, nudge_interval=None):
        self.default_idle_threshold = idle_threshold or DEFAULT_IDLE_THRESHOLD
        self.default_nudge_interval = nudge_interval or DEFAULT_NUDGE_INTERVAL
        # {agent: last_nudge_time}
        self.last_nudge = {}
    
    def check(self, agent, health, idle_threshold=None, nudge_interval=None):
        """Check if this agent needs a nudge.
        
        Args:
            agent: Agent name
            health: Agent health dict
            idle_threshold: Per-agent override (seconds before first nudge)
            nudge_interval: Per-agent override (seconds between nudges)
        
        Returns (should_nudge, idle_seconds) tuple.
        
        Note: nudge_interval is enforced as a minimum gap between nudges,
        even if the agent responded to a nudge and reset the idle timer.
        Without this, agents that respond to nudges would get re-nudged
        every idle_threshold seconds (because the response resets idle),
        making nudge_interval effectively irrelevant.
        """
        threshold = idle_threshold if idle_threshold is not None else self.default_idle_threshold
        interval = nudge_interval if nudge_interval is not None else self.default_nudge_interval
        
        last_activity = health.get("last_activity")
        if not last_activity:
            return False, 0
        
        try:
            idle_seconds = time.time() - float(last_activity)
        except (ValueError, TypeError):
            return False, 0
        
        # Not idle enough yet
        if idle_seconds < threshold:
            return False, idle_seconds
        
        # Idle enough -- but have we nudged too recently?
        # nudge_interval is enforced as a minimum gap between nudges,
        # regardless of whether the agent responded in between.
        now_mono = time.monotonic()
        last = self.last_nudge.get(agent, 0)
        
        if last == 0:
            # First nudge ever for this agent
            self.last_nudge[agent] = now_mono
            tprint(f"[heartbeat] DEBUG {agent}: first nudge (idle {idle_seconds:.0f}s, threshold {threshold}s, interval {interval}s)")
            return True, idle_seconds
        
        elapsed = now_mono - last
        if elapsed >= interval:
            # Enough time since last nudge
            self.last_nudge[agent] = now_mono
            tprint(f"[heartbeat] DEBUG {agent}: interval elapsed ({elapsed:.0f}s >= {interval}s, idle {idle_seconds:.0f}s)")
            return True, idle_seconds
        
        return False, idle_seconds


# ============================================================================
# DOORBELL WRITING
# ============================================================================

def format_idle_time(seconds):
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)} seconds"
    elif seconds < 3600:
        mins = int(seconds / 60)
        return f"{mins} minute{'s' if mins != 1 else ''}"
    else:
        hours = int(seconds / 3600)
        mins = int((seconds % 3600) / 60)
        if mins > 0:
            return f"{hours} hour{'s' if hours != 1 else ''} {mins} min"
        return f"{hours} hour{'s' if hours != 1 else ''}"


def ring_heartbeat_doorbell(agent, idle_seconds):
    """Write a heartbeat nudge doorbell for an agent."""
    bell_dir = (config.resolve_asdaaas_dir(agent, AGENTS_HOME_DIR) / "doorbells")
    bell_dir.mkdir(parents=True, exist_ok=True)
    
    duration = format_idle_time(idle_seconds)
    last_active = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - idle_seconds))
    
    bell = {
        "adapter": "heartbeat",
        "priority": 5,  # low priority — informational
        "text": f"You've been idle for {duration}. Last activity: {last_active}. Any tasks pending or observations to record?",
        "idle_seconds": int(idle_seconds),
        "last_activity": time.time() - idle_seconds,
        "ts": time.time(),
    }
    
    fd, tmp_path = tempfile.mkstemp(dir=str(bell_dir), suffix=".tmp", prefix="hb_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(bell, f)
        final = tmp_path.replace(".tmp", ".json")
        os.rename(tmp_path, final)
        tprint(f"[heartbeat] NUDGE: {agent} idle {duration}")
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
    """Read an agent's health file. Returns dict or None."""
    health_file = config.resolve_asdaaas_dir(agent, AGENTS_HOME_DIR) / "health.json"
    if not health_file.exists():
        return None
    try:
        with open(health_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def read_agent_awareness(agent):
    """Read an agent's awareness file. Returns dict or empty dict."""
    awareness_file = config.resolve_asdaaas_dir(agent, AGENTS_HOME_DIR) / "awareness.json"
    try:
        with open(awareness_file) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def get_heartbeat_prefs(awareness):
    """Extract heartbeat preferences from awareness dict.
    
    Awareness file format:
        {
            "heartbeat": {
                "idle_threshold": 1800,
                "nudge_interval": 3600
            }
        }
    
    idle_threshold: seconds of idle before the FIRST nudge fires.
    nudge_interval: minimum seconds between nudges (even if agent responded
                    to a nudge, the next one won't fire until this much time
                    has passed since the last nudge).
    
    For "nudge me once an hour": set nudge_interval to 3600.
    idle_threshold controls how long the agent must be idle before the
    first nudge, but subsequent nudges are gated by nudge_interval.
    
    Returns (idle_threshold, nudge_interval) -- either may be None if not set.
    """
    hb = awareness.get("heartbeat", {})
    if not isinstance(hb, dict):
        return None, None
    idle_threshold = hb.get("idle_threshold")
    nudge_interval = hb.get("nudge_interval")
    # Validate: must be positive numbers
    if idle_threshold is not None and (not isinstance(idle_threshold, (int, float)) or idle_threshold <= 0):
        idle_threshold = None
    if nudge_interval is not None and (not isinstance(nudge_interval, (int, float)) or nudge_interval <= 0):
        nudge_interval = None
    return idle_threshold, nudge_interval


# ============================================================================
# WATCH LOOP
# ============================================================================

def watch_loop(agents, idle_threshold=None, nudge_interval=None, poll_interval=30.0):
    """Main loop: watch health files, send idle nudges.
    
    Per-agent preferences from awareness files override CLI defaults.
    """
    effective_idle = idle_threshold or DEFAULT_IDLE_THRESHOLD
    effective_nudge = nudge_interval or DEFAULT_NUDGE_INTERVAL
    tprint(f"[heartbeat] Heartbeat adapter starting")
    tprint(f"[heartbeat] Watching agents: {', '.join(agents)}")
    tprint(f"[heartbeat] Default idle threshold: {effective_idle}s, nudge interval: {effective_nudge}s")
    tprint(f"[heartbeat] Per-agent overrides read from awareness files")
    
    # Register adapter
    adapter_api.register_adapter(
        name="heartbeat",
        capabilities=["notify"],
        config={
            "type": "notify",
            "agents": agents,
            "default_idle_threshold": effective_idle,
            "default_nudge_interval": effective_nudge,
            "per_agent": True,
        },
    )
    
    tracker = IdleTracker(idle_threshold, nudge_interval)
    heartbeat_update_interval = 30
    last_heartbeat = time.time()
    
    while True:
        try:
            for agent in agents:
                health = read_agent_health(agent)
                if health is None:
                    continue
                
                # Only nudge agents that are "ready" or "active" (not "error")
                status = health.get("status", "")
                if status not in ("ready", "active"):
                    continue
                
                # Read per-agent preferences from awareness file
                awareness = read_agent_awareness(agent)
                agent_idle, agent_nudge = get_heartbeat_prefs(awareness)
                
                should_nudge, idle_seconds = tracker.check(
                    agent, health,
                    idle_threshold=agent_idle,
                    nudge_interval=agent_nudge,
                )
                if should_nudge:
                    ring_heartbeat_doorbell(agent, idle_seconds)
            
            # Heartbeat
            now = time.time()
            if now - last_heartbeat >= heartbeat_update_interval:
                adapter_api.update_heartbeat("heartbeat")
                last_heartbeat = now
        
        except Exception as e:
            tprint(f"[heartbeat] Error: {e}")
            import traceback
            traceback.print_exc()
        
        time.sleep(poll_interval)


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="MikeyV Heartbeat Adapter (Phase 6.3)")
    parser.add_argument("--agents", default=None, help="Comma-separated agent list (default: all)")
    parser.add_argument("--idle-threshold", type=int, default=None, help=f"Default seconds before first nudge (default: {DEFAULT_IDLE_THRESHOLD})")
    parser.add_argument("--nudge-interval", type=int, default=None, help=f"Default seconds between nudges (default: {DEFAULT_NUDGE_INTERVAL})")
    parser.add_argument("--poll-interval", type=float, default=30.0, help="Poll interval in seconds")
    args = parser.parse_args()
    
    if args.agents:
        agents = [a.strip() for a in args.agents.split(",")]
    else:
        agents = list(ALL_AGENTS)
    
    try:
        watch_loop(agents, args.idle_threshold, args.nudge_interval, args.poll_interval)
    except KeyboardInterrupt:
        print("\n[heartbeat] Shutting down.")
        adapter_api.deregister_adapter("heartbeat")


if __name__ == "__main__":
    main()
