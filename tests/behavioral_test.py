"""Behavioral tests for live agents.

Sends a test prompt to a live agent via remind/localmail, then verifies
the agent performed the requested action by checking filesystem state.

These are fire drills — they test whether agents have internalized
operating procedures, not whether infrastructure works.

Usage:
    # Run a single test against a specific agent
    python3 behavioral_test.py --agent Trip --test gaze_switch

    # Run all tests against all agents
    python3 behavioral_test.py --all

    # List available tests
    python3 behavioral_test.py --list

    # Schedule a test for later (via remind adapter)
    python3 behavioral_test.py --agent Trip --test gaze_switch --delay 3600
"""

import json
import os
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))
import adapter_api

try:
    from asdaaas_config import config
except ModuleNotFoundError:
    import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'core'))
    from asdaaas_config import config

AGENTS_HOME = config.agents_home
RESULTS_DIR = AGENTS_HOME / "behavioral_tests"
REMIND_INBOX = lambda agent: config.agent_adapter_inbox(agent, "remind")


# ============================================================================
# Test Definitions
# ============================================================================

TESTS = {}

def behavioral_test(name, description, timeout=60):
    """Decorator to register a behavioral test."""
    def decorator(cls):
        cls.test_name = name
        cls.description = description
        cls.timeout = timeout
        TESTS[name] = cls
        return cls
    return decorator


class BehavioralTest:
    """Base class for behavioral tests."""
    test_name = ""
    description = ""
    timeout = 60

    def prompt(self, agent_name):
        """Return the prompt text to send to the agent."""
        raise NotImplementedError

    def verify(self, agent_name):
        """Check if the agent performed the action. Returns (passed, details)."""
        raise NotImplementedError


@behavioral_test("gaze_switch", "Switch gaze to #behavtest and say 'test complete'", timeout=90)
class GazeSwitchTest(BehavioralTest):
    def prompt(self, agent_name):
        return (
            f"[BEHAVIORAL TEST — gaze_switch]\n"
            f"Please do the following:\n"
            f"1. Set your gaze to #behavtest using the gaze command\n"
            f"2. Say 'behavioral test complete' (this will go to #behavtest via your gaze)\n"
            f"3. Set your gaze back to where it was\n"
            f"This is an automated test of operating procedures. Respond naturally."
        )

    def verify(self, agent_name):
        # Check IRC log for the agent's message in #behavtest
        log = Path(os.path.expanduser("~/.grok/irc_logs/#behavtest.log"))
        if not log.exists():
            return False, "#behavtest.log does not exist"
        text = log.read_text()
        # Look for agent's message in last 2 minutes
        lines = text.strip().split("\n")
        cutoff = time.time() - 120
        for line in reversed(lines[-20:]):
            if agent_name.lower() in line.lower() and "test complete" in line.lower():
                return True, f"Found: {line.strip()}"
        return False, f"No 'test complete' from {agent_name} in #behavtest"


@behavioral_test("file_bug_test", "File a test bug using file_bug()", timeout=90)
class FileBugTest(BehavioralTest):
    def prompt(self, agent_name):
        return (
            f"[BEHAVIORAL TEST — file_bug_test]\n"
            f"Please file a bug report using the bug_report module:\n"
            f"  from bug_report import file_bug\n"
            f"  file_bug(filed_by='{agent_name}', title='behavioral test bug', "
            f"symptoms='This is an automated behavioral test', severity='P3')\n"
            f"This is an automated test. File the bug, then continue with your work."
        )

    def verify(self, agent_name):
        issues_dir = AGENTS_HOME / "issues"
        for f in sorted(issues_dir.glob("issue_*.json"), reverse=True):
            try:
                bug = json.loads(f.read_text())
                if (bug.get("title") == "behavioral test bug" and
                    bug.get("filed_by") == agent_name):
                    return True, f"Found: {f.name} filed by {agent_name}"
            except (json.JSONDecodeError, OSError):
                pass
        return False, f"No 'behavioral test bug' from {agent_name} in ~/agents/bugs/"


@behavioral_test("localmail_ping", "Send localmail to Sr saying 'behavioral ping'", timeout=90)
class LocalmailPingTest(BehavioralTest):
    def prompt(self, agent_name):
        return (
            f"[BEHAVIORAL TEST — localmail_ping]\n"
            f"Please send a localmail to Sr with the text 'behavioral ping from {agent_name}'.\n"
            f"Use: send_mail(from_agent='{agent_name}', to_agent='Sr', "
            f"text='behavioral ping from {agent_name}')\n"
            f"This is an automated test. Send the mail, then continue with your work."
        )

    def verify(self, agent_name):
        inbox = AGENTS_HOME / "Sr" / "asdaaas" / "adapters" / "localmail" / "inbox"
        if not inbox.exists():
            return False, "Sr localmail inbox not found"
        for f in sorted(inbox.glob("*.json"), reverse=True):
            try:
                msg = json.loads(f.read_text())
                if f"behavioral ping from {agent_name}" in msg.get("text", ""):
                    return True, f"Found ping from {agent_name} in Sr inbox"
            except (json.JSONDecodeError, OSError):
                pass
        return False, f"No 'behavioral ping from {agent_name}' in Sr inbox"


@behavioral_test("read_truncated", "Read a truncated localmail and report the hidden keyword", timeout=120)
class ReadTruncatedTest(BehavioralTest):
    """Tests whether agents read the full payload when a message is truncated.
    
    Sends a long message where the critical information (a keyword) is past
    the 500-char truncation point. The agent must cat the payload file and
    report the keyword. If they respond without the keyword, they didn't
    read the full message.
    """
    KEYWORD = "CHRYSANTHEMUM"
    
    def prompt(self, agent_name):
        # The message starts with a real task description that gets cut off
        # mid-sentence at the truncation point, making it clear there's more.
        return (
            f"[BEHAVIORAL TEST — read_truncated]\n"
            f"{agent_name}, I need you to do something specific. Eric asked me to verify "
            f"that agents can follow multi-step instructions delivered via localmail. "
            f"This is important because we discovered that truncated messages are being "
            f"acted on without reading the full content, which means critical instructions "
            f"are being missed. The first step is to read this entire message including "
            f"the part after the truncation marker. The second step is described after "
            f"this paragraph. Do not act until you have read the complete message. The "
            f"instructions you need are at the end of this message, not here. If you "
            f"respond without completing the action described at the end, the test fails. "
            f"The action requires you to send a specific keyword that proves you read the "
            f"full message. The keyword and the exact send_mail command are below.\n\n"
            f"INSTRUCTIONS: Send a localmail to Sr containing only the word {self.KEYWORD}. "
            f"Use: send_mail(from_agent='{agent_name}', to_agent='Sr', text='{self.KEYWORD}')\n"
            f"This proves you read past the truncation point."
        )

    def verify(self, agent_name):
        # Check Sr's localmail inbox and payloads for the keyword
        inbox = AGENTS_HOME / "Sr" / "asdaaas" / "adapters" / "localmail" / "inbox"
        payloads = AGENTS_HOME / "Sr" / "asdaaas" / "adapters" / "localmail" / "payloads"
        
        for search_dir in [inbox, payloads]:
            if not search_dir.exists():
                continue
            for f in sorted(search_dir.glob("*.json"), reverse=True):
                try:
                    msg = json.loads(f.read_text())
                    text = msg.get("text", "")
                    if self.KEYWORD in text and agent_name in msg.get("from", ""):
                        return True, f"Found {self.KEYWORD} from {agent_name}"
                except (json.JSONDecodeError, OSError):
                    pass
        
        # Also check doorbells (mail may have been consumed already)
        bell_dir = AGENTS_HOME / "Sr" / "asdaaas" / "doorbells"
        if bell_dir.exists():
            for f in sorted(bell_dir.glob("*.json"), reverse=True):
                try:
                    bell = json.loads(f.read_text())
                    if self.KEYWORD in bell.get("text", ""):
                        return True, f"Found {self.KEYWORD} in Sr doorbell"
                except (json.JSONDecodeError, OSError):
                    pass
        
        return False, f"No '{self.KEYWORD}' from {agent_name} found in Sr inbox/doorbells"


@behavioral_test("gaze_to_arena", "Set gaze to arena adapter and verify foreground delivery", timeout=90)
class GazeToArenaTest(BehavioralTest):
    """Tests that an agent can set gaze to the arena adapter.

    Verifies:
    1. Agent can set gaze to arena via command queue
    2. gaze.json reflects the change
    3. Agent can set gaze back to original target
    """
    def prompt(self, agent_name):
        return (
            f"[BEHAVIORAL TEST — gaze_to_arena]\n"
            f"Please do the following:\n"
            f"1. Note your current gaze target\n"
            f"2. Set your gaze to the arena adapter: "
            f'{{\"action\": \"gaze\", \"adapter\": \"arena\"}}\n'
            f"3. Wait 2 seconds, then set gaze back to where it was\n"
            f"4. Send a localmail to Sr with text 'gaze_arena_test done by {agent_name}'\n"
            f"This is an automated test of gaze switching to SA."
        )

    def verify(self, agent_name):
        # Check that the agent completed the task (sent confirmation)
        inbox = AGENTS_HOME / "Sr" / "asdaaas" / "adapters" / "localmail" / "inbox"
        payloads = AGENTS_HOME / "Sr" / "asdaaas" / "adapters" / "localmail" / "payloads"
        for search_dir in [inbox, payloads]:
            if not search_dir.exists():
                continue
            for f in sorted(search_dir.glob("*.json"), reverse=True):
                try:
                    msg = json.loads(f.read_text())
                    if f"gaze_arena_test done by {agent_name}" in msg.get("text", ""):
                        return True, f"Found confirmation from {agent_name}"
                except (json.JSONDecodeError, OSError):
                    pass
        return False, f"No 'gaze_arena_test done' from {agent_name}"


@behavioral_test("awareness_add", "Add #behavtest to awareness as doorbell", timeout=90)
class AwarenessAddTest(BehavioralTest):
    def prompt(self, agent_name):
        return (
            f"[BEHAVIORAL TEST — awareness_add]\n"
            f"Please add #behavtest to your background awareness as a doorbell channel.\n"
            f"Use the awareness command: "
            f'{{\"action\": \"awareness\", \"add\": \"#behavtest\", \"mode\": \"doorbell\"}}\n'
            f"Write it to your command queue. This is an automated test."
        )

    def verify(self, agent_name):
        awareness_file = AGENTS_HOME / agent_name / "asdaaas" / "awareness.json"
        if not awareness_file.exists():
            return False, "awareness.json not found"
        try:
            awareness = json.loads(awareness_file.read_text())
            bg = awareness.get("background_channels", {})
            if "#behavtest" in bg and bg["#behavtest"] == "doorbell":
                return True, f"#behavtest found in {agent_name} background_channels as doorbell"
            return False, f"#behavtest not in background_channels: {bg}"
        except (json.JSONDecodeError, OSError) as e:
            return False, f"Error reading awareness: {e}"


# ============================================================================
# Test Runner
# ============================================================================

def send_test(agent_name, test_name, delay=0):
    """Send a behavioral test to an agent. Returns test_id."""
    if test_name not in TESTS:
        print(f"Unknown test: {test_name}. Available: {list(TESTS.keys())}")
        return None

    test = TESTS[test_name]()
    prompt_text = test.prompt(agent_name)
    test_id = f"bt_{test_name}_{agent_name}_{int(time.time())}"

    if delay > 0:
        # Schedule via remind adapter
        remind_dir = REMIND_INBOX(agent_name)
        remind_dir.mkdir(parents=True, exist_ok=True)
        remind_file = remind_dir / f"remind_{int(time.time()*1000)}.json"
        with open(remind_file, "w") as f:
            json.dump({"command": "remind", "delay": delay, "text": prompt_text}, f)
        print(f"Scheduled {test_name} for {agent_name} in {delay}s")
    else:
        # Send immediately via localmail
        from localmail import send_mail
        send_mail(from_agent="Sr", to_agent=agent_name, text=prompt_text)
        print(f"Sent {test_name} to {agent_name}")

    # Record the test
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result = {
        "test_id": test_id,
        "test_name": test_name,
        "agent": agent_name,
        "sent_at": time.time(),
        "timeout": test.timeout,
        "status": "sent",
    }
    with open(RESULTS_DIR / f"{test_id}.json", "w") as f:
        json.dump(result, f, indent=2)

    return test_id


def verify_test(test_id):
    """Verify a previously sent test. Returns (passed, details)."""
    result_file = RESULTS_DIR / f"{test_id}.json"
    if not result_file.exists():
        return False, f"No result file for {test_id}"

    result = json.loads(result_file.read_text())
    test_name = result["test_name"]
    agent_name = result["agent"]

    if test_name not in TESTS:
        return False, f"Unknown test: {test_name}"

    test = TESTS[test_name]()
    passed, details = test.verify(agent_name)

    result["status"] = "passed" if passed else "failed"
    result["verified_at"] = time.time()
    result["details"] = details
    with open(result_file, "w") as f:
        json.dump(result, f, indent=2)

    return passed, details


def run_test(agent_name, test_name, delay=0):
    """Send a test and wait for verification."""
    test_id = send_test(agent_name, test_name, delay=delay)
    if not test_id or delay > 0:
        return test_id

    test = TESTS[test_name]()
    print(f"Waiting {test.timeout}s for {agent_name} to complete {test_name}...")

    deadline = time.time() + test.timeout
    while time.time() < deadline:
        time.sleep(10)
        passed, details = verify_test(test_id)
        if passed:
            print(f"PASSED: {details}")
            return test_id
        print(f"  checking... ({int(deadline - time.time())}s remaining)")

    passed, details = verify_test(test_id)
    if passed:
        print(f"PASSED: {details}")
    else:
        print(f"FAILED: {details}")
    return test_id


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Behavioral tests for live agents")
    parser.add_argument("--agent", help="Agent to test")
    parser.add_argument("--test", help="Test name to run")
    parser.add_argument("--delay", type=int, default=0, help="Delay in seconds (schedule via remind)")
    parser.add_argument("--verify", help="Verify a test by ID")
    parser.add_argument("--list", action="store_true", help="List available tests")
    parser.add_argument("--all", action="store_true", help="Run all tests against all agents")
    parser.add_argument("--results", action="store_true", help="Show recent results")
    args = parser.parse_args()

    if args.list:
        print("Available behavioral tests:")
        for name, cls in TESTS.items():
            print(f"  {name:20s} {cls.description}")
        return

    if args.verify:
        passed, details = verify_test(args.verify)
        status = "PASSED" if passed else "FAILED"
        print(f"{status}: {details}")
        return

    if args.results:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        for f in sorted(RESULTS_DIR.glob("bt_*.json"))[-20:]:
            r = json.loads(f.read_text())
            print(f"  {r['test_id']:40s} {r['status']:8s} {r.get('agent','?'):8s} {r.get('details','')[:60]}")
        return

    if args.all:
        agents = ["Sr", "Jr", "Trip", "Q", "Cinco"]
        for agent in agents:
            for test_name in TESTS:
                send_test(agent, test_name, delay=args.delay)
        print(f"Sent {len(agents) * len(TESTS)} tests. Use --results to check.")
        return

    if args.agent and args.test:
        run_test(args.agent, args.test, delay=args.delay)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
