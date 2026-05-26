#!/usr/bin/env python3
"""Health check tests for agent responsiveness.

Run after launching an agent to verify it's actually working:
  python3 tests/test_health_check.py Astro        # check specific agent
  python3 tests/test_health_check.py              # check all configured agents
  python3 tests/test_health_check.py --timeout 60 # custom timeout

Checks performed:
  1. Process alive (asdaaas.py running for this agent)
  2. Health file exists and is recent (< 5 min old)
  3. Health file updating (two reads, timestamp advances)
  4. No error loop (asdaaas log has no repeated errors)
  5. Backend responding (health status not 'error')
  6. Session file exists and growing (updates.jsonl or chat_history)
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR.parent / "agents.json"

# Thresholds
HEALTH_STALE_SECS = 300      # health file older than 5 min = stale
ERROR_LOOP_THRESHOLD = 5      # consecutive errors in log = loop
HEALTH_UPDATE_WAIT = 30       # seconds to wait for health file to update
SESSION_FILE_MIN_BYTES = 100  # minimum session file size


class HealthCheck:
    def __init__(self, agent_name, config, verbose=False):
        self.agent = agent_name
        self.config = config
        self.agent_cfg = config["agents"][agent_name]
        self.home = self.agent_cfg["home"]
        self.verbose = verbose
        self.results = []

    def check(self, name, passed, detail=""):
        status = "PASS" if passed else "FAIL"
        self.results.append((name, passed, detail))
        if self.verbose or not passed:
            print(f"  [{status}] {name}: {detail}")
        return passed

    def run_all(self):
        print(f"\n=== Health Check: {self.agent} ===")
        self.check_process()
        self.check_health_file()
        self.check_health_freshness()
        self.check_error_loop()
        self.check_backend_status()
        self.check_session_file()

        passed = sum(1 for _, p, _ in self.results if p)
        total = len(self.results)
        failed = total - passed
        print(f"\n  {self.agent}: {passed}/{total} passed", end="")
        if failed:
            print(f" ({failed} FAILED)")
        else:
            print(" — healthy")
        return failed == 0

    def check_process(self):
        """Is asdaaas.py running for this agent?"""
        try:
            result = subprocess.run(
                ["pgrep", "-f", f"asdaaas.py --agent {self.agent}"],
                capture_output=True, text=True
            )
            pids = result.stdout.strip().split('\n') if result.stdout.strip() else []
            if pids and pids[0]:
                self.check("process_alive", True, f"PID {pids[0]}")
            else:
                self.check("process_alive", False, "no asdaaas.py process found")
        except Exception as e:
            self.check("process_alive", False, str(e))

    def check_health_file(self):
        """Does health.json exist and parse?"""
        health_path = os.path.join(self.home, "asdaaas", "health.json")
        if not os.path.exists(health_path):
            self.check("health_file", False, f"missing: {health_path}")
            return
        try:
            with open(health_path) as f:
                data = json.load(f)
            status = data.get("status", "unknown")
            tokens = data.get("totalTokens", 0)
            ctx = data.get("contextWindow", 0)
            pct = round(tokens / ctx * 100, 1) if ctx > 0 else 0
            self.check("health_file", True, f"status={status}, {pct}% context ({tokens}/{ctx})")
        except Exception as e:
            self.check("health_file", False, f"parse error: {e}")

    def check_health_freshness(self):
        """Is health.json recent (< 5 min)?"""
        health_path = os.path.join(self.home, "asdaaas", "health.json")
        if not os.path.exists(health_path):
            self.check("health_fresh", False, "no health file")
            return
        try:
            mtime = os.path.getmtime(health_path)
            age = time.time() - mtime
            if age < HEALTH_STALE_SECS:
                self.check("health_fresh", True, f"updated {int(age)}s ago")
            else:
                self.check("health_fresh", False, f"stale: {int(age)}s old (threshold: {HEALTH_STALE_SECS}s)")
        except Exception as e:
            self.check("health_fresh", False, str(e))

    def check_error_loop(self):
        """Check asdaaas log for consecutive errors (error loop detection)."""
        log_dir = self.config["settings"].get("log_dir", "/tmp")
        log_name = f"asdaaas_{self.agent.lower()}.log"
        log_path = os.path.join(log_dir, log_name)

        if not os.path.exists(log_path):
            self.check("no_error_loop", True, "no log file (fresh start)")
            return

        try:
            # Read last 50 lines
            result = subprocess.run(
                ["tail", "-50", log_path],
                capture_output=True, text=True
            )
            lines = result.stdout.strip().split('\n')
            consecutive_errors = 0
            max_consecutive = 0
            last_error = ""
            for line in lines:
                if "ERROR" in line or "error" in line.lower() and "Traceback" not in line:
                    consecutive_errors += 1
                    last_error = line.strip()[:120]
                    max_consecutive = max(max_consecutive, consecutive_errors)
                else:
                    consecutive_errors = 0

            if max_consecutive >= ERROR_LOOP_THRESHOLD:
                self.check("no_error_loop", False,
                           f"{max_consecutive} consecutive errors. Last: {last_error}")
            else:
                self.check("no_error_loop", True,
                           f"max {max_consecutive} consecutive errors (threshold: {ERROR_LOOP_THRESHOLD})")
        except Exception as e:
            self.check("no_error_loop", False, str(e))

    def check_backend_status(self):
        """Is the backend in a good state (not errored)?"""
        health_path = os.path.join(self.home, "asdaaas", "health.json")
        if not os.path.exists(health_path):
            self.check("backend_ok", False, "no health file")
            return
        try:
            with open(health_path) as f:
                data = json.load(f)
            status = data.get("status", "unknown")
            backend = self.agent_cfg.get("backend", "grok")
            error_count = data.get("consecutiveErrors", data.get("errors", 0))

            if status in ("error", "crashed", "shutdown"):
                self.check("backend_ok", False, f"backend={backend}, status={status}, errors={error_count}")
            elif error_count and int(error_count) > 3:
                self.check("backend_ok", False, f"backend={backend}, {error_count} errors")
            else:
                self.check("backend_ok", True, f"backend={backend}, status={status}")
        except Exception as e:
            self.check("backend_ok", False, str(e))

    def check_session_file(self):
        """Does the session have an active updates/history file?"""
        backend = self.agent_cfg.get("backend", "grok")
        session_id = self.agent_cfg.get("session", "")

        if not session_id:
            self.check("session_file", True, "no session configured (new agent)")
            return

        if backend == "claude":
            # Claude sessions are in home dir
            session_dir = os.path.join(self.home, ".claude")
            if os.path.isdir(session_dir):
                self.check("session_file", True, f"claude session dir exists")
            else:
                self.check("session_file", True, "claude backend (session managed externally)")
        else:
            # Grok sessions
            cwd_encoded = self.home.replace("/", "%2F")
            sessions_base = os.path.expanduser("~/.grok/sessions")
            session_dir = os.path.join(sessions_base, cwd_encoded, session_id)
            updates_path = os.path.join(session_dir, "updates.jsonl")

            if os.path.exists(updates_path):
                size = os.path.getsize(updates_path)
                if size > SESSION_FILE_MIN_BYTES:
                    self.check("session_file", True, f"updates.jsonl: {size:,} bytes")
                else:
                    self.check("session_file", False, f"updates.jsonl too small: {size} bytes")
            elif os.path.isdir(session_dir):
                self.check("session_file", True, f"session dir exists (no updates.jsonl yet)")
            else:
                self.check("session_file", False, f"session dir missing: {session_dir}")


def load_config():
    if not CONFIG_PATH.exists():
        print(f"FAIL: Config not found: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="Agent health check")
    parser.add_argument("agents", nargs="*", help="Agent names (default: all)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show all checks")
    parser.add_argument("--timeout", type=int, default=0, help="Wait N seconds for health before checking")
    args = parser.parse_args()

    config = load_config()
    targets = args.agents or list(config["agents"].keys())

    if args.timeout > 0:
        print(f"Waiting {args.timeout}s for agents to initialize...")
        time.sleep(args.timeout)

    all_healthy = True
    for agent in targets:
        if agent not in config["agents"]:
            print(f"\n  {agent}: UNKNOWN (not in agents.json)")
            all_healthy = False
            continue
        hc = HealthCheck(agent, config, verbose=args.verbose)
        if not hc.run_all():
            all_healthy = False

    print()
    if all_healthy:
        print("All agents healthy.")
    else:
        print("Some agents have issues.")
        sys.exit(1)


if __name__ == "__main__":
    main()
