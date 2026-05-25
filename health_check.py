#!/usr/bin/env python3
"""
MikeyV Infrastructure Health Check
====================================
Checks all comms infrastructure components and writes a JSON status file.
Can be run standalone or called by the dashboard.

Usage:
  python3 health_check.py              # Print to stdout
  python3 health_check.py --write      # Write to ~/asdaaas/health.json
  python3 health_check.py --watch 30   # Continuous mode, every 30s
"""

import json
import os
import subprocess
import sys
import time
import argparse
from pathlib import Path


HEALTH_FILE = Path(os.path.expanduser("~/asdaaas/health.json"))
COMPONENTS = {
    "miniircd": {
        "process": "miniircd",
        "description": "IRC server",
    },
    "hub": {
        "process": "mikeyv_hub.py",
        "description": "Message routing hub",
    },
    "irc_adapter": {
        "process": "irc_adapter.py",
        "description": "IRC transport adapter",
    },
    "slack_adapter": {
        "process": "slack_adapter.py",
        "description": "Slack transport adapter",
    },
}


def check_process(search_term):
    """Check if a process matching search_term is running. Returns (running, pid, uptime_info)."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", search_term],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            pids = result.stdout.strip().split("\n")
            # Get the main process (not the shell wrapper)
            for pid in pids:
                try:
                    # Get process start time
                    stat = subprocess.run(
                        ["ps", "-p", pid, "-o", "etimes=,cmd="],
                        capture_output=True, text=True, timeout=5
                    )
                    if stat.returncode == 0:
                        parts = stat.stdout.strip().split(None, 1)
                        if len(parts) >= 1:
                            elapsed_s = int(parts[0])
                            return True, int(pid), elapsed_s
                except (ValueError, subprocess.TimeoutExpired):
                    continue
            return True, int(pids[0]), -1
        return False, None, None
    except subprocess.TimeoutExpired:
        return False, None, None


def check_file_freshness(path, max_age_s=60):
    """Check if a file exists and was modified within max_age_s seconds."""
    try:
        stat = os.stat(path)
        age = time.time() - stat.st_mtime
        return age <= max_age_s, age
    except FileNotFoundError:
        return False, -1


def format_uptime(seconds):
    """Format seconds into human-readable uptime."""
    if seconds < 0:
        return "unknown"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    return f"{hours}h {mins}m"


def run_health_check():
    """Run all health checks and return status dict."""
    status = {
        "timestamp": time.time(),
        "components": {},
        "summary": {"total": 0, "healthy": 0, "unhealthy": 0},
    }

    for name, config in COMPONENTS.items():
        running, pid, uptime_s = check_process(config["process"])
        component = {
            "description": config["description"],
            "running": running,
            "pid": pid,
            "uptime": format_uptime(uptime_s) if uptime_s is not None else None,
            "uptime_seconds": uptime_s,
        }
        status["components"][name] = component
        status["summary"]["total"] += 1
        if running:
            status["summary"]["healthy"] += 1
        else:
            status["summary"]["unhealthy"] += 1

    # Check hub inbox/outbox for stuck messages
    inbox = Path(os.path.expanduser("~/asdaaas/inbox"))
    if inbox.exists():
        stuck = list(inbox.glob("*.json"))
        status["queues"] = {
            "inbox_pending": len(stuck),
        }
        if stuck:
            oldest_age = max(time.time() - f.stat().st_mtime for f in stuck)
            status["queues"]["inbox_oldest_age_s"] = round(oldest_age, 1)
    else:
        status["queues"] = {"inbox_pending": 0}

    # Check outbox directories
    outbox_base = Path(os.path.expanduser("~/asdaaas/outbox"))
    if outbox_base.exists():
        for adapter_dir in outbox_base.iterdir():
            if adapter_dir.is_dir():
                pending = list(adapter_dir.glob("*.json"))
                status["queues"][f"outbox_{adapter_dir.name}_pending"] = len(pending)

    # Check session registry
    reg_path = os.path.expanduser("~/.grok/session_registry.json")
    try:
        with open(reg_path) as f:
            reg = json.load(f)
        status["agents"] = {name: info.get("status", "unknown") for name, info in reg.items()}
    except Exception:
        status["agents"] = {}

    # Check hub log freshness (is the hub actually doing work?)
    hub_log = os.path.expanduser("~/.grok/infra_logs/hub.log")
    fresh, age = check_file_freshness(hub_log, max_age_s=120)
    status["hub_log"] = {
        "fresh": fresh,
        "age_seconds": round(age, 1) if age >= 0 else None,
    }

    # Overall health
    all_healthy = status["summary"]["unhealthy"] == 0
    no_stuck = status["queues"].get("inbox_pending", 0) == 0
    status["healthy"] = all_healthy and no_stuck

    return status


def print_status(status):
    """Pretty-print the health status."""
    print(f"MikeyV Health Check - {status['timestamp']}")
    print(f"{'=' * 50}")

    for name, comp in status["components"].items():
        icon = "OK" if comp["running"] else "DOWN"
        uptime = f" (up {comp['uptime']})" if comp.get("uptime") else ""
        pid_str = f" pid={comp['pid']}" if comp.get("pid") else ""
        print(f"  [{icon:>4}] {comp['description']:.<30} {name}{pid_str}{uptime}")

    print(f"\nQueues:")
    for key, val in status.get("queues", {}).items():
        print(f"  {key}: {val}")

    agents_str = ", ".join(f"{k}={v}" for k, v in status.get("agents", {}).items())
    print(f"\nAgents: {agents_str}")

    hub_log = status.get("hub_log", {})
    if hub_log.get("age_seconds") is not None:
        print(f"Hub log: {'fresh' if hub_log['fresh'] else 'STALE'} ({hub_log['age_seconds']:.0f}s ago)")

    overall = "HEALTHY" if status.get("healthy") else "UNHEALTHY"
    print(f"\nOverall: {overall}")


def main():
    parser = argparse.ArgumentParser(description="MikeyV Infrastructure Health Check")
    parser.add_argument("--write", action="store_true", help="Write status to health.json")
    parser.add_argument("--watch", type=int, metavar="SECONDS", help="Continuous mode")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    while True:
        status = run_health_check()

        if args.json:
            print(json.dumps(status, indent=2))
        else:
            print_status(status)

        if args.write:
            HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(HEALTH_FILE, "w") as f:
                json.dump(status, f, indent=2)

        if not args.watch:
            break

        time.sleep(args.watch)
        print()


if __name__ == "__main__":
    main()
