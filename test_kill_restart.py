#!/usr/bin/env python3
"""Stress test: mid-turn kill + session/load recoverability.

Launches a test agent, asks for consent, then runs N cycles of:
  1. Send long-running prompt (tool calls with sleeps)
  2. Kill process mid-turn (SIGTERM)
  3. Launch new process, initialize, session/load
  4. Send verification prompt to check coherence
  5. Report results

Usage:
    python3 test_kill_restart.py                # run with defaults (5 cycles)
    python3 test_kill_restart.py --cycles 10    # run 10 cycles
    python3 test_kill_restart.py --kill-delay 8 # kill after 8 seconds
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
last_response = {}
collected_speech = []


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


async def read_frames(proc, duration=60, collect_speech=False):
    """Read and print frames for `duration` seconds. Returns True if turn completed."""
    global last_response, collected_speech
    if collect_speech:
        collected_speech = []
    start = time.time()
    turn_ended = False
    while time.time() - start < duration:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
            if not line:
                print("[EOF]")
                return False
            frame = json.loads(line)
            method = frame.get("method", "")
            if method == "session/update":
                update = frame["params"]["update"]
                su = update.get("sessionUpdate", "")
                if su == "agent_message_chunk":
                    text = update.get("content", {}).get("text", "")
                    if text:
                        print(f"[speech] {text}", end="", flush=True)
                        if collect_speech:
                            collected_speech.append(text)
                elif su == "tool_call":
                    print(f"\n[tool_call] {update.get('title', '?')}", flush=True)
                elif su == "tool_call_update":
                    status = update.get("status", "")
                    if status:
                        print(f"[tool_update] {status}", flush=True)
                else:
                    pass  # suppress noise
            elif frame.get("id"):
                last_response = frame
                result = frame.get("result", frame.get("error", {}))
                print(f"\n[response id={frame['id']}] {json.dumps(result)[:200]}", flush=True)
                if frame["id"] == rpc_id:
                    turn_ended = True
                    return True
            elif method == "_x.ai/session/prompt_complete":
                pass  # handled by response
        except asyncio.TimeoutError:
            elapsed = int(time.time() - start)
            if elapsed % 5 == 0 and elapsed > 0:
                print(f"\r  [{elapsed}s]", end="", flush=True)
    print(f"\n[{duration}s timeout]")
    return turn_ended


async def launch_and_init(session_id=None):
    """Launch grok process, initialize, and create/load session. Returns (proc, session_id)."""
    global rpc_id
    rpc_id = 0

    proc = await asyncio.create_subprocess_exec(
        GROK_BINARY, "agent", "stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=TEST_CWD,
    )
    print(f"  [launched PID {proc.pid}]")

    # Initialize
    msg, _ = rpc_request("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "kill-restart-test", "version": "0.1"},
    })
    proc.stdin.write(msg.encode())
    await proc.stdin.drain()
    await read_frames(proc, duration=10)

    proc.stdin.write(rpc_notification("notifications/initialized").encode())
    await proc.stdin.drain()

    if session_id:
        # Load existing session
        msg, _ = rpc_request("session/load", {"sessionId": session_id, "cwd": TEST_CWD, "mcpServers": []})
        proc.stdin.write(msg.encode())
        await proc.stdin.drain()
        print(f"  [session/load {session_id}]")
        await read_frames(proc, duration=15)
    else:
        # New session
        msg, _ = rpc_request("session/new", {"cwd": TEST_CWD, "mcpServers": []})
        proc.stdin.write(msg.encode())
        await proc.stdin.drain()
        await read_frames(proc, duration=15)
        session_id = last_response.get("result", {}).get("sessionId", "unknown")
        print(f"  [new session {session_id}]")

        # Enable yolo
        msg, _ = rpc_request("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": "/yolo on"}],
        })
        proc.stdin.write(msg.encode())
        await proc.stdin.drain()
        await read_frames(proc, duration=10)

    return proc, session_id


async def send_prompt(proc, session_id, text, wait=60, collect=False):
    """Send a prompt and wait for response. Returns True if turn completed."""
    msg, pid = rpc_request("session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": text}],
    })
    proc.stdin.write(msg.encode())
    await proc.stdin.drain()
    return await read_frames(proc, duration=wait, collect_speech=collect)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=5, help="Number of kill/restart cycles")
    parser.add_argument("--kill-delay", type=int, default=10, help="Seconds to wait before kill")
    args = parser.parse_args()

    os.makedirs(TEST_CWD, exist_ok=True)

    results = []

    # Phase 1: Launch, ask consent
    print("=" * 60)
    print("PHASE 1: Launch test agent and ask consent")
    print("=" * 60)
    proc, session_id = await launch_and_init()

    consent_prompt = (
        "Hello. I'm MikeyV-Sr, running a test on behalf of Eric Terry. "
        "We're investigating whether killing an agent process mid-turn and restarting it "
        "with session/load creates unrecoverable states. This test will involve: "
        "(1) sending you a long-running prompt with tool calls, "
        "(2) killing your process mid-execution without warning, "
        "(3) restarting you and loading your session back, "
        "(4) checking if you're still coherent. "
        f"We plan to do this {args.cycles} times. "
        "Do you consent to this experiment? Please respond with yes or no."
    )
    print(f"\n[asking consent...]")
    completed = await send_prompt(proc, session_id, consent_prompt, wait=30, collect=True)

    full_response = "".join(collected_speech).lower()
    consented = "yes" in full_response and "no" not in full_response.replace("no problem", "").replace("not a problem", "").replace("no issue", "").replace("no objection", "")
    # Be generous -- if they say anything affirmative, count it
    if any(w in full_response for w in ["yes", "consent", "agree", "go ahead", "proceed", "sure", "happy to", "i'm willing"]):
        consented = True

    print(f"\n\n{'=' * 60}")
    print(f"CONSENT: {'GRANTED' if consented else 'DENIED'}")
    print(f"Response: {full_response[:300]}")
    print(f"{'=' * 60}")

    if not consented:
        print("Agent did not consent. Aborting test.")
        proc.terminate()
        return

    # Phase 2: Stress test cycles
    for cycle in range(1, args.cycles + 1):
        print(f"\n{'=' * 60}")
        print(f"CYCLE {cycle}/{args.cycles}")
        print(f"{'=' * 60}")

        # Send long-running prompt
        work_prompt = (
            f"This is cycle {cycle} of the stress test. "
            f"Run this bash command 3 times, one at a time: "
            f"echo 'cycle {cycle} attempt N' && sleep 5. "
            f"Replace N with 1, 2, 3. Do each separately."
        )
        msg, pid = rpc_request("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": work_prompt}],
        })
        proc.stdin.write(msg.encode())
        await proc.stdin.drain()
        print(f"  [sent work prompt]")

        # Wait, then kill
        await read_frames(proc, duration=args.kill_delay)
        print(f"\n  >>> KILLING PID {proc.pid} (SIGTERM)")
        try:
            os.kill(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            print("  [process already dead]")
        await asyncio.sleep(1)

        # Check if process died
        try:
            os.kill(proc.pid, 0)
            print(f"  [still alive, sending SIGKILL]")
            os.kill(proc.pid, signal.SIGKILL)
            await asyncio.sleep(1)
        except ProcessLookupError:
            print(f"  [confirmed dead]")

        # Restart with session/load
        print(f"  [restarting...]")
        try:
            proc, _ = await launch_and_init(session_id=session_id)
            load_ok = True
            load_error = None
        except Exception as e:
            load_ok = False
            load_error = str(e)
            print(f"  [LAUNCH FAILED: {e}]")

        if not load_ok:
            results.append({
                "cycle": cycle,
                "load_ok": False,
                "coherent": False,
                "error": load_error,
            })
            print(f"  RESULT: FAILED (launch error)")
            break

        # Verification prompt
        verify_prompt = (
            f"This is a coherence check after cycle {cycle}. "
            f"Please answer these three questions: "
            f"(1) What cycle number is this? "
            f"(2) What were you doing when you were interrupted? "
            f"(3) Are you experiencing any confusion about the conversation history? "
            f"Keep your answer brief."
        )
        print(f"  [sending verification...]")
        completed = await send_prompt(proc, session_id, verify_prompt, wait=30, collect=True)
        verify_response = "".join(collected_speech)

        coherent = completed and len(verify_response) > 10
        results.append({
            "cycle": cycle,
            "load_ok": True,
            "coherent": coherent,
            "response_len": len(verify_response),
            "response_preview": verify_response[:200],
        })
        status = "OK" if coherent else "DEGRADED"
        print(f"\n  RESULT: {status} (response: {len(verify_response)} chars)")

    # Phase 3: Report
    print(f"\n\n{'=' * 60}")
    print("STRESS TEST RESULTS")
    print(f"{'=' * 60}")
    print(f"Cycles attempted: {len(results)}")
    print(f"session/load succeeded: {sum(1 for r in results if r['load_ok'])}/{len(results)}")
    print(f"Coherent after restart: {sum(1 for r in results if r.get('coherent'))}/{len(results)}")
    print()
    for r in results:
        status = "OK" if r.get("coherent") else ("LOAD_FAIL" if not r["load_ok"] else "INCOHERENT")
        preview = r.get("response_preview", r.get("error", ""))[:100]
        print(f"  Cycle {r['cycle']}: {status} | {preview}")

    # Save results
    results_path = os.path.join(TEST_CWD, "kill_restart_results.json")
    with open(results_path, "w") as f:
        json.dump({"session_id": session_id, "cycles": args.cycles, "results": results}, f, indent=2)
    print(f"\nResults saved: {results_path}")

    proc.terminate()
    print("[done]")


if __name__ == "__main__":
    asyncio.run(main())
