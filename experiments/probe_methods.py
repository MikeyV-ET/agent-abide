#!/usr/bin/env python3
"""
Probe which x.ai/* extension methods are accessible from stdio client.
Sends each method and checks for -32601 (not registered) vs other responses.
"""

import asyncio
import json
import os
import time

_rpc_id = 0

def rpc_request(method, params=None):
    global _rpc_id
    _rpc_id += 1
    msg = {"jsonrpc": "2.0", "method": method, "id": _rpc_id}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg) + "\n", _rpc_id


async def read_frame(stdout):
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


async def send_and_collect(stdin, stdout, method, params=None, timeout=5.0):
    """Send request, collect response + any notifications."""
    msg_str, msg_id = rpc_request(method, params)
    stdin.write(msg_str.encode("utf-8"))
    await stdin.drain()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            frame = await asyncio.wait_for(
                read_frame(stdout),
                timeout=max(0.3, deadline - time.monotonic())
            )
        except asyncio.TimeoutError:
            return None, msg_id
        if frame is None:
            return None, msg_id
        if frame.get("id") == msg_id:
            return frame, msg_id
        # skip notifications
    return None, msg_id


async def main():
    workdir = "/tmp/probe-methods-test"
    os.makedirs(workdir, exist_ok=True)

    grok = os.path.expanduser("~/.grok/bin/grok")
    proc = await asyncio.create_subprocess_exec(
        grok, "agent", "--always-approve", "stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
        limit=10 * 1024 * 1024,
    )

    # Initialize
    resp, _ = await send_and_collect(proc.stdin, proc.stdout, "initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}, "terminal": True},
    })
    if not resp:
        print("ERROR: initialize failed")
        proc.terminate()
        return

    # Create session
    resp, _ = await send_and_collect(proc.stdin, proc.stdout, "session/new", {
        "cwd": workdir, "mcpServers": [],
    }, timeout=15.0)
    sid = resp.get("result", {}).get("sessionId", "") if resp else ""
    if not sid:
        print("ERROR: no session ID")
        proc.terminate()
        return
    print(f"Session: {sid}\n")

    # Methods to probe — representative from each category
    methods = [
        # Conversation / Queue
        ("x.ai/interject", {"sessionId": sid, "text": "test", "interjectionId": "test-1"}),
        ("x.ai/queue/interject", {"sessionId": sid, "text": "test"}),
        ("x.ai/queue/remove", {"sessionId": sid, "entryId": "fake"}),
        ("x.ai/queue/reorder", {"sessionId": sid}),
        ("x.ai/queue/clear", {"sessionId": sid}),
        ("x.ai/compact_conversation", {"sessionId": sid}),
        ("x.ai/prompt_history", {"sessionId": sid}),
        ("x.ai/rewind/points", {"sessionId": sid}),
        ("x.ai/follow_ups", {"sessionId": sid}),
        ("x.ai/toggle_plan_mode", {"sessionId": sid}),
        ("x.ai/btw", {"sessionId": sid, "text": "test"}),
        ("x.ai/scheduled_task_inject_prompt", {"sessionId": sid, "text": "test"}),

        # Session management
        ("x.ai/session/info", {"sessionId": sid}),
        ("x.ai/session/list", {}),
        ("x.ai/sessions/list", {}),
        ("x.ai/session/fork", {"sessionId": sid}),

        # Filesystem
        ("x.ai/fs/list", {"sessionId": sid, "path": workdir}),
        ("x.ai/fs/read_file", {"sessionId": sid, "path": f"{workdir}/nonexistent"}),

        # Git
        ("x.ai/git/stage", {"sessionId": sid, "paths": []}),

        # Search
        ("x.ai/search/content", {"sessionId": sid, "query": "test"}),

        # Terminal
        ("x.ai/terminal/create", {"sessionId": sid}),

        # MCP
        ("x.ai/mcp/servers", {"sessionId": sid}),

        # Memory
        ("x.ai/memory/rewrite", {"sessionId": sid}),
        ("x.ai/memory/flush", {"sessionId": sid}),

        # Skills/plugins
        ("x.ai/skills/list", {"sessionId": sid}),
        ("x.ai/plugins/list", {"sessionId": sid}),
        ("x.ai/commands/list", {"sessionId": sid}),

        # Settings
        ("x.ai/settings/update", {}),
        ("x.ai/yolo_mode_changed", {"sessionId": sid}),

        # Subagent
        ("x.ai/subagent/cancel", {"sessionId": sid, "taskId": "fake"}),

        # Other
        ("x.ai/debug/trigger", {"sessionId": sid}),

        # Hunk tracker
        ("x.ai/hunk-tracker/get-files", {"sessionId": sid}),

        # Also test conversation.queue namespace
        ("conversation.queue.interject", {"sessionId": sid, "text": "test"}),
        ("conversation.queue.add", {"sessionId": sid, "text": "test"}),
    ]

    accessible = []
    blocked = []
    timeout_methods = []

    for method, params in methods:
        resp, mid = await send_and_collect(proc.stdin, proc.stdout, method, params, timeout=3.0)
        if resp is None:
            timeout_methods.append(method)
            status = "TIMEOUT"
        elif resp.get("error", {}).get("code") == -32601:
            blocked.append(method)
            status = "-32601 NOT FOUND"
        elif "error" in resp:
            # Has the method but we gave bad params or something
            err = resp["error"]
            accessible.append(method)
            status = f"ERROR {err.get('code')}: {err.get('message', '')[:60]}"
        else:
            accessible.append(method)
            status = f"OK: {json.dumps(resp.get('result', {}))[:80]}"
        print(f"  {status:40s}  {method}")

    print(f"\n{'='*60}")
    print(f"ACCESSIBLE ({len(accessible)}):")
    for m in accessible:
        print(f"  + {m}")
    print(f"\nBLOCKED -32601 ({len(blocked)}):")
    for m in blocked:
        print(f"  - {m}")
    if timeout_methods:
        print(f"\nTIMEOUT ({len(timeout_methods)}):")
        for m in timeout_methods:
            print(f"  ? {m}")

    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
