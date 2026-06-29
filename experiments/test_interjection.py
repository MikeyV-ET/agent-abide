#!/usr/bin/env python3
"""
Experimental ACP client for testing mid-turn interjection.

Tests two capabilities:
1. conversation.queue.interject — inject messages mid-turn
2. session_recap — observe post-turn summary events

Usage:
    python3 experiments/test_interjection.py [--workdir DIR]

Spawns grok agent stdio, runs a multi-tool-call prompt, injects an
interjection mid-turn, and logs everything that happens.
"""

import asyncio
import json
import os
import sys
import time
import argparse

# ── JSON-RPC helpers (borrowed from asdaaas.py) ──────────────────────────

_rpc_id = 0

def rpc_request(method, params=None):
    global _rpc_id
    _rpc_id += 1
    msg = {"jsonrpc": "2.0", "method": method, "id": _rpc_id}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg) + "\n", _rpc_id

def rpc_notification(method, params=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg) + "\n"


async def read_frame(stdout):
    """Read one JSON-RPC frame from stdout."""
    chunks = []
    while True:
        try:
            chunk = await stdout.readuntil(b'\n')
            chunks.append(chunk)
            break
        except asyncio.LimitOverrunError as e:
            chunk = await stdout.read(e.consumed)
            chunks.append(chunk)
        except asyncio.IncompleteReadError as e:
            if e.partial:
                chunks.append(e.partial)
            if not chunks:
                return None
            break
    data = b"".join(chunks)
    if not data:
        return None
    return json.loads(data.decode("utf-8").strip())


async def send(stdin, msg_str):
    """Send a JSON-RPC message."""
    stdin.write(msg_str.encode("utf-8"))
    await stdin.drain()


async def send_and_wait(proc_stdin, proc_stdout, method, params=None, timeout=30.0):
    """Send a request and wait for the matching response."""
    msg_str, msg_id = rpc_request(method, params)
    print(f"  → {method} (id={msg_id})")
    await send(proc_stdin, msg_str)

    deadline = time.monotonic() + timeout
    notifications = []
    while time.monotonic() < deadline:
        try:
            frame = await asyncio.wait_for(
                read_frame(proc_stdout),
                timeout=max(0.5, deadline - time.monotonic())
            )
        except asyncio.TimeoutError:
            break
        if frame is None:
            raise RuntimeError("Binary closed stdout")
        if frame.get("id") == msg_id:
            print(f"  ← response: {json.dumps(frame.get('result', {}))[:200]}")
            return frame.get("result", {}), notifications
        else:
            notifications.append(frame)
            _log_notification(frame)

    raise TimeoutError(f"No response to {method} (id={msg_id}) within {timeout}s")


def _log_notification(frame):
    """Log a notification frame concisely."""
    method = frame.get("method", "???")
    params = frame.get("params", {})
    update = params.get("update", {})
    su = update.get("sessionUpdate", "")

    if su == "agent_message_chunk":
        text = update.get("content", {}).get("text", "")
        if text.strip():
            print(f"  📝 agent: {text[:120]}")
    elif su == "tool_call":
        print(f"  🔧 tool_call: {update.get('title', '?')} (id={update.get('toolCallId', '?')[:20]})")
    elif su == "tool_call_update":
        status = update.get("status", "?")
        print(f"  🔧 tool_update: {update.get('toolCallId', '?')[:20]} → {status}")
    elif su == "session_recap":
        print(f"  📋 SESSION_RECAP: {update.get('summary', '')[:200]}")
    elif su == "agent_thought_chunk":
        pass  # skip thinking noise
    elif su:
        print(f"  📦 {su}: {json.dumps(update)[:150]}")
    elif method:
        print(f"  📨 {method}: {json.dumps(params)[:150]}")


# ── Main experiment ──────────────────────────────────────────────────────

async def run_experiment(workdir: str, interject_after_n_tools: int = 2):
    """
    1. Start grok agent stdio
    2. Initialize + create session
    3. Send a prompt that triggers multiple tool calls
    4. After N tool calls, send conversation.queue.interject
    5. Log everything — especially interjection delivery and session_recap
    """
    print("=" * 60)
    print("INTERJECTION EXPERIMENT")
    print("=" * 60)

    grok_bin = os.path.expanduser("~/.grok/bin/grok")
    if not os.path.exists(grok_bin):
        print(f"ERROR: grok binary not found at {grok_bin}")
        return

    print(f"\n1. Starting grok agent stdio (workdir={workdir})...")
    proc = await asyncio.create_subprocess_exec(
        grok_bin, "agent", "--always-approve", "stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
        limit=10 * 1024 * 1024,  # 10MB buffer
    )
    print(f"   PID: {proc.pid}")

    try:
        # Step 2: Initialize
        print("\n2. Initializing ACP session...")
        result, _ = await send_and_wait(proc.stdin, proc.stdout, "initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
            },
        })
        print(f"   Server: {result.get('serverInfo', {}).get('name', '?')} v{result.get('serverInfo', {}).get('version', '?')}")

        # Step 3: Create session
        print("\n3. Creating new session...")
        result, _ = await send_and_wait(proc.stdin, proc.stdout, "session/new", {
            "cwd": workdir,
            "mcpServers": [],
        })
        session_id = result.get("sessionId", "")
        print(f"   Session ID: {session_id}")

        if not session_id:
            print("ERROR: No session ID returned")
            return

        # Step 4: Send a multi-tool-call prompt
        print("\n4. Sending prompt (designed to trigger multiple tool calls)...")
        prompt_text = (
            "List the files in the current directory, then read the first "
            "file you find, then list files again. Do all three as separate "
            "tool calls."
        )
        prompt_msg, prompt_id = rpc_request("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": prompt_text}],
        })
        await send(proc.stdin, prompt_msg)
        print(f"   Prompt sent (id={prompt_id})")

        # Step 5: Stream updates, inject after N tool calls
        print(f"\n5. Streaming updates (will interject after {interject_after_n_tools} tool calls)...")
        tool_call_count = 0
        interjected = False
        saw_recap = False
        interjection_event = None
        all_events = []

        deadline = time.monotonic() + 120.0  # 2 minute max
        while time.monotonic() < deadline:
            try:
                frame = await asyncio.wait_for(
                    read_frame(proc.stdout),
                    timeout=max(0.5, deadline - time.monotonic())
                )
            except asyncio.TimeoutError:
                print("   ⏰ Timeout waiting for frame")
                break

            if frame is None:
                print("   ⚠️ Binary closed stdout")
                break

            all_events.append(frame)

            # Check for final response
            if frame.get("id") == prompt_id:
                print(f"\n   ✅ Prompt complete (response id={prompt_id})")
                break

            # Log the notification
            _log_notification(frame)

            # Track tool calls
            method = frame.get("method", "")
            params = frame.get("params", {})
            update = params.get("update", {})
            su = update.get("sessionUpdate", "")

            if su == "tool_call":
                tool_call_count += 1

            # Interject after N tool completions
            if su == "tool_call_update" and update.get("status") == "completed":
                completed_tools = sum(
                    1 for e in all_events
                    if e.get("params", {}).get("update", {}).get("sessionUpdate") == "tool_call_update"
                    and e.get("params", {}).get("update", {}).get("status") == "completed"
                )
                if completed_tools >= interject_after_n_tools and not interjected:
                    print(f"\n   🎯 INJECTING INTERJECTION (after {completed_tools} completed tools)...")

                    # Try as notification (no id) — fire and forget
                    for method_name in [
                        "conversation.queue.interject",
                        "x.ai/queue/interject",
                    ]:
                        notif = rpc_notification(method_name, {
                            "sessionId": session_id,
                            "text": "INTERJECTION: Tell me the time too.",
                        })
                        await send(proc.stdin, notif)
                        print(f"   → Sent notification: {method_name}")

                    # Also try as request for the one most likely to work
                    interject_msg, interject_id = rpc_request(
                        "conversation.queue.interject",
                        {
                            "sessionId": session_id,
                            "text": "INTERJECTION: Tell me the time too.",
                        }
                    )
                    await send(proc.stdin, interject_msg)
                    print(f"   → Sent request: conversation.queue.interject (id={interject_id})")
                    await send(proc.stdin, interject_msg)
                    print(f"   → Sent interject (id={interject_id})")
                    interjected = True

            if su == "session_recap":
                saw_recap = True

        # Step 6: Wait a bit for post-turn events (session_recap)
        print("\n6. Waiting for post-turn events (session_recap)...")
        post_deadline = time.monotonic() + 15.0
        while time.monotonic() < post_deadline:
            try:
                frame = await asyncio.wait_for(
                    read_frame(proc.stdout),
                    timeout=max(0.5, post_deadline - time.monotonic())
                )
            except asyncio.TimeoutError:
                break
            if frame is None:
                break
            all_events.append(frame)
            _log_notification(frame)
            su = frame.get("params", {}).get("update", {}).get("sessionUpdate", "")
            if su == "session_recap":
                saw_recap = True

        # Step 7: Summary
        print("\n" + "=" * 60)
        print("RESULTS")
        print("=" * 60)
        print(f"  Tool calls seen: {tool_call_count}")
        print(f"  Interjection sent: {interjected}")
        print(f"  Session recap seen: {saw_recap}")

        # Check if we got a response to the interject request
        if interjected:
            interject_responses = [
                e for e in all_events
                if e.get("id") == interject_id
            ]
            if interject_responses:
                resp = interject_responses[0]
                if "error" in resp:
                    print(f"  Interject response: ERROR — {resp['error']}")
                else:
                    print(f"  Interject response: OK — {json.dumps(resp.get('result', {}))[:200]}")
            else:
                print("  Interject response: (none received)")

        # Dump all event types seen
        event_types = []
        for e in all_events:
            su = e.get("params", {}).get("update", {}).get("sessionUpdate", "")
            if su:
                event_types.append(su)
            elif e.get("id"):
                event_types.append(f"response:{e['id']}")
            elif e.get("method"):
                event_types.append(e["method"])
        print(f"\n  Event sequence ({len(event_types)} events):")
        for i, et in enumerate(event_types):
            print(f"    {i+1}. {et}")

        # Save full log
        log_path = os.path.join(workdir, "interjection_experiment.jsonl")
        with open(log_path, "w") as f:
            for e in all_events:
                f.write(json.dumps(e) + "\n")
        print(f"\n  Full log saved to: {log_path}")

    finally:
        print("\n7. Cleaning up...")
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
        print("   Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test mid-turn interjection")
    parser.add_argument("--workdir", default="/tmp/interjection-test",
                        help="Working directory for the test session")
    parser.add_argument("--interject-after", type=int, default=1,
                        help="Send interjection after N completed tool calls")
    args = parser.parse_args()

    os.makedirs(args.workdir, exist_ok=True)
    asyncio.run(run_experiment(args.workdir, args.interject_after))
