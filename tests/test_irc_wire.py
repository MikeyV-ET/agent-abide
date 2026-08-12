"""L1/L2 IRC wire tests: miniircd + raw client + adapter nick catalog.

Does NOT start asdaaas/grok/TUI. Asserts on the IRC wire and channel log —
the same surface the Room tab watches.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapters"))
import irc_adapter

MINIIRCD = Path.home() / ".local" / "bin" / "miniircd"
IRC_HOST = "127.0.0.1"


def _free_port() -> int:
    s = socket.socket()
    s.bind((IRC_HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _recv_until(sock: socket.socket, needle: bytes, timeout: float = 5.0) -> bytes:
    sock.settimeout(0.5)
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if needle in buf:
            return buf
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        except socket.timeout:
            continue
    return buf


@pytest.fixture(scope="module")
def irc_server():
    """Start miniircd on an ephemeral port with a temp channel-log dir."""
    if not MINIIRCD.exists():
        pytest.skip("miniircd not installed at ~/.local/bin/miniircd")
    port = _free_port()
    log_dir = tempfile.mkdtemp(prefix="irc_wire_logs_")
    proc = subprocess.Popen(
        [
            sys.executable,
            str(MINIIRCD),
            "--listen",
            IRC_HOST,
            "--ports",
            str(port),
            "--channel-log-dir",
            log_dir,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Wait for listen
    for _ in range(30):
        try:
            s = socket.create_connection((IRC_HOST, port), timeout=0.3)
            s.close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("miniircd did not start")
    yield {"port": port, "log_dir": Path(log_dir), "proc": proc}
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def _irc_client(port: int, nick: str, channel: str) -> socket.socket:
    s = socket.create_connection((IRC_HOST, port), timeout=5)
    s.sendall(f"NICK {nick}\r\nUSER {nick} 0 * :{nick}\r\nJOIN {channel}\r\n".encode())
    _recv_until(s, b"366")  # end of NAMES, or timeout ok
    time.sleep(0.2)
    return s


# ---------------------------------------------------------------------------
# L1 — raw wire + channel log
# ---------------------------------------------------------------------------

def test_l1_privmsg_appears_on_peer_and_log(irc_server):
    port = irc_server["port"]
    channel = "#wiretest"
    log_dir = irc_server["log_dir"]

    alice = _irc_client(port, "alice", channel)
    bob = _irc_client(port, "bob", channel)
    try:
        token = f"wire-l1-{int(time.time())}"
        alice.sendall(f"PRIVMSG {channel} :{token}\r\n".encode())
        # Bob should receive PRIVMSG
        data = _recv_until(bob, token.encode(), timeout=5)
        assert token.encode() in data, f"bob did not see PRIVMSG: {data!r}"

        # Channel log (miniircd) should get a line
        log_path = log_dir / f"{channel}.log"
        deadline = time.time() + 5
        body = ""
        while time.time() < deadline:
            if log_path.exists():
                body = log_path.read_text(errors="replace")
                if token in body:
                    break
            time.sleep(0.1)
        assert token in body, f"channel log missing message: {body!r}"
    finally:
        alice.close()
        bob.close()


# ---------------------------------------------------------------------------
# L2 — adapter catalog + per-connection join semantics
# ---------------------------------------------------------------------------

def test_l2_load_agent_nicks_includes_squiggy():
    nicks = irc_adapter.load_agent_nicks()
    assert "Squiggy" in nicks
    assert nicks["Squiggy"]
    # Core cast present
    for name in ("Sr", "Jr", "Trip", "Q", "Cinco", "Squiggy"):
        assert name in nicks, f"missing {name}"


def test_l2_join_if_needed_is_per_connection(irc_server):
    """Two nicks both must JOIN — global join set must not skip second nick."""
    port = irc_server["port"]
    channel = "#jointest"

    async def _run():
        import asyncio
        a = irc_adapter.IRCConnection("nickA", channel, IRC_HOST, port, "AgentA")
        b = irc_adapter.IRCConnection("nickB", channel, IRC_HOST, port, "AgentB")
        await a.connect()
        await b.connect()
        await a.join_if_needed(channel)
        # Critical: B must still join even if A already joined same channel name
        await b.join_if_needed(channel)
        assert channel in a._joined
        assert channel in b._joined
        # Witness: third client sees both in channel after PRIVMSG from each
        watcher = _irc_client(port, "watch", channel)
        try:
            await a.send("from-A", target=channel)
            await b.send("from-B", target=channel)
            await asyncio.sleep(0.3)
            data = _recv_until(watcher, b"from-B", timeout=5)
            assert b"from-A" in data or b"from-B" in data
            assert b"from-B" in data, f"nickB message missing (join skipped?): {data!r}"
        finally:
            watcher.close()
            await a.close()
            await b.close()

    import asyncio
    asyncio.run(_run())


def test_l2_speech_path_join_then_send(irc_server):
    """Mimic gaze #room: join_if_needed then PRIVMSG — peer sees it."""
    port = irc_server["port"]
    channel = "#meetingroom1"

    async def _run():
        import asyncio
        conn = irc_adapter.IRCConnection("Squiggy", "#standup", IRC_HOST, port, "Squiggy")
        await conn.connect()
        # Was bug: send to #meetingroom1 without this nick JOINing
        await conn.join_if_needed(channel)
        peer = _irc_client(port, "eric_tui", channel)
        try:
            token = f"squiggy-hi-{int(time.time())}"
            await conn.send(token, target=channel)
            data = _recv_until(peer, token.encode(), timeout=5)
            assert token.encode() in data
        finally:
            peer.close()
            await conn.close()

    import asyncio
    asyncio.run(_run())
