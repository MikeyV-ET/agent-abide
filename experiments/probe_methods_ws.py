#!/usr/bin/env python3
"""Probe extension methods via WebSocket serve mode."""

import asyncio
import json
import os
import re
import time
import websockets

_rpc_id = 0

def rpc_request(method, params=None):
    global _rpc_id
    _rpc_id += 1
    msg = {"jsonrpc": "2.0", "method": method, "id": _rpc_id}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg), _rpc_id


async def ws_send_and_collect(ws, method, params=None, timeout=3.0):
    msg_str, msg_id = rpc_request(method, params)
    await ws.send(msg_str)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.3, deadline - time.monotonic()))
            frame = json.loads(raw)
            if frame.get("id") == msg_id:
                return frame
        except asyncio.TimeoutError:
            return None
        except websockets.exceptions.ConnectionClosed:
            return None
    return None


async def main():
    workdir = "/tmp/probe-ws-test"
    os.makedirs(workdir, exist_ok=True)
    port = 2422
    grok = os.path.expanduser("~/.grok/bin/grok")

    proc = await asyncio.create_subprocess_exec(
        grok, "agent", "--always-approve", "serve", "--bind", f"127.0.0.1:{port}",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
    )

    secret = None
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            line = await asyncio.wait_for(proc.stderr.readline(), timeout=1.0)
            text = line.decode("utf-8", errors="replace").strip()
            m = re.search(r'[Ss]ecret[:\s]+(\S+)', text)
            if m:
                secret = m.group(1)
                break
        except asyncio.TimeoutError:
            pass

    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    ws = await asyncio.wait_for(
        websockets.connect(f"ws://127.0.0.1:{port}/ws", additional_headers=headers),
        timeout=10.0
    )

    resp = await ws_send_and_collect(ws, "initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}, "terminal": True},
    }, timeout=10.0)

    resp = await ws_send_and_collect(ws, "session/new", {"cwd": workdir, "mcpServers": []}, timeout=15.0)
    sid = resp.get("result", {}).get("sessionId", "") if resp else ""
    print(f"Session: {sid}\n")

    methods = [
        ("x.ai/interject", {"sessionId": sid, "text": "t", "interjectionId": "t1"}),
        ("x.ai/queue/interject", {"sessionId": sid, "text": "t"}),
        ("x.ai/queue/remove", {"sessionId": sid, "entryId": "fake"}),
        ("x.ai/queue/clear", {"sessionId": sid}),
        ("x.ai/compact_conversation", {"sessionId": sid}),
        ("x.ai/prompt_history", {"sessionId": sid}),
        ("x.ai/rewind/points", {"sessionId": sid}),
        ("x.ai/follow_ups", {"sessionId": sid}),
        ("x.ai/toggle_plan_mode", {"sessionId": sid}),
        ("x.ai/btw", {"sessionId": sid, "text": "t"}),
        ("x.ai/scheduled_task_inject_prompt", {"sessionId": sid, "text": "t"}),
        ("x.ai/session/info", {"sessionId": sid}),
        ("x.ai/session/list", {}),
        ("x.ai/sessions/list", {}),
        ("x.ai/session/fork", {"sessionId": sid}),
        ("x.ai/fs/list", {"sessionId": sid, "path": workdir}),
        ("x.ai/fs/read_file", {"sessionId": sid, "path": f"{workdir}/x"}),
        ("x.ai/git/stage", {"sessionId": sid, "paths": []}),
        ("x.ai/search/content", {"sessionId": sid, "query": "test"}),
        ("x.ai/terminal/create", {"sessionId": sid}),
        ("x.ai/mcp/servers", {"sessionId": sid}),
        ("x.ai/memory/rewrite", {"sessionId": sid}),
        ("x.ai/skills/list", {"sessionId": sid}),
        ("x.ai/plugins/list", {"sessionId": sid}),
        ("x.ai/commands/list", {"sessionId": sid}),
        ("x.ai/settings/update", {}),
        ("x.ai/subagent/cancel", {"sessionId": sid, "taskId": "fake"}),
        ("x.ai/hunk-tracker/get-files", {"sessionId": sid}),
    ]

    accessible = []
    blocked = []

    for method, params in methods:
        resp = await ws_send_and_collect(ws, method, params, timeout=3.0)
        if resp is None:
            print(f"  {'TIMEOUT':40s}  {method}")
        elif resp.get("error", {}).get("code") == -32601:
            blocked.append(method)
            print(f"  {'-32601 NOT FOUND':40s}  {method}")
        elif "error" in resp:
            err = resp["error"]
            accessible.append(method)
            print(f"  {'ERROR ' + str(err.get('code','?')) + ': ' + err.get('message','')[:40]:40s}  {method}")
        else:
            accessible.append(method)
            print(f"  {'OK: ' + json.dumps(resp.get('result',{}))[:36]:40s}  {method}")

    print(f"\n{'='*60}")
    print(f"ACCESSIBLE ({len(accessible)}):")
    for m in accessible:
        print(f"  + {m}")
    print(f"\nBLOCKED -32601 ({len(blocked)}):")
    for m in blocked:
        print(f"  - {m}")

    await ws.close()
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        proc.kill()

if __name__ == "__main__":
    asyncio.run(main())
