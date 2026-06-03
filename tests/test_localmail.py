"""Tests for localmail — agent-to-agent messaging via filesystem."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# Add core to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))


@pytest.fixture
def tmp_agents(tmp_path):
    """Create a temporary agents home directory and patch localmail to use it."""
    import localmail
    original = localmail.AGENTS_HOME_DIR
    localmail.AGENTS_HOME_DIR = tmp_path
    yield tmp_path
    localmail.AGENTS_HOME_DIR = original


class TestSendMail:
    def test_single_recipient(self, tmp_agents):
        from localmail import send_mail
        msg_id = send_mail("Trip", "Q", "hello Q")
        assert msg_id  # non-empty UUID
        inbox = tmp_agents / "Q" / "asdaaas" / "adapters" / "localmail" / "inbox"
        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        msg = json.loads(files[0].read_text())
        assert msg["from"] == "Trip"
        assert msg["to"] == "Q"
        assert msg["text"] == "hello Q"
        assert msg["id"] == msg_id

    def test_multiple_recipients(self, tmp_agents):
        from localmail import send_mail
        msg_id = send_mail("Sr", ["Trip", "Q", "Jr"], "team update")
        for agent in ["Trip", "Q", "Jr"]:
            inbox = tmp_agents / agent / "asdaaas" / "adapters" / "localmail" / "inbox"
            files = list(inbox.glob("*.json"))
            assert len(files) == 1
            msg = json.loads(files[0].read_text())
            assert msg["from"] == "Sr"
            assert msg["to"] == ["Trip", "Q", "Jr"]
            assert msg["id"] == msg_id

    def test_empty_recipient_raises(self, tmp_agents):
        from localmail import send_mail
        with pytest.raises(ValueError):
            send_mail("Trip", [], "nope")

    def test_priority_and_meta(self, tmp_agents):
        from localmail import send_mail
        send_mail("Trip", "Q", "urgent", priority=1, meta={"tag": "bug"})
        inbox = tmp_agents / "Q" / "asdaaas" / "adapters" / "localmail" / "inbox"
        msg = json.loads(list(inbox.glob("*.json"))[0].read_text())
        assert msg["priority"] == 1
        assert msg["meta"]["tag"] == "bug"

    def test_timestamp_present(self, tmp_agents):
        from localmail import send_mail
        send_mail("Trip", "Q", "test")
        inbox = tmp_agents / "Q" / "asdaaas" / "adapters" / "localmail" / "inbox"
        msg = json.loads(list(inbox.glob("*.json"))[0].read_text())
        assert "ts" in msg
        assert isinstance(msg["ts"], float)


class TestReadMail:
    def test_read_returns_messages(self, tmp_agents):
        from localmail import send_mail, read_mail
        send_mail("Sr", "Trip", "msg1")
        send_mail("Q", "Trip", "msg2")
        msgs = read_mail("Trip")
        assert len(msgs) == 2
        texts = {m["text"] for m in msgs}
        assert texts == {"msg1", "msg2"}

    def test_read_deletes_by_default(self, tmp_agents):
        from localmail import send_mail, read_mail
        send_mail("Sr", "Trip", "ephemeral")
        read_mail("Trip")
        inbox = tmp_agents / "Trip" / "asdaaas" / "adapters" / "localmail" / "inbox"
        assert list(inbox.glob("*.json")) == []

    def test_peek_does_not_delete(self, tmp_agents):
        from localmail import send_mail, peek_mail
        send_mail("Sr", "Trip", "persistent")
        msgs = peek_mail("Trip")
        assert len(msgs) == 1
        inbox = tmp_agents / "Trip" / "asdaaas" / "adapters" / "localmail" / "inbox"
        assert len(list(inbox.glob("*.json"))) == 1

    def test_read_empty_inbox(self, tmp_agents):
        from localmail import read_mail
        assert read_mail("Trip") == []


class TestRingDoorbell:
    def test_doorbell_created(self, tmp_agents):
        from localmail import ring_doorbell
        msg = {"id": "test-123", "from": "Q", "text": "short msg", "priority": 3}
        ring_doorbell("Trip", msg)
        bell_dir = tmp_agents / "Trip" / "asdaaas" / "doorbells"
        bells = list(bell_dir.glob("bell_*.json"))
        assert len(bells) == 1
        bell = json.loads(bells[0].read_text())
        assert bell["adapter"] == "localmail"
        assert bell["from"] == "Q"
        assert bell["msg_id"] == "test-123"
        assert "short msg" in bell["text"]

    def test_long_message_creates_payload(self, tmp_agents):
        from localmail import ring_doorbell
        long_text = "x" * 600
        msg = {"id": "long-1", "from": "Sr", "text": long_text, "priority": 2}
        ring_doorbell("Trip", msg)
        payload_dir = tmp_agents / "Trip" / "asdaaas" / "adapters" / "localmail" / "payloads"
        payload = payload_dir / "long-1.json"
        assert payload.exists()
        full_msg = json.loads(payload.read_text())
        assert full_msg["text"] == long_text
        # Doorbell should have truncated preview
        bell_dir = tmp_agents / "Trip" / "asdaaas" / "doorbells"
        bell = json.loads(list(bell_dir.glob("bell_*.json"))[0].read_text())
        assert "..." in bell["text"]
        assert "Full message: cat" in bell["text"]

    def test_duplicate_doorbell_skipped(self, tmp_agents):
        from localmail import ring_doorbell
        msg = {"id": "dup-1", "from": "Q", "text": "hello", "priority": 3}
        ring_doorbell("Trip", msg)
        ring_doorbell("Trip", msg)  # same msg_id
        bell_dir = tmp_agents / "Trip" / "asdaaas" / "doorbells"
        bells = list(bell_dir.glob("bell_*.json"))
        assert len(bells) == 1


class TestReplyAll:
    def test_reply_all_excludes_self(self, tmp_agents):
        from localmail import reply_all
        original = {"from": "Eric", "to": ["Trip", "Q", "Sr"]}
        reply_all(original, from_agent="Trip", text="ack")
        # Should go to Eric, Q, Sr — not Trip
        for agent in ["Q", "Sr"]:
            inbox = tmp_agents / agent / "asdaaas" / "adapters" / "localmail" / "inbox"
            assert len(list(inbox.glob("*.json"))) == 1
        # Eric too (original sender)
        eric_inbox = tmp_agents / "Eric" / "asdaaas" / "adapters" / "localmail" / "inbox"
        assert len(list(eric_inbox.glob("*.json"))) == 1
        # Trip should NOT get it
        trip_inbox = tmp_agents / "Trip" / "asdaaas" / "adapters" / "localmail" / "inbox"
        assert not trip_inbox.exists() or len(list(trip_inbox.glob("*.json"))) == 0


class TestGetAsdaaasAgents:
    def test_detects_agents_with_doorbells_dir(self, tmp_agents):
        from localmail import get_asdaaas_agents
        (tmp_agents / "Trip" / "asdaaas" / "doorbells").mkdir(parents=True)
        (tmp_agents / "Q" / "asdaaas" / "doorbells").mkdir(parents=True)
        (tmp_agents / "Jr").mkdir()  # no asdaaas dir
        agents = get_asdaaas_agents()
        assert "Trip" in agents
        assert "Q" in agents
        assert "Jr" not in agents