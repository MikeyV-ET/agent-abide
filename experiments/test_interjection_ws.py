#!/usr/bin/env python3
"""
Test x.ai/interject via WebSocket serve mode.

Spawns `grok agent serve`, connects over WebSocket, sends a multi-tool
prompt, and attempts x.ai/interject mid-turn. Compares behavior to
stdio mode (which returns -32601 for all interject methods).

Usage:
    python3 experiments/test_interjection_ws.py [--port PORT]
"""

import asyncio
import json
import os
import re
import sys
import time
import uuid
import argparse

import websockets

# ── JSON-RPC helpers ──────────────────────────────────────────────────

_rpc_id = 0

def rpc_request(method, params=None):
    global _rpc_id
    _rpc_id += 1
    msg = {"jsonrpc": "2.0", "method": method, "id": _rpc_id}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg), _rpc_id

def rpc_notification(method, params=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def _log_update(frame):
    """Log a notification frame concisely."""
    method = frame.get("method", "???")
    params = frame.get("params", {})
    update = params.get("update", {})
    su = update.get("sessionUpdate", "")

    if su == "agent_message_chunk":
        text = update.get("content", {}).get("text", "")
        if text.strip():
            print(f"  agent: {text[:120]}")
    elif su == "tool_call":
        print(f"  tool_call: {update.get('title', '?')} (id={update.get('toolCallId', '?')[:20]})")
    elif su == "tool_call_update":
        status = update.get("status", "?")
        print(f"  tool_update: {update.get('toolCallId', '?')[:20]} -> {status}")
    elif su == "session_recap":
        print(f"  SESSION_RECAP: {update.get('summary', '')[:200]}")
    elif su == "agent_thought_chunk":
        pass
    elif su:
        print(f"  {su}: {json.dumps(update)[:150]}")
    elif method:
        print(f"  {method}: {json.dumps(params)[:150]}")


async def ws_send_and_wait(ws, method, params=None, timeout=30.0):
    """Send a request over WebSocket and wait for matching response."""
    msg_str, msg_id = rpc_request(method, params)
    print(f"  -> {method} (id={msg_id})")
    await ws.send(msg_str)

    deadline = time.monotonic() + timeout
    notifications = []
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(
                ws.recv(),
                timeout=max(0.5, deadline - time.monotonic())
            )
            frame = json.loads(raw)
        except asyncio.TimeoutError:
            break

        if frame.get("id") == msg_id:
            result = frame.get("result", {})
            error = frame.get("error")
            if error:
                print(f"  <- ERROR: {error}")
            else:
                print(f"  <- response: {json.dumps(result)[:200]}")
            return frame, notifications
        else:
            notifications.append(frame)
            _log_update(frame)

    raise TimeoutError(f"No response to {method} (id={msg_id}) within {timeout}s")


async def run_experiment(port: int, workdir: str):
    print("=" * 60)
    print("INTERJECTION EXPERIMENT — WebSocket serve mode")
    print("=" * 60)

    grok_bin = os.path.expanduser("~/.grok/bin/grok")
    if not os.path.exists(grok_bin):
        print(f"ERROR: grok binary not found at {grok_bin}")
        return

    bind_addr = f"127.0.0.1:{port}"

    # Step 1: Start grok agent serve
    print(f"\n1. Starting grok agent serve --bind {bind_addr} ...")
    proc = await asyncio.create_subprocess_exec(
        grok_bin, "agent", "--always-approve", "serve", "--bind", bind_addr,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
    )
    print(f"   PID: {proc.pid}")

    # Capture the secret from startup output (stderr or stdout)
    secret = None
    print("   Waiting for secret token...")
    deadline = time.monotonic() + 15.0
    
    # Read stderr for startup messages (the secret is usually printed there)
    collected_output = []
    while time.monotonic() < deadline:
        try:
            line = await asyncio.wait_for(proc.stderr.readline(), timeout=1.0)
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            collected_output.append(text)
            print(f"   stderr: {text}")
            
            # Look for secret in various formats
            # Common patterns: "secret: abc123", "Secret: abc123", "token: abc123"
            for pattern in [
                r'[Ss]ecret[:\s]+(\S+)',
                r'[Tt]oken[:\s]+(\S+)',
                r'--secret\s+(\S+)',
            ]:
                m = re.search(pattern, text)
                if m:
                    secret = m.group(1)
                    print(f"   Found secret: {secret[:8]}...")
                    break
            if secret:
                break
            
            # Also check if it says "listening" — might mean ready
            if "listen" in text.lower() or "ready" in text.lower() or "bound" in text.lower():
                # Give it one more second for secret line
                await asyncio.sleep(0.5)
        except asyncio.TimeoutError:
            # Check if process is still alive
            if proc.returncode is not None:
                remaining = await proc.stderr.read()
                print(f"   Process exited! rc={proc.returncode}")
                print(f"   stderr: {remaining.decode('utf-8', errors='replace')}")
                return
            # Maybe output is on stdout
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=0.5)
                if line:
                    text = line.decode("utf-8", errors="replace").strip()
                    collected_output.append(text)
                    print(f"   stdout: {text}")
                    for pattern in [r'[Ss]ecret[:\s]+(\S+)', r'[Tt]oken[:\s]+(\S+)']:
                        m = re.search(pattern, text)
                        if m:
                            secret = m.group(1)
                            print(f"   Found secret: {secret[:8]}...")
                            break
            except asyncio.TimeoutError:
                pass

    if not secret:
        print("   WARNING: Could not find secret token in output.")
        print(f"   Collected output: {collected_output}")
        print("   Trying without auth header...")

    try:
        # Step 2: Connect via WebSocket
        print(f"\n2. Connecting to ws://{bind_addr} ...")
        headers = {}
        if secret:
            headers["x-grok-secret"] = secret
            headers["Authorization"] = f"Bearer {secret}"
        
        # Try common WebSocket paths
        ws = None
        for path in ["/ws", "/", "/agent", "/acp", "/v1"]:
            try:
                ws = await asyncio.wait_for(
                    websockets.connect(f"ws://{bind_addr}{path}", additional_headers=headers),
                    timeout=5.0
                )
                print(f"   Connected on path: {path}")
                break
            except Exception as e:
                print(f"   Path {path}: {e}")
        
        if ws is None:
            print("   ERROR: Could not connect on any known path")
            return

        # Step 3: Initialize
        print("\n3. Initializing ACP session...")
        resp, _ = await ws_send_and_wait(ws, "initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
            },
        })
        result = resp.get("result", {})
        print(f"   Server: {result.get('serverInfo', {}).get('name', '?')} v{result.get('serverInfo', {}).get('version', '?')}")

        # Step 4: Create session
        print("\n4. Creating new session...")
        resp, _ = await ws_send_and_wait(ws, "session/new", {
            "cwd": workdir,
            "mcpServers": [],
        })
        session_id = resp.get("result", {}).get("sessionId", "")
        print(f"   Session ID: {session_id}")

        if not session_id:
            print("ERROR: No session ID returned")
            return

        # Step 5: Test x.ai/interject directly (before any prompt)
        # This tells us immediately if the method is registered
        print("\n5. Testing method availability (pre-prompt)...")
        methods_to_test = [
            "x.ai/interject",
            "conversation.queue.interject",
            "x.ai/queue/interject",
        ]
        for method in methods_to_test:
            try:
                resp, _ = await ws_send_and_wait(ws, method, {
                    "sessionId": session_id,
                    "text": "test",
                    "interjectionId": str(uuid.uuid4()),
                }, timeout=5.0)
                error = resp.get("error")
                if error:
                    code = error.get("code", "?")
                    msg = error.get("message", "?")
                    print(f"   {method}: ERROR {code} — {msg}")
                else:
                    print(f"   {method}: OK! {json.dumps(resp.get('result', {}))[:100]}")
            except TimeoutError:
                print(f"   {method}: TIMEOUT (no response)")

        # Step 6: Send a multi-tool-call prompt and try interject mid-turn
        print("\n6. Sending prompt + attempting mid-turn interject...")
        prompt_text = (
            "List the files in the current directory, then read any file you find, "
            "then list files again. Do all three as separate tool calls."
        )
        prompt_msg, prompt_id = rpc_request("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": prompt_text}],
        })
        await ws.send(prompt_msg)
        print(f"   Prompt sent (id={prompt_id})")

        tool_completions = 0
        interjected = False
        all_events = []
        interject_id = None

        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                frame = json.loads(raw)
            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed:
                print("   WebSocket closed!")
                break

            all_events.append(frame)

            if frame.get("id") == prompt_id:
                print(f"\n   Prompt complete (response id={prompt_id})")
                break

            # Check for interject response
            if interject_id and frame.get("id") == interject_id:
                error = frame.get("error")
                if error:
                    print(f"   INTERJECT RESPONSE: ERROR {error.get('code')} — {error.get('message')}")
                else:
                    print(f"   INTERJECT RESPONSE: OK! {json.dumps(frame.get('result', {}))[:200]}")
                continue

            _log_update(frame)

            su = frame.get("params", {}).get("update", {}).get("sessionUpdate", "")
            if su == "tool_call_update" and frame.get("params", {}).get("update", {}).get("status") == "completed":
                tool_completions += 1
                if tool_completions >= 1 and not interjected:
                    print(f"\n   INJECTING x.ai/interject (after {tool_completions} tool completions)...")
                    iid = str(uuid.uuid4())
                    interject_msg, interject_id = rpc_request("x.ai/interject", {
                        "sessionId": session_id,
                        "text": "INTERJECTION: Also tell me the current date and time.",
                        "interjectionId": iid,
                    })
                    await ws.send(interject_msg)
                    print(f"   -> Sent x.ai/interject (id={interject_id}, interjectionId={iid[:12]}...)")
                    interjected = True

        # Step 7: Wait for post-turn events
        print("\n7. Waiting for post-turn events...")
        post_deadline = time.monotonic() + 10.0
        while time.monotonic() < post_deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                frame = json.loads(raw)
                all_events.append(frame)
                _log_update(frame)
            except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                break

        # Step 8: Summary
        print("\n" + "=" * 60)
        print("RESULTS")
        print("=" * 60)
        print(f"  Transport: WebSocket serve mode")
        print(f"  Tool completions seen: {tool_completions}")
        print(f"  Interjection attempted: {interjected}")

        event_types = []
        for e in all_events:
            su = e.get("params", {}).get("update", {}).get("sessionUpdate", "")
            if su:
                event_types.append(su)
            elif e.get("error"):
                event_types.append(f"error:{e.get('id','?')}")
            elif e.get("id"):
                event_types.append(f"response:{e['id']}")
            elif e.get("method"):
                event_types.append(e["method"])
        print(f"\n  Event sequence ({len(event_types)} events):")
        for i, et in enumerate(event_types):
            print(f"    {i+1}. {et}")

        log_path = os.path.join(workdir, "interjection_ws_experiment.jsonl")
        with open(log_path, "w") as f:
            for e in all_events:
                f.write(json.dumps(e) + "\n")
        print(f"\n  Full log: {log_path}")

        await ws.close()

    finally:
        print("\n8. Cleaning up...")
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
        print("   Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test interjection via WebSocket serve mode")
    parser.add_argument("--port", type=int, default=2419, help="Port for WebSocket server")
    parser.add_argument("--workdir", default="/tmp/interjection-ws-test",
                        help="Working directory for test session")
    args = parser.parse_args()

    os.makedirs(args.workdir, exist_ok=True)
    asyncio.run(run_experiment(args.port, args.workdir))
