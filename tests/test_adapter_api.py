"""Tests for adapter_api.py — the filesystem message passing library.

Ported from mikeyv-infra/tests/test_adapter_api.py on 2026-06-24.
Tests: write_message, poll_responses, per-adapter inbox/outbox,
adapter registration, payloads, attention, agent utilities.
"""

import json
import os
import sys
import time
import pytest
from pathlib import Path

# Add core to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

import adapter_api


# ============================================================================
# Fixture: hub_dir — temp directory structure with monkeypatched paths
# ============================================================================

@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    """Create a temporary asdaaas directory structure and monkeypatch adapter_api to use it.

    Agent-centric structure:
      tmp_path/agents/<AgentName>/asdaaas/{gaze,awareness,attention/,doorbells/,profile/}
      tmp_path/agents/<AgentName>/asdaaas/adapters/<adapter>/{inbox,outbox}/

    Engine config:
      tmp_path/asdaaas/adapters/<adapter>.json  (adapter registrations)

    Returns the engine dir (tmp_path/asdaaas).
    """
    hub = tmp_path / "asdaaas"
    agents_home = tmp_path / "agents"

    agents = ["Sr", "Jr", "Trip", "Q", "Cinco"]
    adapters = ["irc", "localmail", "session", "context", "heartbeat", "remind"]

    # Per-agent dirs (agent-centric: ~/agents/<name>/asdaaas/...)
    for agent in agents:
        agent_asdaaas = agents_home / agent / "asdaaas"
        (agent_asdaaas / "doorbells").mkdir(parents=True, exist_ok=True)
        (agent_asdaaas / "attention").mkdir(parents=True, exist_ok=True)
        (agent_asdaaas / "profile").mkdir(parents=True, exist_ok=True)
        for adapter in adapters:
            (agent_asdaaas / "adapters" / adapter / "inbox").mkdir(parents=True, exist_ok=True)
            (agent_asdaaas / "adapters" / adapter / "outbox").mkdir(parents=True, exist_ok=True)

    # Engine config dirs
    (hub / "adapters").mkdir(parents=True, exist_ok=True)

    # Legacy dirs (still used by some code paths)
    (hub / "inbox").mkdir(parents=True, exist_ok=True)
    (hub / "payloads").mkdir(parents=True, exist_ok=True)
    for agent in agents:
        (hub / "outbox" / agent).mkdir(parents=True, exist_ok=True)
    (hub / "outbox" / "irc").mkdir(parents=True, exist_ok=True)
    (hub / "outbox" / "localmail").mkdir(parents=True, exist_ok=True)

    # Legacy agent dirs
    for agent in agents:
        (hub / "agents" / agent / "doorbells").mkdir(parents=True, exist_ok=True)

    # Monkeypatch adapter_api module
    monkeypatch.setattr(adapter_api, "HUB_DIR", hub)
    monkeypatch.setattr(adapter_api, "AGENTS_DIR", hub / "agents")
    monkeypatch.setattr(adapter_api, "AGENTS_HOME_DIR", agents_home)
    monkeypatch.setattr(adapter_api, "INBOX_DIR", hub / "inbox")
    monkeypatch.setattr(adapter_api, "OUTBOX_DIR", hub / "outbox")
    monkeypatch.setattr(adapter_api, "PER_ADAPTER_DIR", hub / "adapters")
    monkeypatch.setattr(adapter_api, "ADAPTERS_DIR", hub / "adapters")
    monkeypatch.setattr(adapter_api, "PAYLOADS_DIR", hub / "payloads")
    monkeypatch.setattr(adapter_api, "STATUS_QUERY_DIR", hub / "status")
    monkeypatch.setattr(adapter_api, "SESSION_INBOX", hub / "adapters" / "session" / "inbox")

    # S1 transition: patch AsdaaasEnv.from_config so env fallback uses tmp_path
    from asdaaas_env import AsdaaasEnv
    test_env = AsdaaasEnv(agents_home=agents_home)
    monkeypatch.setattr(AsdaaasEnv, "from_config", classmethod(lambda cls: test_env))

    return hub


# ============================================================================
# write_message / poll_responses (legacy universal inbox/outbox)
# ============================================================================

class TestWriteMessage:
    def test_creates_json_file(self, hub_dir):
        msg_id = adapter_api.write_message(to="Sr", text="hello", adapter="irc")
        files = list((hub_dir / "inbox").glob("*.json"))
        assert len(files) == 1
        with open(files[0]) as f:
            data = json.load(f)
        assert data["to"] == "Sr"
        assert data["text"] == "hello"
        assert data["adapter"] == "irc"
        assert data["id"] == msg_id

    def test_returns_uuid(self, hub_dir):
        msg_id = adapter_api.write_message(to="Sr", text="hello", adapter="irc")
        assert len(msg_id) == 36  # UUID format
        assert "-" in msg_id

    def test_custom_msg_id(self, hub_dir):
        msg_id = adapter_api.write_message(to="Sr", text="hello", adapter="irc", msg_id="custom-123")
        assert msg_id == "custom-123"

    def test_sender_defaults_to_adapter(self, hub_dir):
        adapter_api.write_message(to="Sr", text="hello", adapter="irc")
        files = list((hub_dir / "inbox").glob("*.json"))
        with open(files[0]) as f:
            data = json.load(f)
        assert data["from"] == "irc"

    def test_custom_sender(self, hub_dir):
        adapter_api.write_message(to="Sr", text="hello", adapter="irc", sender="eric")
        files = list((hub_dir / "inbox").glob("*.json"))
        with open(files[0]) as f:
            data = json.load(f)
        assert data["from"] == "eric"

    def test_meta_field(self, hub_dir):
        adapter_api.write_message(to="Sr", text="hello", adapter="irc", meta={"channel": "#standup"})
        files = list((hub_dir / "inbox").glob("*.json"))
        with open(files[0]) as f:
            data = json.load(f)
        assert data["meta"]["channel"] == "#standup"

    def test_expect_response_in_meta(self, hub_dir):
        adapter_api.write_message(to="Sr", text="hello", adapter="irc", expect_response=True, timeout=30)
        files = list((hub_dir / "inbox").glob("*.json"))
        with open(files[0]) as f:
            data = json.load(f)
        assert data["meta"]["expect_response"] is True
        assert data["meta"]["timeout"] == 30

    def test_has_timestamp(self, hub_dir):
        adapter_api.write_message(to="Sr", text="hello", adapter="irc")
        files = list((hub_dir / "inbox").glob("*.json"))
        with open(files[0]) as f:
            data = json.load(f)
        assert "ts" in data
        assert isinstance(data["ts"], float)  # epoch float

    def test_multiple_messages_unique_files(self, hub_dir):
        for i in range(5):
            adapter_api.write_message(to="Sr", text=f"msg {i}", adapter="irc")
        files = list((hub_dir / "inbox").glob("*.json"))
        assert len(files) == 5

    def test_atomic_write_no_tmp_files(self, hub_dir):
        adapter_api.write_message(to="Sr", text="hello", adapter="irc")
        tmp_files = list((hub_dir / "inbox").glob("*.tmp"))
        assert len(tmp_files) == 0


class TestPollResponses:
    def test_reads_and_deletes(self, hub_dir):
        outbox = hub_dir / "outbox" / "irc"
        msg = {"from": "Sr", "text": "response", "id": "r1"}
        with open(outbox / "resp_001.json", "w") as f:
            json.dump(msg, f)

        responses = adapter_api.poll_responses("irc")
        assert len(responses) == 1
        assert responses[0]["text"] == "response"
        assert len(list(outbox.glob("*.json"))) == 0  # deleted

    def test_no_delete_option(self, hub_dir):
        outbox = hub_dir / "outbox" / "irc"
        msg = {"from": "Sr", "text": "response", "id": "r1"}
        with open(outbox / "resp_001.json", "w") as f:
            json.dump(msg, f)

        responses = adapter_api.poll_responses("irc", delete=False)
        assert len(responses) == 1
        assert len(list(outbox.glob("*.json"))) == 1  # still there

    def test_chronological_order(self, hub_dir):
        outbox = hub_dir / "outbox" / "irc"
        for i in range(3):
            with open(outbox / f"resp_{i:03d}.json", "w") as f:
                json.dump({"from": "Sr", "text": f"msg {i}", "id": f"r{i}"}, f)

        responses = adapter_api.poll_responses("irc")
        assert [r["text"] for r in responses] == ["msg 0", "msg 1", "msg 2"]

    def test_empty_outbox(self, hub_dir):
        responses = adapter_api.poll_responses("irc")
        assert responses == []

    def test_skips_corrupt_json(self, hub_dir):
        outbox = hub_dir / "outbox" / "irc"
        with open(outbox / "resp_001.json", "w") as f:
            f.write("not valid json{{{")
        with open(outbox / "resp_002.json", "w") as f:
            json.dump({"from": "Sr", "text": "good", "id": "r2"}, f)

        responses = adapter_api.poll_responses("irc")
        assert len(responses) == 1
        assert responses[0]["text"] == "good"

    def test_ignores_tmp_files(self, hub_dir):
        outbox = hub_dir / "outbox" / "irc"
        with open(outbox / "resp_001.tmp", "w") as f:
            json.dump({"from": "Sr", "text": "partial"}, f)

        responses = adapter_api.poll_responses("irc")
        assert len(responses) == 0


# ============================================================================
# Per-adapter inbox/outbox (Phase 3)
# ============================================================================

class TestWriteToAdapterInbox:
    def test_creates_file_in_correct_path(self, hub_dir):
        msg_id = adapter_api.write_to_adapter_inbox("irc", "Trip", "hello from IRC", sender="eric")
        inbox = hub_dir.parent / "agents" / "Trip" / "asdaaas" / "adapters" / "irc" / "inbox"
        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        with open(files[0]) as f:
            data = json.load(f)
        assert data["to"] == "Trip"
        assert data["from"] == "eric"
        assert data["adapter"] == "irc"
        assert data["id"] == msg_id

    def test_sender_defaults_to_adapter(self, hub_dir):
        adapter_api.write_to_adapter_inbox("irc", "Sr", "hello")
        inbox = hub_dir.parent / "agents" / "Sr" / "asdaaas" / "adapters" / "irc" / "inbox"
        files = list(inbox.glob("*.json"))
        with open(files[0]) as f:
            data = json.load(f)
        assert data["from"] == "irc"

    def test_creates_dir_if_missing(self, hub_dir):
        # Write to a new agent that doesn't have a pre-created dir
        adapter_api.write_to_adapter_inbox("irc", "NewAgent", "hello")
        inbox = hub_dir.parent / "agents" / "NewAgent" / "asdaaas" / "adapters" / "irc" / "inbox"
        assert inbox.exists()
        assert len(list(inbox.glob("*.json"))) == 1

    def test_meta_field(self, hub_dir):
        adapter_api.write_to_adapter_inbox("irc", "Sr", "hello", meta={"channel": "#standup"})
        inbox = hub_dir.parent / "agents" / "Sr" / "asdaaas" / "adapters" / "irc" / "inbox"
        files = list(inbox.glob("*.json"))
        with open(files[0]) as f:
            data = json.load(f)
        assert data["meta"]["channel"] == "#standup"


class TestPollAdapterInbox:
    def test_reads_and_deletes(self, hub_dir):
        inbox = hub_dir.parent / "agents" / "Sr" / "asdaaas" / "adapters" / "irc" / "inbox"
        msg = {"from": "eric", "to": "Sr", "text": "hello", "adapter": "irc"}
        with open(inbox / "msg_001.json", "w") as f:
            json.dump(msg, f)

        messages = adapter_api.poll_adapter_inbox("irc", "Sr")
        assert len(messages) == 1
        assert messages[0]["text"] == "hello"
        assert len(list(inbox.glob("*.json"))) == 0

    def test_no_delete_option(self, hub_dir):
        inbox = hub_dir.parent / "agents" / "Sr" / "asdaaas" / "adapters" / "irc" / "inbox"
        msg = {"from": "eric", "to": "Sr", "text": "hello"}
        with open(inbox / "msg_001.json", "w") as f:
            json.dump(msg, f)

        messages = adapter_api.poll_adapter_inbox("irc", "Sr", delete=False)
        assert len(messages) == 1
        assert len(list(inbox.glob("*.json"))) == 1

    def test_empty_inbox(self, hub_dir):
        messages = adapter_api.poll_adapter_inbox("irc", "Sr")
        assert messages == []

    def test_nonexistent_agent(self, hub_dir):
        messages = adapter_api.poll_adapter_inbox("irc", "NonexistentAgent")
        assert messages == []

    def test_chronological_order(self, hub_dir):
        inbox = hub_dir.parent / "agents" / "Sr" / "asdaaas" / "adapters" / "irc" / "inbox"
        for i in range(3):
            with open(inbox / f"msg_{i:03d}.json", "w") as f:
                json.dump({"text": f"msg {i}"}, f)

        messages = adapter_api.poll_adapter_inbox("irc", "Sr")
        assert [m["text"] for m in messages] == ["msg 0", "msg 1", "msg 2"]

    def test_timestamp_prefix_ordering(self, hub_dir):
        """Messages written via write_to_adapter_inbox get timestamp-prefixed
        filenames that sort in arrival order."""
        for i in range(5):
            adapter_api.write_to_adapter_inbox("irc", "Sr", f"msg {i}", sender="eric")
            time.sleep(0.002)  # ensure distinct timestamps

        messages = adapter_api.poll_adapter_inbox("irc", "Sr")
        assert [m["text"] for m in messages] == [f"msg {i}" for i in range(5)]

    def test_timestamp_prefix_in_filename(self, hub_dir):
        """Verify filenames contain timestamp prefix for sortability."""
        adapter_api.write_to_adapter_inbox("irc", "Sr", "test", sender="eric")
        inbox = hub_dir.parent / "agents" / "Sr" / "asdaaas" / "adapters" / "irc" / "inbox"
        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        name = files[0].name
        # Should be msg_<timestamp>_<random>.json
        assert name.startswith("msg_")
        parts = name.replace(".json", "").split("_")
        assert len(parts) >= 3  # msg, timestamp, random
        assert parts[1].isdigit()  # timestamp is numeric


class TestWriteToAdapterOutbox:
    def test_creates_file_in_correct_path(self, hub_dir):
        msg_id = adapter_api.write_to_adapter_outbox("irc", "Trip", "response text", content_type="speech")
        outbox = hub_dir.parent / "agents" / "Trip" / "asdaaas" / "adapters" / "irc" / "outbox"
        files = list(outbox.glob("*.json"))
        assert len(files) == 1
        with open(files[0]) as f:
            data = json.load(f)
        assert data["from"] == "Trip"
        assert data["content_type"] == "speech"
        assert data["text"] == "response text"

    def test_thoughts_content_type(self, hub_dir):
        adapter_api.write_to_adapter_outbox("irc", "Trip", "thinking...", content_type="thoughts")
        outbox = hub_dir.parent / "agents" / "Trip" / "asdaaas" / "adapters" / "irc" / "outbox"
        files = list(outbox.glob("*.json"))
        with open(files[0]) as f:
            data = json.load(f)
        assert data["content_type"] == "thoughts"


class TestPollAdapterOutbox:
    def test_reads_and_deletes(self, hub_dir):
        outbox = hub_dir.parent / "agents" / "Trip" / "asdaaas" / "adapters" / "irc" / "outbox"
        msg = {"from": "Trip", "content_type": "speech", "text": "hello"}
        with open(outbox / "resp_001.json", "w") as f:
            json.dump(msg, f)

        responses = adapter_api.poll_adapter_outbox("irc", "Trip")
        assert len(responses) == 1
        assert responses[0]["text"] == "hello"
        assert len(list(outbox.glob("*.json"))) == 0

    def test_empty_outbox(self, hub_dir):
        responses = adapter_api.poll_adapter_outbox("irc", "Trip")
        assert responses == []


# ============================================================================
# Adapter Registration
# ============================================================================

class TestAdapterRegistration:
    def test_register_creates_file(self, hub_dir):
        path = adapter_api.register_adapter("test_adapter", capabilities=["send"], config={"foo": "bar"})
        assert path.exists()
        with open(path) as f:
            data = json.load(f)
        assert data["name"] == "test_adapter"
        assert data["capabilities"] == ["send"]
        assert data["config"]["foo"] == "bar"
        assert data["pid"] == os.getpid()

    def test_update_heartbeat(self, hub_dir):
        adapter_api.register_adapter("test_adapter")
        time.sleep(0.1)
        adapter_api.update_heartbeat("test_adapter")
        reg_path = hub_dir / "adapters" / "test_adapter.json"
        with open(reg_path) as f:
            data = json.load(f)
        assert data["pid"] == os.getpid()

    def test_deregister_removes_file(self, hub_dir):
        adapter_api.register_adapter("test_adapter")
        reg_path = hub_dir / "adapters" / "test_adapter.json"
        assert reg_path.exists()
        adapter_api.deregister_adapter("test_adapter")
        assert not reg_path.exists()

    def test_list_adapters(self, hub_dir):
        adapter_api.register_adapter("adapter_a")
        adapter_api.register_adapter("adapter_b")
        adapters = adapter_api.list_adapters(max_heartbeat_age=0)
        names = [a["name"] for a in adapters]
        assert "adapter_a" in names
        assert "adapter_b" in names


# ============================================================================
# Payload (reference passing)
# ============================================================================

class TestPayloads:
    def test_write_and_read(self, hub_dir):
        path = adapter_api.write_payload("msg-001", "eric", "Sr", "full message text")
        assert path.exists()
        data = adapter_api.read_payload("msg-001")
        assert data["text"] == "full message text"
        assert data["from"] == "eric"
        assert data["to"] == "Sr"

    def test_read_by_path(self, hub_dir):
        path = adapter_api.write_payload("msg-002", "eric", "Sr", "hello")
        data = adapter_api.read_payload_by_path(str(path))
        assert data["text"] == "hello"

    def test_read_nonexistent(self, hub_dir):
        data = adapter_api.read_payload("nonexistent")
        assert data is None

    def test_format_reference_short(self, hub_dir):
        ref = adapter_api.format_reference("msg-003", "eric", "hub", "short message")
        assert ref == "short message"

    def test_format_reference_truncates(self, hub_dir):
        long_text = "x" * 200
        ref = adapter_api.format_reference("msg-004", "eric", "hub", long_text)
        assert len(ref) <= 153  # 150 + "..."
        assert ref.endswith("...")

    def test_cleanup_old_payloads(self, hub_dir):
        adapter_api.write_payload("old-001", "eric", "Sr", "old message")
        # Touch the file to make it old
        path = hub_dir / "payloads" / "old-001.json"
        old_time = time.time() - 7200  # 2 hours ago
        os.utime(path, (old_time, old_time))

        deleted = adapter_api.cleanup_payloads(max_age_seconds=3600)
        assert deleted == 1
        assert not path.exists()

    def test_cleanup_keeps_recent(self, hub_dir):
        adapter_api.write_payload("new-001", "eric", "Sr", "new message")
        deleted = adapter_api.cleanup_payloads(max_age_seconds=3600)
        assert deleted == 0


# ============================================================================
# Attention declarations
# ============================================================================

class TestWriteAttention:
    def test_creates_attention_file(self, hub_dir):
        msg_id = adapter_api.write_attention(
            agent_name="Jr",
            expecting_from="Trip",
            msg_id="att-001",
            timeout_s=30,
            message_text="next slide please",
        )
        assert msg_id == "att-001"
        attn_file = hub_dir.parent / "agents" / "Jr" / "asdaaas" / "attention" / "att-001.json"
        assert attn_file.exists()
        with open(attn_file) as f:
            data = json.load(f)
        assert data["msg_id"] == "att-001"
        assert data["expecting_from"] == "Trip"
        assert data["timeout_s"] == 30
        assert data["status"] == "pending"
        assert data["expires_at"] > data["created_at"]
        assert data["expires_at"] - data["created_at"] == pytest.approx(30, abs=1)

    def test_truncates_long_message_text(self, hub_dir):
        long_text = "x" * 500
        adapter_api.write_attention(
            agent_name="Jr",
            expecting_from="Trip",
            msg_id="att-002",
            message_text=long_text,
        )
        attn_file = hub_dir.parent / "agents" / "Jr" / "asdaaas" / "attention" / "att-002.json"
        with open(attn_file) as f:
            data = json.load(f)
        assert len(data["message_text"]) == 200

    def test_creates_directory_if_missing(self, hub_dir):
        # Remove the pre-created directory
        attn_dir = hub_dir.parent / "agents" / "NewAgent" / "asdaaas" / "attention"
        adapter_api.write_attention(
            agent_name="NewAgent",
            expecting_from="Sr",
            msg_id="att-003",
        )
        assert (attn_dir / "att-003.json").exists()

    def test_atomic_write(self, hub_dir):
        """No .tmp files should remain after write."""
        adapter_api.write_attention(
            agent_name="Jr",
            expecting_from="Trip",
            msg_id="att-004",
        )
        attn_dir = hub_dir.parent / "agents" / "Jr" / "asdaaas" / "attention"
        tmp_files = list(attn_dir.glob("*.tmp"))
        assert len(tmp_files) == 0


class TestSendWithAttention:
    def test_creates_message_and_attention(self, hub_dir):
        msg_id = adapter_api.send_with_attention(
            to="Trip",
            text="next slide",
            adapter="jr",
            timeout=15,
        )
        # Should create a message in the hub inbox
        inbox_files = list((hub_dir / "inbox").glob("*.json"))
        assert len(inbox_files) == 1
        with open(inbox_files[0]) as f:
            msg = json.load(f)
        assert msg["to"] == "Trip"
        assert msg["text"] == "next slide"

        # Should create an attention file for the sender
        attn_dir = hub_dir.parent / "agents" / "Jr" / "asdaaas" / "attention"  # "jr" -> "Jr"
        attn_files = list(attn_dir.glob("*.json"))
        assert len(attn_files) == 1
        with open(attn_files[0]) as f:
            attn = json.load(f)
        assert attn["expecting_from"] == "Trip"
        assert attn["timeout_s"] == 15
        assert attn["msg_id"] == msg_id

    def test_sender_override(self, hub_dir):
        adapter_api.send_with_attention(
            to="Trip",
            text="hello",
            adapter="irc",
            sender="MikeyV-Jr",
            timeout=30,
        )
        # Attention should be under "MikeyV-Jr" (already capitalized)
        attn_dir = hub_dir.parent / "agents" / "MikeyV-Jr" / "asdaaas" / "attention"
        attn_files = list(attn_dir.glob("*.json"))
        assert len(attn_files) == 1


# ============================================================================
# Agent utilities (self-compact, status, gaze, awareness)
# ============================================================================

class TestRequestCompact:
    def test_writes_compact_command(self, hub_dir):
        req_id = adapter_api.request_compact("Trip")
        inbox = hub_dir.parent / "agents" / "Trip" / "asdaaas" / "adapters" / "session" / "inbox"
        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        with open(files[0]) as f:
            cmd = json.load(f)
        assert cmd["command"] == "compact"
        assert cmd["request_id"] == req_id
        assert cmd["source"] == "self"

    def test_creates_inbox_dir(self, hub_dir):
        # Verify it works even if inbox doesn't exist yet
        new_inbox = hub_dir.parent / "agents" / "NewAgent" / "asdaaas" / "adapters" / "session" / "inbox"
        assert not new_inbox.exists()
        adapter_api.request_compact("NewAgent")
        assert new_inbox.exists()
        files = list(new_inbox.glob("*.json"))
        assert len(files) == 1


class TestRequestStatus:
    def test_writes_status_command(self, hub_dir):
        req_id = adapter_api.request_status("Trip")
        inbox = hub_dir.parent / "agents" / "Trip" / "asdaaas" / "adapters" / "session" / "inbox"
        files = list(inbox.glob("*.json"))
        assert len(files) == 1
        with open(files[0]) as f:
            cmd = json.load(f)
        assert cmd["command"] == "status"
        assert cmd["request_id"] == req_id


class TestSetGaze:
    def test_writes_gaze_file(self, hub_dir):
        adapter_api.set_gaze("Trip", room="#standup")
        gaze_file = hub_dir.parent / "agents" / "Trip" / "asdaaas" / "gaze.json"
        assert gaze_file.exists()
        with open(gaze_file) as f:
            gaze = json.load(f)
        assert gaze["speech"]["target"] == "irc"
        assert gaze["speech"]["params"]["room"] == "#standup"
        assert gaze["thoughts"]["params"]["room"] == "#trip-thoughts"

    def test_pm_room(self, hub_dir):
        adapter_api.set_gaze("Cinco", room="pm:eric")
        gaze_file = hub_dir.parent / "agents" / "Cinco" / "asdaaas" / "gaze.json"
        with open(gaze_file) as f:
            gaze = json.load(f)
        assert gaze["speech"]["params"]["room"] == "pm:eric"

    def test_custom_thoughts(self, hub_dir):
        adapter_api.set_gaze("Q", room="#standup", thoughts_room="#q-lab")
        gaze_file = hub_dir.parent / "agents" / "Q" / "asdaaas" / "gaze.json"
        with open(gaze_file) as f:
            gaze = json.load(f)
        assert gaze["thoughts"]["params"]["room"] == "#q-lab"

    def test_overwrites_existing(self, hub_dir):
        adapter_api.set_gaze("Trip", room="#standup")
        adapter_api.set_gaze("Trip", room="pm:Jr")
        gaze_file = hub_dir.parent / "agents" / "Trip" / "asdaaas" / "gaze.json"
        with open(gaze_file) as f:
            gaze = json.load(f)
        assert gaze["speech"]["params"]["room"] == "pm:Jr"


class TestSetAwareness:
    def test_writes_awareness_file(self, hub_dir):
        adapter_api.set_awareness("Trip", background_channels={"#standup": "doorbell"})
        aw_file = hub_dir.parent / "agents" / "Trip" / "asdaaas" / "awareness.json"
        assert aw_file.exists()
        with open(aw_file) as f:
            aw = json.load(f)
        assert aw["background_channels"]["#standup"] == "doorbell"
        assert aw["background_default"] == "pending"

    def test_drop_default(self, hub_dir):
        adapter_api.set_awareness("Q", background_default="drop")
        aw_file = hub_dir.parent / "agents" / "Q" / "asdaaas" / "awareness.json"
        with open(aw_file) as f:
            aw = json.load(f)
        assert aw["background_default"] == "drop"

    def test_multiple_channels(self, hub_dir):
        adapter_api.set_awareness("Trip", background_channels={
            "#standup": "doorbell",
            "#trip-thoughts": "drop",
            "#general": "pending",
        })
        aw_file = hub_dir.parent / "agents" / "Trip" / "asdaaas" / "awareness.json"
        with open(aw_file) as f:
            aw = json.load(f)
        assert len(aw["background_channels"]) == 3
        assert aw["background_channels"]["#trip-thoughts"] == "drop"
