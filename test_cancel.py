#!/usr/bin/env python3
"""Test mid-turn cancel on grok agent stdio.

Launches a session, sends a prompt that triggers repeated tool calls,
then tries cancel methods while the agent is working.

Usage:
    python3 test_cancel.py              # run the test
    python3 test_cancel.py --method sigint   # try SIGINT
    python3 test_cancel.py --method slash    # try /cancel via session/prompt
    python3 test_cancel.py --method esc      # try raw Esc bytes
"""

import asyncio
import json
import signal
import sys
import os
import argparse
import time

GROK_BINARY = os.path.expanduser("~/.grok/bin/archive/grok-0.1.174.et")
TEST_CWD = os.path.expanduser("~/agents/testagent")

rpc_id = 0

def rpc_request(method, params=None):
    global rpc_id
    rpc_id += 1
    msg = {"jsonrpc": "2.0", "method": method, "id": rpc_id}
    if params:
        msg["params"] = params
    return json.dumps(msg) + "\n", rpc_id

def rpc_notification(method, params=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if params:
        msg["params"] = params
    return json.dumps(msg) + "\n"

last_response = {}

async def read_frames(proc, duration=60):
    """Read and print frames for `duration` seconds."""
    global last_response
    start = time.time()
    while time.time() - start < duration:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
            if not line:
                print("[EOF]")
                break
            frame = json.loads(line)
            method = frame.get("method", "")
            if method == "session/update":
                update = frame["params"]["update"]
                su = update.get("sessionUpdate", "")
                if su == "agent_message_chunk":
                    text = update.get("content", {}).get("text", "")
                    if text:
                        print(f"[speech] {text}", end="", flush=True)
                elif su == "tool_call":
                    print(f"\n[tool_call] {update.get('title', '?')}", flush=True)
                elif su == "tool_call_update":
                    status = update.get("status", "")
                    if status:
                        print(f"[tool_update] {status}", flush=True)
                else:
                    print(f"[update] {su}", flush=True)
            elif frame.get("id"):
                last_response = frame
                print(f"\n[response id={frame['id']}] {json.dumps(frame.get('result', frame.get('error', {})))[:200]}", flush=True)
                if frame["id"] == rpc_id:
                    print("[prompt complete — agent turn ended]")
                    return
            elif method == "_x.ai/session/prompt_complete":
                print("[prompt_complete signal]", flush=True)
            else:
                print(f"[frame] {method or 'response'}: {str(frame)[:150]}", flush=True)
        except asyncio.TimeoutError:
            elapsed = int(time.time() - start)
            print(f"\r  [{elapsed}s elapsed]", end="", flush=True)
    print("\n[timeout reached]")

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--method", choices=["sigint", "slash", "esc", "cancelreq", "cancelled", "none"], default="none",
                        help="Cancel method to try")
    parser.add_argument("--delay", type=int, default=15,
                        help="Seconds to wait before sending cancel (default: 15)")
    args = parser.parse_args()

    os.makedirs(TEST_CWD, exist_ok=True)

    print(f"Launching: {GROK_BINARY} agent stdio")
    proc = await asyncio.create_subprocess_exec(
        GROK_BINARY, "agent", "stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=TEST_CWD,
    )
    print(f"PID: {proc.pid}")

    # Initialize
    msg, mid = rpc_request("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "cancel-test", "version": "0.1"},
    })
    proc.stdin.write(msg.encode())
    await proc.stdin.drain()
    print("[sent] initialize")
    await read_frames(proc, duration=10)

    proc.stdin.write(rpc_notification("notifications/initialized").encode())
    await proc.stdin.drain()
    print("[sent] notifications/initialized")

    # New session
    msg, mid = rpc_request("session/new", {"cwd": TEST_CWD, "mcpServers": []})
    proc.stdin.write(msg.encode())
    await proc.stdin.drain()
    print("[sent] session/new")
    await read_frames(proc, duration=15)

    session_id = last_response.get("result", {}).get("sessionId", "unknown")
    print(f"[session] {session_id}")

    # Yolo mode
    msg, mid = rpc_request("session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "/yolo on"}],
    })
    proc.stdin.write(msg.encode())
    await proc.stdin.drain()
    print("[sent] /yolo on")
    await read_frames(proc, duration=10)

    # Send the long-running prompt
    prompt_text = "Run this bash command 5 times, one at a time: echo 'hello from attempt N' && sleep 5. Replace N with the attempt number (1 through 5). Do each one separately."
    msg, prompt_id = rpc_request("session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": prompt_text}],
    })
    proc.stdin.write(msg.encode())
    await proc.stdin.drain()
    print(f"[sent] prompt (id={prompt_id}): {prompt_text[:80]}...")

    if args.method == "none":
        print(f"\n=== No cancel method selected. Watching for {60}s ===")
        await read_frames(proc, duration=60)
    else:
        print(f"\n=== Waiting {args.delay}s then sending cancel method: {args.method} ===")
        # Read for delay seconds
        await read_frames(proc, duration=args.delay)

        if args.method == "sigint":
            print(f"\n>>> Sending SIGINT to PID {proc.pid}")
            os.kill(proc.pid, signal.SIGINT)
        elif args.method == "slash":
            print(f"\n>>> Sending /cancel via session/prompt")
            cancel_msg, _ = rpc_request("session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "/cancel"}],
            })
            proc.stdin.write(cancel_msg.encode())
            await proc.stdin.drain()
        elif args.method == "esc":
            print(f"\n>>> Sending double-Esc bytes")
            proc.stdin.write(b"\x1b\x1b")
            await proc.stdin.drain()
        elif args.method == "cancelreq":
            print(f"\n>>> Sending $/cancelRequest for prompt id={prompt_id}")
            cancel_msg = json.dumps({"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": prompt_id}}) + "\n"
            proc.stdin.write(cancel_msg.encode())
            await proc.stdin.drain()
        elif args.method == "cancelled":
            print(f"\n>>> Sending notifications/cancelled for prompt id={prompt_id}")
            cancel_msg = json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": prompt_id}}) + "\n"
            proc.stdin.write(cancel_msg.encode())
            await proc.stdin.drain()

        print(">>> Cancel sent. Reading response...")
        await read_frames(proc, duration=30)

    proc.terminate()
    print("\n[done]")

if __name__ == "__main__":
    asyncio.run(main())
