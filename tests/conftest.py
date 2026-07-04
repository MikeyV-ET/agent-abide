"""
conftest.py — Canonical fixture for true e2e tests.
=====================================================
Provides asdaaas_env fixture: hermetic agent workspace in tmp_path,
with helpers to inject input and read output through the public file interface.

See docs/specs/t1_fixture_api.md for the full design.
"""

import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

# Add core to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from asdaaas_env import AsdaaasEnv
from turn_engine import TurnEngine, GatherResult, DeliverResult, PostTurnResult


AGENT_NAME = "TestAgent"


class AsdaaasTestEnv:
    """Test harness wrapping a hermetic asdaaas instance."""

    def __init__(self, tmp_path: Path, agent_name: str = AGENT_NAME):
        self.agent_name = agent_name
        self.agents_home = tmp_path / "agents"
        self.agent_home = self.agents_home / agent_name
        self.asdaaas_dir = self.agent_home / "asdaaas"

        # Build directory tree
        for subdir in [
            "asdaaas/doorbells",
            "asdaaas/commands",
            "asdaaas/adapters/tui/inbox",
            "asdaaas/adapters/tui/outbox",
            "asdaaas/adapters/irc/inbox",
            "asdaaas/adapters/irc/outbox",
            "asdaaas/adapters/localmail/inbox",
            "asdaaas/adapters/localmail/payloads",
            "asdaaas/adapters/remind/inbox",
            "asdaaas/profile",
        ]:
            (self.agent_home / subdir).mkdir(parents=True, exist_ok=True)

        # Write default awareness
        awareness = {
            "direct_attach": ["tui"],
            "control_watch": {},
            "notify_watch": [],
            "accept_from": ["*"],
            "default_doorbell": True,
            "doorbell_ttl": {"default": 3},
        }
        self._write_json(self.asdaaas_dir / "awareness.json", awareness)

        # Write default gaze
        gaze = {"speech": {"target": "tui", "params": {}}, "thoughts": None}
        self._write_json(self.asdaaas_dir / "gaze.json", gaze)

        # Write AGENTS.md
        (self.agent_home / "AGENTS.md").write_text(
            f"# {agent_name}\nRespond normally.\n"
        )

        # Empty conversation log
        (self.asdaaas_dir / "conversation.jsonl").write_text("")

        # Build the AsdaaasEnv composition root
        self.env = AsdaaasEnv(
            agents_home=self.agents_home,
            asdaaas_dir=self.asdaaas_dir,
        )

    # --- Input: inject into the agent's world ---

    def inject_message(self, adapter: str, text: str,
                       sender: str = "eric") -> Path:
        """Write a message to adapter inbox. Returns the file path."""
        inbox = self.asdaaas_dir / "adapters" / adapter / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        rand = secrets.token_hex(4)
        msg = {
            "text": text,
            "sender": sender,
            "adapter": adapter,
            "ts": time.time(),
        }
        path = inbox / f"msg_{ts}_{rand}.json"
        self._write_json(path, msg)
        return path

    def inject_doorbell(self, doorbell_id: str, adapter: str = "tui",
                        sender: str = "eric", text: str = "") -> Path:
        """Write a doorbell to the doorbells dir."""
        bell = {
            "id": doorbell_id,
            "adapter": adapter,
            "sender": sender,
            "text": text or f"Message from {sender}",
            "ts": time.time(),
            "delivered_count": 0,
        }
        path = self.asdaaas_dir / "doorbells" / f"{doorbell_id}.json"
        self._write_json(path, bell)
        return path

    def inject_command(self, command: dict) -> Path:
        """Write a command to the commands dir."""
        ts = int(time.time() * 1000)
        rand = secrets.token_hex(4)
        path = self.asdaaas_dir / "commands" / f"cmd_{ts}_{rand}.json"
        self._write_json(path, command)
        return path

    def inject_localmail(self, from_agent: str, text: str) -> Path:
        """Write a localmail message to the inbox using the real localmail API."""
        from localmail import send_mail
        msg_id = send_mail(
            from_agent=from_agent,
            to_agent=self.agent_name,
            text=text,
            env=self.env,
        )
        return self.asdaaas_dir / "adapters" / "localmail" / "inbox" / f"{msg_id}.json"

    def inject_interjection(self, text: str) -> None:
        """Queue an interjection using the real interjection API."""
        from interjection import queue_interjection
        queue_interjection(self.agent_name, text, env=self.env)

    def drain_interjections(self) -> list:
        """Drain and return all queued interjections."""
        from interjection import drain_interjection_queue
        return drain_interjection_queue(self.agent_name, env=self.env)

    # --- Output: read from the agent's world ---

    def outbox(self, adapter: str = "tui") -> list:
        """Return parsed outbox messages for adapter, sorted by timestamp."""
        outbox_dir = self.asdaaas_dir / "adapters" / adapter / "outbox"
        if not outbox_dir.exists():
            return []
        msgs = []
        for f in sorted(outbox_dir.glob("*.json")):
            try:
                msgs.append(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
        return msgs

    def health(self) -> dict:
        """Return parsed health.json."""
        path = self.asdaaas_dir / "health.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def doorbells(self) -> list:
        """Return all active doorbells, sorted by timestamp."""
        bells_dir = self.asdaaas_dir / "doorbells"
        if not bells_dir.exists():
            return []
        bells = []
        for f in sorted(bells_dir.glob("*.json")):
            try:
                bells.append(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
        return bells

    def commands(self) -> list:
        """Return all pending commands, sorted by timestamp."""
        cmds_dir = self.asdaaas_dir / "commands"
        if not cmds_dir.exists():
            return []
        cmds = []
        for f in sorted(cmds_dir.glob("*.json")):
            try:
                cmds.append(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
        return cmds

    def conversation(self) -> list:
        """Return parsed conversation.jsonl entries."""
        path = self.asdaaas_dir / "conversation.jsonl"
        if not path.exists():
            return []
        entries = []
        for line in path.read_text().strip().splitlines():
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries

    def gaze(self) -> dict:
        """Return current gaze.json."""
        path = self.asdaaas_dir / "gaze.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def awareness(self) -> dict:
        """Return current awareness.json."""
        path = self.asdaaas_dir / "awareness.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    # --- Engine ---

    def make_engine(self, backend=None, context_window: int = 200000) -> TurnEngine:
        """Create a TurnEngine wired to this hermetic environment."""
        return TurnEngine(
            env=self.env,
            agent_name=self.agent_name,
            backend=backend,
            context_window=context_window,
        )

    async def run_turn(self, engine: TurnEngine) -> tuple:
        """Run one full turn cycle: gather → deliver → post_turn.

        Returns (GatherResult, DeliverResult | None, PostTurnResult | None).
        If gather has no content, deliver and post_turn are skipped (None, None).
        """
        gathered = await engine.gather_pending()
        if not gathered.has_content:
            return gathered, None, None
        dr = await engine.deliver_turn(gathered)
        if dr is None:
            return gathered, None, None
        ptr = await engine.post_turn(dr)
        return gathered, dr, ptr

    # --- Helpers ---

    def clear_outbox(self, adapter: str = "tui"):
        """Remove all outbox files for adapter."""
        outbox_dir = self.asdaaas_dir / "adapters" / adapter / "outbox"
        if outbox_dir.exists():
            for f in outbox_dir.glob("*.json"):
                f.unlink()

    def clear_doorbells(self):
        """Remove all doorbell files."""
        for f in (self.asdaaas_dir / "doorbells").glob("*.json"):
            f.unlink()

    @staticmethod
    def _write_json(path: Path, data: dict):
        """Atomic JSON write via temp file."""
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f)
            os.rename(tmp, str(path))
        except Exception:
            os.unlink(tmp)
            raise


@pytest.fixture
def asdaaas_env(tmp_path):
    """Canonical fixture: hermetic agent workspace for true e2e tests."""
    return AsdaaasTestEnv(tmp_path)
