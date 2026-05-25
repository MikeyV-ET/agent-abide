"""
server.py — asdaaas HTTP API server.

Endpoints:
    GET  /agents                              — list all agents
    GET  /agents/{name}/status                — health, gaze, context
    GET  /agents/{name}/messages?last=N       — recent messages
    GET  /agents/{name}/messages?before=X&limit=N — paginated history
    WS   /agents/{name}/ws                    — live tail via WebSocket

Usage:
    python api/server.py                      — run on 0.0.0.0:8420
    ASDAAAS_CONFIG=/path/to/agents.json python api/server.py
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# Add parent dir so we can find agents.json via session_locator defaults
sys.path.insert(0, str(Path(__file__).parent))

from normalizers import read_messages, FileTailer
from session_locator import SessionLocator

app = FastAPI(title="asdaaas API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

locator = SessionLocator()


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/agents")
def list_agents():
    """List all configured agents with status."""
    return locator.list_agents()


@app.get("/agents/{name}/status")
def get_status(name: str):
    """Get agent status: health, gaze, awareness."""
    cfg = locator.agent_config(name)
    if cfg is None:
        raise HTTPException(404, f"Agent '{name}' not found")

    home = Path(cfg.get("home", ""))
    asdaaas_dir = home / "asdaaas"
    result = {"agent": name, "backend": cfg.get("backend", "grok")}

    # health.json
    health_path = asdaaas_dir / "health.json"
    if health_path.exists():
        with open(health_path) as f:
            result["health"] = json.load(f)

    # gaze.json
    gaze_path = asdaaas_dir / "gaze.json"
    if gaze_path.exists():
        with open(gaze_path) as f:
            result["gaze"] = json.load(f)

    # awareness.json
    awareness_path = asdaaas_dir / "awareness.json"
    if awareness_path.exists():
        with open(awareness_path) as f:
            result["awareness"] = json.load(f)

    return result


@app.get("/agents/{name}/messages")
def get_messages(
    name: str,
    last: Optional[int] = Query(None, ge=1, description="Return last N messages"),
    before: Optional[int] = Query(None, ge=0, description="Return messages before this id"),
    limit: Optional[int] = Query(None, ge=1, le=1000, description="Max messages (with before)"),
):
    """Get normalized messages for an agent."""
    cfg = locator.agent_config(name)
    if cfg is None:
        raise HTTPException(404, f"Agent '{name}' not found")

    session_path = locator.session_file(name)
    if session_path is None or not session_path.exists():
        raise HTTPException(404, f"No active session for agent '{name}'")

    backend = locator.agent_backend(name)
    messages = read_messages(
        session_path,
        backend,
        last=last or (50 if before is None else None),
        before=before,
        limit=limit,
    )

    return {
        "agent": name,
        "backend": backend,
        "count": len(messages),
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# WebSocket live tail
# ---------------------------------------------------------------------------

@app.websocket("/agents/{name}/ws")
async def websocket_tail(ws: WebSocket, name: str):
    """Stream new messages as they appear in the session file.

    Client connects, optionally sends {"after_id": N} to set starting point.
    Server polls for new messages and sends them as JSON arrays.
    """
    cfg = locator.agent_config(name)
    if cfg is None:
        await ws.close(code=4004, reason=f"Agent '{name}' not found")
        return

    session_path = locator.session_file(name)
    if session_path is None:
        await ws.close(code=4004, reason=f"No session for '{name}'")
        return

    await ws.accept()
    backend = locator.agent_backend(name)

    # Default: start from end of file (only see new messages)
    start_id = -1
    raw_mode = False

    # Check if client sends a starting point and/or raw mode
    try:
        init = await asyncio.wait_for(ws.receive_text(), timeout=1.0)
        data = json.loads(init)
        if "after_id" in data:
            start_id = data["after_id"]
        if data.get("raw"):
            raw_mode = True
    except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
        pass

    tailer = FileTailer(session_path, backend, start_id=start_id)

    try:
        while True:
            new_msgs = tailer.poll(raw=raw_mode)
            if new_msgs:
                await ws.send_text(json.dumps(new_msgs))
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8420, log_level="info")