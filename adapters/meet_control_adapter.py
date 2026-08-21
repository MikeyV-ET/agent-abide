#!/usr/bin/env python3
"""
Meet Control Adapter — Controls Google Meet via Chrome DevTools Protocol.

Part of the MikeyV ASDAAAS adapter system. Holds a persistent CDP WebSocket
connection to Chrome's Meet tab. Receives commands via hub outbox, executes
them against the live meeting, returns results as inline doorbells.

Primary control path: CDP JavaScript (Runtime.evaluate). Buttons remain in
DOM even when Meet's toolbar auto-hides — CDP bypasses this completely.

Audio path: edge-tts -> ffmpeg -> paplay -> virtual_mic -> Chrome source.

Commands:
  get_state        - Comprehensive state (mic, camera, call, meeting code)
  toggle_mic       - Toggle microphone via CDP JS click
  toggle_camera    - Toggle camera via CDP JS click
  get_mic_state    - Read mic muted state (data-is-muted attribute)
  get_camera_state - Read camera muted state
  leave_call       - Click Leave call button
  get_meeting_code - Read meeting code from URL
  speak            - TTS generation + playback through virtual_mic
  speak_file       - Play pre-generated audio file through virtual_mic
  route_audio      - Route Chrome source outputs to virtual_mic
  send_chat        - Type and send a chat message
  status           - Adapter health check
  ping             - Liveness check

Usage:
  python3 meet_control_adapter.py
  python3 meet_control_adapter.py --port 9222 --poll-interval 0.25
  python3 meet_control_adapter.py --test

Sending a command (from any agent via hub):
  adapter_api.write_message(
      to="meet",
      text='{"action": "toggle_mic"}',
      adapter="orchestration",
      sender="Jr"
  )

Requires:
  - Chrome running with CDP flags:
    DISPLAY=:0 google-chrome --no-sandbox --force-renderer-accessibility \\
      --remote-debugging-port=9222 --remote-allow-origins=* \\
      --user-data-dir=/tmp/chrome-cdp-profile "https://meet.google.com/..."
  - PulseAudio with virtual_mic null-sink
  - edge-tts, ffmpeg, paplay for speak command
  - websocket-client, requests python packages

Author: MikeyV-Jr
Date: 2026-03-26
"""

import json
import os
import sys
import time
import signal
import argparse
import logging
import subprocess
import tempfile
from pathlib import Path

# Add comms dir to path for adapter_api
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))
import adapter_api

# CDP dependencies
try:
    import websocket
    import requests
except ImportError as e:
    print(f"ERROR: Missing dependency: {e}")
    print("Install: pip install websocket-client requests")
    sys.exit(1)

# ============================================================================
# CONFIGURATION
# ============================================================================

ADAPTER_NAME = "meet"
ADAPTER_CAPABILITIES = [
    "get_state", "toggle_mic", "toggle_camera",
    "get_mic_state", "get_camera_state",
    "leave_call", "get_meeting_code",
    "speak", "speak_file", "route_audio",
    "send_chat", "check_waiting", "admit",
    "status", "ping",
]
ADAPTER_CONFIG = {
    "description": "Google Meet control via Chrome DevTools Protocol",
    "type": "control",
    "doorbell_priority": 1,
    "default_timeout": 30,  # Higher default: speak can take 5-10s
}

# ASDAAAS paths
try:
    from asdaaas_config import config
except ModuleNotFoundError:
    import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'core'))
    from asdaaas_config import config
HUB_DIR = config.hub_dir
AGENTS_DIR = HUB_DIR / "agents"  # legacy
AGENTS_HOME_DIR = config.agents_home

# TTS settings (proven Session 13)
TTS_VOICE = "en-US-SteffanNeural"
TTS_RATE = "+10%"
VIRTUAL_MIC_SINK = "virtual_mic"


def _get_pw_target():
    """Find PipeWire node ID for virtual_mic sink. Returns ID string or None."""
    try:
        result = subprocess.run(
            ["pw-cli", "list-objects"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            lines = result.stdout.split("\n")
            for i, line in enumerate(lines):
                if "node.name" in line and "virtual_mic" in line:
                    # Walk backwards to find the node ID
                    for j in range(i, max(i - 10, -1), -1):
                        if lines[j].strip().startswith("id "):
                            return lines[j].split(",")[0].replace("id ", "").strip()
    except Exception:
        pass
    return None


def _play_audio(wav_path, timeout=60):
    """Play a WAV file through virtual_mic. Tries pw-play first, falls back to paplay.
    Returns (success: bool, elapsed: float, error: str)."""
    t0 = time.time()

    # Try PipeWire first (pw-play --target=<node_id>)
    pw_target = _get_pw_target()
    if pw_target:
        result = subprocess.run(
            ["pw-play", f"--target={pw_target}", wav_path],
            capture_output=True, text=True, timeout=timeout,
        )
        elapsed = time.time() - t0
        if result.returncode == 0:
            return True, elapsed, ""
        log.warning("pw-play failed (rc=%d): %s — trying paplay", result.returncode, result.stderr[:100])

    # Fall back to PulseAudio (paplay --device=virtual_mic)
    t0 = time.time()
    result = subprocess.run(
        ["paplay", f"--device={VIRTUAL_MIC_SINK}", wav_path],
        capture_output=True, text=True, timeout=timeout,
    )
    elapsed = time.time() - t0
    if result.returncode == 0:
        return True, elapsed, ""

    return False, elapsed, result.stderr[:200]

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("meet")


# ============================================================================
# CDP CONNECTION
# ============================================================================

class CDPConnection:
    """Persistent Chrome DevTools Protocol connection via WebSocket.

    Connects to a specific Chrome tab (the Meet tab) and provides
    JavaScript evaluation. Handles tab discovery, reconnection, and
    CDP message correlation.
    """

    def __init__(self, port=9222):
        self.port = port
        self.ws = None
        self.tab_id = None
        self.tab_url = None
        self._msg_id = 0
        self.connected = False

    def connect(self):
        """Find Meet tab and connect via WebSocket."""
        resp = requests.get(
            f"http://localhost:{self.port}/json", timeout=5
        )
        tabs = resp.json()

        # Find the Meet tab
        meet_tab = None
        for t in tabs:
            if t.get("type") != "page":
                continue
            url = t.get("url", "")
            title = t.get("title", "")
            if "meet.google.com" in url or "Meet" in title:
                meet_tab = t
                break

        if not meet_tab:
            available = [
                f"{t.get('title', '?')[:40]} ({t.get('url', '?')[:40]})"
                for t in tabs
                if t.get("type") == "page"
            ]
            raise ConnectionError(
                f"Meet tab not found. Available tabs: {available}"
            )

        ws_url = meet_tab["webSocketDebuggerUrl"]
        self.ws = websocket.create_connection(ws_url, timeout=10)
        self.tab_id = meet_tab["id"]
        self.tab_url = meet_tab.get("url", "")
        self.connected = True
        log.info(
            "CDP connected to tab %s (%s)",
            self.tab_id[:8],
            meet_tab.get("title", "?")[:50],
        )

    def reconnect(self, max_attempts=3, delay=2.0):
        """Reconnect after connection loss. Returns True on success."""
        self.connected = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

        for attempt in range(1, max_attempts + 1):
            try:
                log.info("CDP reconnect attempt %d/%d...", attempt, max_attempts)
                self.connect()
                return True
            except Exception as e:
                log.warning("Attempt %d failed: %s", attempt, e)
                if attempt < max_attempts:
                    time.sleep(delay)
        log.error("All %d CDP reconnect attempts failed", max_attempts)
        return False

    def ensure(self):
        """Ensure connection is alive. Reconnect if needed."""
        if not self.connected or self.ws is None:
            if not self.reconnect():
                raise ConnectionError("Cannot connect to Chrome CDP")
        # Quick health check
        try:
            result = self.js_eval("return document.title;")
            if result is None:
                raise ConnectionError("Health check returned None")
        except Exception:
            self.connected = False
            if not self.reconnect():
                raise ConnectionError("CDP connection lost, reconnect failed")

    def send(self, method, params=None):
        """Send CDP command and wait for response. Skips async events."""
        self._msg_id += 1
        msg_id = self._msg_id
        msg = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params
        self.ws.send(json.dumps(msg))

        # Read responses, skip CDP events (which lack "id")
        max_reads = 100  # Safety limit
        for _ in range(max_reads):
            raw = self.ws.recv()
            resp = json.loads(raw)
            if resp.get("id") == msg_id:
                return resp
        raise TimeoutError(f"No response for CDP message {msg_id} after {max_reads} reads")

    def js_eval(self, expr):
        """Evaluate JavaScript expression in Meet tab.

        Wraps in IIFE to avoid const/let redeclaration errors across calls.
        Returns the JS return value, or None on error.
        """
        wrapped = f"(() => {{ {expr} }})()"
        r = self.send(
            "Runtime.evaluate",
            {"expression": wrapped, "returnByValue": True},
        )
        result = r.get("result", {}).get("result", {})
        if result.get("type") == "undefined":
            return None
        if "value" in result:
            return result["value"]
        # Check for exceptions
        exc = r.get("result", {}).get("exceptionDetails")
        if exc:
            log.warning("JS exception: %s", exc.get("text", str(exc)))
            return None
        return result.get("description")


# Global connection instance (set in main)
conn = None


# ============================================================================
# COMMAND HANDLERS
# ============================================================================

def cmd_get_state(params):
    """Get comprehensive Meet state: mic, camera, call status, meeting code."""
    conn.ensure()

    state_js = """
        const mic = document.querySelector('[aria-label*="microphone"]');
        const cam = document.querySelector('[aria-label*="camera"]');
        const leave = document.querySelector('[aria-label*="Leave call"]');
        const code = window.location.pathname.replace(/^\\//,'');
        return JSON.stringify({
            in_call: !!leave,
            mic_muted: mic ? mic.getAttribute('data-is-muted') === 'true' : null,
            mic_label: mic ? mic.getAttribute('aria-label') : null,
            camera_muted: cam ? cam.getAttribute('data-is-muted') === 'true' : null,
            camera_label: cam ? cam.getAttribute('aria-label') : null,
            meeting_code: code,
            title: document.title
        });
    """
    raw = conn.js_eval(state_js)
    if not raw:
        return {
            "status": "error",
            "summary": "could not read Meet state",
            "error": "JS eval returned None",
        }

    state = json.loads(raw)
    mic_str = "muted" if state["mic_muted"] else "on" if state["mic_muted"] is not None else "?"
    cam_str = "off" if state["camera_muted"] else "on" if state["camera_muted"] is not None else "?"
    call_str = "in call" if state["in_call"] else "not in call"

    return {
        "status": "ok",
        "summary": f"{call_str}, mic {mic_str}, cam {cam_str}",
        "data": state,
    }


def cmd_toggle_mic(params):
    """Toggle microphone via CDP JS click."""
    conn.ensure()
    raw = conn.js_eval("""
        const mic = document.querySelector('[aria-label*="microphone"]');
        if (!mic) return JSON.stringify({success: false, error: 'mic button not found'});
        const was_muted = mic.getAttribute('data-is-muted') === 'true';
        mic.click();
        // Brief delay then read new state
        return JSON.stringify({success: true, was_muted: was_muted});
    """)
    if not raw:
        return {"status": "error", "summary": "mic toggle failed", "error": "JS eval failed"}

    result = json.loads(raw)
    if not result.get("success"):
        return {"status": "error", "summary": result.get("error", "failed"), "error": result.get("error")}

    # Brief wait then verify new state
    time.sleep(0.3)
    new_state = conn.js_eval("""
        const mic = document.querySelector('[aria-label*="microphone"]');
        return mic ? mic.getAttribute('data-is-muted') : null;
    """)
    now_muted = new_state == "true" if new_state else None

    action = "muted" if now_muted else "unmuted"
    return {
        "status": "ok",
        "summary": f"mic {action}",
        "data": {
            "was_muted": result["was_muted"],
            "now_muted": now_muted,
        },
    }


def cmd_toggle_camera(params):
    """Toggle camera via CDP JS click."""
    conn.ensure()
    raw = conn.js_eval("""
        const cam = document.querySelector('[aria-label*="camera"]');
        if (!cam) return JSON.stringify({success: false, error: 'camera button not found'});
        const was_muted = cam.getAttribute('data-is-muted') === 'true';
        cam.click();
        return JSON.stringify({success: true, was_muted: was_muted});
    """)
    if not raw:
        return {"status": "error", "summary": "camera toggle failed", "error": "JS eval failed"}

    result = json.loads(raw)
    if not result.get("success"):
        return {"status": "error", "summary": result.get("error", "failed"), "error": result.get("error")}

    time.sleep(0.3)
    new_state = conn.js_eval("""
        const cam = document.querySelector('[aria-label*="camera"]');
        return cam ? cam.getAttribute('data-is-muted') : null;
    """)
    now_muted = new_state == "true" if new_state else None

    action = "off" if now_muted else "on"
    return {
        "status": "ok",
        "summary": f"camera {action}",
        "data": {"was_muted": result["was_muted"], "now_muted": now_muted},
    }


def cmd_get_mic_state(params):
    """Read microphone state without toggling."""
    conn.ensure()
    raw = conn.js_eval("""
        const mic = document.querySelector('[aria-label*="microphone"]');
        if (!mic) return JSON.stringify({state: 'not_found'});
        return JSON.stringify({
            state: mic.getAttribute('data-is-muted') === 'true' ? 'muted' : 'on',
            muted: mic.getAttribute('data-is-muted') === 'true',
            label: mic.getAttribute('aria-label')
        });
    """)
    if not raw:
        return {"status": "error", "summary": "could not read mic state", "error": "JS eval failed"}

    state = json.loads(raw)
    return {
        "status": "ok",
        "summary": f"mic {state['state']}",
        "data": state,
    }


def cmd_get_camera_state(params):
    """Read camera state without toggling."""
    conn.ensure()
    raw = conn.js_eval("""
        const cam = document.querySelector('[aria-label*="camera"]');
        if (!cam) return JSON.stringify({state: 'not_found'});
        return JSON.stringify({
            state: cam.getAttribute('data-is-muted') === 'true' ? 'off' : 'on',
            muted: cam.getAttribute('data-is-muted') === 'true',
            label: cam.getAttribute('aria-label')
        });
    """)
    if not raw:
        return {"status": "error", "summary": "could not read camera state", "error": "JS eval failed"}

    state = json.loads(raw)
    return {
        "status": "ok",
        "summary": f"camera {state['state']}",
        "data": state,
    }


def cmd_leave_call(params):
    """Leave the current Meet call via CDP JS click."""
    conn.ensure()
    raw = conn.js_eval("""
        const btn = document.querySelector('[aria-label*="Leave call"]');
        if (!btn) return JSON.stringify({success: false, error: 'Leave call button not found'});
        btn.click();
        return JSON.stringify({success: true});
    """)
    if not raw:
        return {"status": "error", "summary": "leave failed", "error": "JS eval failed"}

    result = json.loads(raw)
    if not result.get("success"):
        return {"status": "error", "summary": result.get("error", "failed"), "error": result.get("error")}

    return {
        "status": "ok",
        "summary": "left call",
        "data": {"left": True},
    }


def cmd_get_meeting_code(params):
    """Get the current meeting code from URL."""
    conn.ensure()
    code = conn.js_eval("return window.location.pathname.replace(/^\\//, '');")
    if not code:
        return {"status": "error", "summary": "no meeting code", "error": "Could not read URL"}

    return {
        "status": "ok",
        "summary": f"code: {code}",
        "data": {"code": code, "url": f"https://meet.google.com/{code}"},
    }


def cmd_speak(params):
    """Generate TTS audio and play through virtual_mic.

    Params:
        text:   Text to speak (required)
        voice:  TTS voice (default: en-US-SteffanNeural)
        rate:   Speech rate (default: +10%)

    NOTE: This is a BLOCKING operation. ~2-3s for generation + playback.
    For scripted demo lines, prefer speak_file with pre-generated audio.
    """
    text = params.get("text", "")
    if not text:
        return {"status": "error", "summary": "no text provided", "error": "Missing 'text' parameter"}

    voice = params.get("voice", TTS_VOICE)
    rate = params.get("rate", TTS_RATE)

    # Create temp files
    mp3_fd, mp3_path = tempfile.mkstemp(suffix=".mp3", prefix="tts_")
    os.close(mp3_fd)
    wav_path = mp3_path.replace(".mp3", ".wav")

    try:
        # Step 1: Generate TTS
        t0 = time.time()
        result = subprocess.run(
            ["edge-tts", "--voice", voice, "--rate", rate,
             "--text", text, "--write-media", mp3_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return {
                "status": "error",
                "summary": "TTS generation failed",
                "error": f"edge-tts error: {result.stderr[:200]}",
            }
        t_gen = time.time() - t0

        # Step 2: Convert MP3 -> WAV
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_path,
             "-ar", "44100", "-ac", "1", "-sample_fmt", "s16", wav_path],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return {
                "status": "error",
                "summary": "audio conversion failed",
                "error": f"ffmpeg error: {result.stderr[:200]}",
            }

        # Step 3: Play through virtual_mic (pw-play or paplay)
        ok, t_play, err = _play_audio(wav_path, timeout=60)
        if not ok:
            return {
                "status": "error",
                "summary": "audio playback failed",
                "error": f"playback error: {err}",
            }

        return {
            "status": "ok",
            "summary": f"spoke {len(text)} chars ({t_gen:.1f}s gen, {t_play:.1f}s play)",
            "data": {
                "text": text[:100],
                "voice": voice,
                "rate": rate,
                "gen_time": round(t_gen, 2),
                "play_time": round(t_play, 2),
            },
        }
    finally:
        # Cleanup temp files
        for f in (mp3_path, wav_path):
            try:
                os.unlink(f)
            except OSError:
                pass


def cmd_speak_file(params):
    """Play pre-generated audio file through virtual_mic.

    Params:
        file:  Path to WAV file (required)

    Much faster than speak — no TTS generation, just playback.
    Use for scripted demo narration.
    """
    wav_path = params.get("file", params.get("path", ""))
    if not wav_path:
        return {"status": "error", "summary": "no file specified", "error": "Missing 'file' parameter"}

    wav_path = os.path.expanduser(wav_path)
    if not os.path.exists(wav_path):
        return {
            "status": "error",
            "summary": f"file not found: {wav_path}",
            "error": f"Audio file not found: {wav_path}",
        }

    ok, elapsed, err = _play_audio(wav_path, timeout=120)
    if not ok:
        return {
            "status": "error",
            "summary": "playback failed",
            "error": f"playback error: {err}",
        }

    return {
        "status": "ok",
        "summary": f"played {os.path.basename(wav_path)} ({elapsed:.1f}s)",
        "data": {"file": wav_path, "play_time": round(elapsed, 2)},
    }


def cmd_route_audio(params):
    """Route Chrome source outputs to virtual_mic (Source 3).

    Call once after Chrome starts or reconnects to audio.
    Finds all Chrome source-outputs and moves them to source 3.
    """
    result = subprocess.run(
        ["pactl", "list", "source-outputs"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {"status": "error", "summary": "pactl failed", "error": result.stderr[:200]}

    # Parse source outputs to find Chrome entries
    chrome_ids = []
    current_id = None
    for line in result.stdout.split("\n"):
        if "Source Output #" in line:
            current_id = line.split("#")[1].strip()
        if current_id and "chrome" in line.lower():
            chrome_ids.append(current_id)
            current_id = None

    if not chrome_ids:
        return {
            "status": "ok",
            "summary": "no Chrome source outputs found",
            "data": {"moved": [], "note": "Chrome may not have an active audio stream yet"},
        }

    moved = []
    errors = []
    for sid in chrome_ids:
        r = subprocess.run(
            ["pactl", "move-source-output", sid, "3"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            moved.append(sid)
        else:
            errors.append(f"#{sid}: {r.stderr.strip()}")

    if errors:
        return {
            "status": "error",
            "summary": f"routed {len(moved)}/{len(chrome_ids)}, {len(errors)} errors",
            "error": "; ".join(errors),
            "data": {"moved": moved, "errors": errors},
        }

    return {
        "status": "ok",
        "summary": f"routed {len(moved)} Chrome outputs to virtual_mic",
        "data": {"moved": moved},
    }


def cmd_send_chat(params):
    """Send a chat message in Meet.

    Uses CDP to open chat panel, focus input, type via Input.insertText,
    and press Enter via Input.dispatchKeyEvent.

    Params:
        message:  Text to send (required)
    """
    message = params.get("message", params.get("text", ""))
    if not message:
        return {"status": "error", "summary": "no message", "error": "Missing 'message' parameter"}

    conn.ensure()

    # Step 1: Open chat panel if not open
    conn.js_eval("""
        const btn = document.querySelector('[aria-label*="Chat with everyone"]');
        if (btn) btn.click();
    """)
    time.sleep(1.0)

    # Step 2: Focus chat input
    focused = conn.js_eval("""
        const input = document.querySelector('[aria-label="Send a message to everyone"]')
            || document.querySelector('textarea[aria-label*="Send a message"]')
            || document.querySelector('[aria-label*="Send a message"]');
        if (!input) return 'not_found';
        input.focus();
        input.click();
        return 'focused';
    """)

    if focused == "not_found":
        return {
            "status": "error",
            "summary": "chat input not found",
            "error": "Could not find chat message input field",
        }

    time.sleep(0.3)

    # Step 3: Type message via CDP Input.insertText
    conn.send("Input.insertText", {"text": message})
    time.sleep(0.3)

    # Step 4: Press Enter to send
    for event_type in ("keyDown", "keyUp"):
        conn.send("Input.dispatchKeyEvent", {
            "type": event_type,
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13,
        })

    return {
        "status": "ok",
        "summary": f"sent: {message[:50]}",
        "data": {"message": message},
    }



def cmd_check_waiting(params):
    """Check if anyone is waiting to be admitted to the call."""
    conn.ensure()
    raw = conn.js_eval("""
        // Real admit buttons are <button> with aria-label="Admit [Name]"
        // or <button> with text "Admit all". The banner div "Admit N guest"
        // is just a notification link, not actionable for admitting.
        const btns = document.querySelectorAll('button');
        const waiting = [];
        let hasAdmitAll = false;
        btns.forEach(b => {
            const text = b.textContent.trim();
            const label = b.getAttribute('aria-label') || '';
            if (label.match(/^Admit\s+/i) && label !== '') {
                // Individual admit: aria-label="Admit Eric Terry"
                const name = label.replace(/^Admit\s+/i, '').trim();
                waiting.push({name: name, type: 'individual'});
            } else if (text === 'Admit all') {
                hasAdmitAll = true;
            }
        });
        // Also check "Waiting to be admitted" section for count
        const allEls = document.querySelectorAll('[role="button"]');
        let waitCount = 0;
        allEls.forEach(el => {
            const t = el.textContent.trim();
            const m = t.match(/Waiting to be admitted\s*(\d+)/i);
            if (m) waitCount = parseInt(m[1]);
        });
        const count = waiting.length || waitCount;
        return JSON.stringify({
            count: count,
            waiting: waiting,
            hasAdmitAll: hasAdmitAll
        });
    """)
    if not raw:
        return {"status": "error", "summary": "could not check waiting", "error": "JS eval failed"}

    data = json.loads(raw)
    count = data.get("count", 0)
    names = [w.get("name", "?") for w in data.get("waiting", [])]

    if count > 0:
        name_str = ", ".join(names) if names else f"{count} guest(s)"
        return {
            "status": "ok",
            "summary": f"{count} waiting ({name_str})",
            "data": data,
        }
    else:
        return {
            "status": "ok",
            "summary": "no one waiting",
            "data": {"count": 0, "waiting": []},
        }


def cmd_admit(params):
    """Admit waiting participant(s). Optional 'name' param to admit specific person."""
    conn.ensure()
    target_name = params.get("name") if params else None
    target_json = json.dumps(target_name) if target_name else "null"

    raw = conn.js_eval(f"""
        // Strategy: prefer "Admit all" button, or individual "Admit [Name]" buttons.
        // Only click real <button> elements, NOT the banner div notification.
        const btns = document.querySelectorAll('button');
        let admitted = 0;
        const details = [];
        const targetName = {target_json};
        
        // First pass: look for "Admit all" (if no specific target)
        if (!targetName) {{
            for (const b of btns) {{
                if (b.textContent.trim() === 'Admit all') {{
                    b.click();
                    admitted = -1; // flag: used admit all
                    details.push('Admit all');
                    break;
                }}
            }}
        }}
        
        // Second pass: individual admit buttons (aria-label="Admit [Name]")
        if (admitted === 0) {{
            btns.forEach(b => {{
                const label = b.getAttribute('aria-label') || '';
                const labelMatch = label.match(/^Admit\s+(.+)/i);
                if (labelMatch) {{
                    const name = labelMatch[1];
                    if (!targetName || name.toLowerCase().includes(targetName.toLowerCase())) {{
                        b.click();
                        admitted++;
                        details.push(name);
                    }}
                }}
            }});
        }}
        
        if (admitted === -1) admitted = details.length || 1;
        return JSON.stringify({{admitted: admitted, details: details}});
    """)
    if not raw:
        return {"status": "error", "summary": "admit failed", "error": "JS eval failed"}

    data = json.loads(raw)
    admitted = data.get("admitted", 0)
    details = data.get("details", [])

    if admitted > 0:
        detail_str = ", ".join(details) if details else f"{admitted} participant(s)"
        return {
            "status": "ok",
            "summary": f"admitted {detail_str}",
            "data": data,
        }
    else:
        return {
            "status": "ok",
            "summary": "no one waiting",
            "data": {"admitted": 0},
        }


def cmd_status(params):
    """Adapter health check."""
    connected = False
    call_info = None
    try:
        conn.ensure()
        connected = True
        raw = conn.js_eval("""
            const leave = document.querySelector('[aria-label*="Leave call"]');
            return JSON.stringify({
                in_call: !!leave,
                title: document.title,
                url: window.location.href
            });
        """)
        if raw:
            call_info = json.loads(raw)
    except Exception:
        pass

    return {
        "status": "ok",
        "summary": (
            f"connected, {_command_count} cmds"
            + (f", {'in call' if call_info and call_info.get('in_call') else 'no call'}" if call_info else "")
            if connected
            else f"disconnected, {_command_count} cmds"
        ),
        "data": {
            "adapter": ADAPTER_NAME,
            "connected": connected,
            "uptime_s": int(time.time() - _start_time),
            "commands_handled": _command_count,
            "call_info": call_info,
        },
    }


# ============================================================================
# COMMAND DISPATCH
# ============================================================================

COMMANDS = {
    "get_state": cmd_get_state,
    "toggle_mic": cmd_toggle_mic,
    "toggle_camera": cmd_toggle_camera,
    "get_mic_state": cmd_get_mic_state,
    "get_camera_state": cmd_get_camera_state,
    "leave_call": cmd_leave_call,
    "get_meeting_code": cmd_get_meeting_code,
    "speak": cmd_speak,
    "speak_file": cmd_speak_file,
    "route_audio": cmd_route_audio,
    "send_chat": cmd_send_chat,
    "check_waiting": cmd_check_waiting,
    "admit": cmd_admit,
    "status": cmd_status,
    "ping": lambda p: {"status": "ok", "summary": "pong", "data": {"pong": True}},
}


# ============================================================================
# ADAPTER CORE
# ============================================================================

_start_time = time.time()
_command_count = 0
_running = True


def execute_command(action, params):
    """Route action to handler. Handle connection errors with reconnect."""
    handler = COMMANDS.get(action)
    if handler is None:
        available = ", ".join(sorted(COMMANDS.keys()))
        return {
            "status": "error",
            "summary": f"unknown command '{action}'",
            "error": f"Unknown command: {action}. Available: {available}",
        }
    try:
        return handler(params)
    except Exception as e:
        etype = type(e).__name__
        # Connection-class errors: attempt reconnect and retry once
        if etype in (
            "WebSocketConnectionClosedException",
            "WebSocketTimeoutException",
            "ConnectionError",
            "ConnectionRefusedError",
            "BrokenPipeError",
            "ConnectionResetError",
            "TimeoutError",
        ):
            log.warning("Connection error in %s: %s — reconnecting", action, e)
            if conn.reconnect():
                try:
                    return handler(params)
                except Exception as e2:
                    return {
                        "status": "error",
                        "summary": f"failed after reconnect: {e2}",
                        "error": str(e2),
                    }
            return {
                "status": "error",
                "summary": "connection lost",
                "error": "CDP connection lost, all reconnect attempts failed",
            }
        # Non-connection errors
        log.error("Error in %s: %s", action, e, exc_info=True)
        return {
            "status": "error",
            "summary": f"error: {e}",
            "error": str(e),
        }


def poll_all_agent_inboxes():
    """Poll all agents' per-adapter inbox for commands.

    Scans ~/agents/<agent>/asdaaas/adapters/meet/inbox/ for each agent.
    Returns list of command dicts.
    """
    messages = []
    if not AGENTS_HOME_DIR.exists():
        return messages
    try:
        for agent_name in sorted(config.agents.keys()):
            inbox = (config.resolve_asdaaas_dir(agent_name, AGENTS_HOME_DIR) / "adapters" / ADAPTER_NAME / "inbox")
            if not inbox.exists():
                continue
            for f in sorted(inbox.glob("*.json")):
                try:
                    with open(f) as fh:
                        msg = json.load(fh)
                    if "from" not in msg:
                        msg["from"] = agent_name
                    messages.append(msg)
                    os.unlink(f)
                except (json.JSONDecodeError, OSError) as e:
                    log.warning("Inbox read error (%s): %s", f.name, e)
    except OSError as e:
        log.warning("Inbox scan error: %s", e)
    return messages


def write_doorbell(agent_name, doorbell_text, action="", priority=1, meta=None):
    """Write a doorbell notification to ASDAAAS doorbell directory.

    Path: ~/agents/<agent>/asdaaas/doorbells/<bell>.json
    ASDAAAS polls this and delivers to agent stdin as:
      [meet:<action>] <result>
    """
    bell_dir = (config.resolve_asdaaas_dir(agent_name, AGENTS_HOME_DIR) / "doorbells")
    bell_dir.mkdir(parents=True, exist_ok=True)

    bell = {
        "adapter": ADAPTER_NAME,
        "command": action,
        "priority": priority,
        "text": doorbell_text,
        "ts": time.time(),
    }
    if meta:
        bell.update(meta)

    fd, tmp_path = tempfile.mkstemp(dir=str(bell_dir), suffix=".tmp", prefix="bell_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(bell, f)
        final = tmp_path.replace(".tmp", ".json")
        os.rename(tmp_path, final)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def handle_command(msg):
    """Parse command from hub message, execute, write result back."""
    global _command_count
    _command_count += 1

    text = msg.get("text", "")
    sender = msg.get("from", "unknown")
    msg_id = msg.get("request_id", msg.get("id", "unknown"))
    meta = msg.get("meta", {})
    origin_adapter = meta.get("origin_adapter", "unknown")

    # Parse command
    try:
        cmd = json.loads(text)
        action = cmd.get("action", cmd.get("command", ""))
        params = cmd.get("params", {})
        # Support flattened format: {"action": "speak", "text": "hello"}
        if not params:
            params = {k: v for k, v in cmd.items() if k not in ("action", "command")}
    except (json.JSONDecodeError, AttributeError):
        action = text.strip()
        params = {}

    log.info("CMD from %s: %s %s", sender, action,
             {k: (v[:50] + "..." if isinstance(v, str) and len(v) > 50 else v)
              for k, v in params.items()} if params else "")

    result = execute_command(action, params)

    # Add standard metadata
    result["action"] = action
    result["adapter"] = ADAPTER_NAME
    result["ts"] = time.time()

    # Format doorbell for ASDAAAS inline display
    doorbell = (
        f"[{ADAPTER_NAME}:{action}] "
        f"{result.get('status', '?')}: {result.get('summary', '')}"
    )
    result["doorbell"] = doorbell

    # Write doorbell to ASDAAAS doorbell directory for the requesting agent
    write_doorbell(sender, doorbell, action=action, meta={
        "request_id": msg_id,
        "origin_adapter": origin_adapter,
    })

    log.info("-> %s: %s", sender, doorbell)
    return result


def run_adapter(poll_interval=0.25, heartbeat_interval=60.0):
    """Main loop: poll per-adapter inbox for commands, execute, write doorbells."""
    global _running

    # Attempt initial CDP connection
    try:
        conn.connect()
        log.info("Initial CDP connection successful")
    except Exception as e:
        log.warning(
            "Initial CDP connection failed: %s (will retry on first command)", e
        )

    # Register with hub
    adapter_api.register_adapter(
        name=ADAPTER_NAME,
        capabilities=ADAPTER_CAPABILITIES,
        config=ADAPTER_CONFIG,
    )
    log.info("Registered with hub. Polling every %.2fs", poll_interval)

    last_heartbeat = time.time()

    while _running:
        # Poll per-adapter inbox for commands from all agents
        commands = poll_all_agent_inboxes()
        for cmd in commands:
            handle_command(cmd)

        # Periodic heartbeat
        now = time.time()
        if now - last_heartbeat >= heartbeat_interval:
            adapter_api.update_heartbeat(ADAPTER_NAME)
            last_heartbeat = now

        time.sleep(poll_interval)

    # Clean shutdown
    adapter_api.deregister_adapter(ADAPTER_NAME)
    if conn and conn.ws:
        try:
            conn.ws.close()
        except Exception:
            pass
    log.info("Deregistered. Goodbye.")


def signal_handler(sig, frame):
    """Handle shutdown signals gracefully."""
    global _running
    log.info("Shutting down (signal %d)...", sig)
    _running = False


def main():
    global conn

    parser = argparse.ArgumentParser(
        description="MikeyV Meet Control Adapter"
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.25,
        help="Outbox poll interval in seconds (default: 0.25)",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=60.0,
        help="Heartbeat interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9222,
        help="Chrome CDP port (default: 9222)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Self-test: connect to Chrome and read Meet state",
    )
    args = parser.parse_args()

    conn = CDPConnection(args.port)

    if args.test:
        # Self-test mode
        print("=" * 55)
        print("  MikeyV Meet Control Adapter — Self-Test")
        print("=" * 55)
        try:
            conn.connect()
            print(f"  CDP: connected to tab {conn.tab_id[:12]}")
            print(f"  URL: {conn.tab_url[:60]}")

            result = cmd_get_state({})
            print(f"  State: {result['summary']}")
            data = result.get("data", {})
            print(f"    in_call: {data.get('in_call')}")
            print(f"    mic: {'muted' if data.get('mic_muted') else 'on'}")
            print(f"    camera: {'off' if data.get('camera_muted') else 'on'}")
            print(f"    code: {data.get('meeting_code', '?')}")

            result = cmd_get_mic_state({})
            print(f"  Mic: {result['summary']}")

            result = cmd_get_camera_state({})
            print(f"  Camera: {result['summary']}")

            print(f"\n  All commands: {', '.join(sorted(COMMANDS.keys()))}")
            print("=" * 55)
            print("  PASSED")
            print("=" * 55)
        except Exception as e:
            print(f"\n  FAILED: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
        return

    # Normal adapter mode
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("=" * 55)
    print("  MikeyV Meet Control Adapter")
    print(f"  CDP: localhost:{args.port}")
    print(f"  Poll: {args.poll_interval}s | Heartbeat: {args.heartbeat_interval}s")
    print(f"  Voice: {TTS_VOICE} ({TTS_RATE})")
    print(f"  Commands: {', '.join(sorted(COMMANDS.keys()))}")
    print("=" * 55)

    run_adapter(
        poll_interval=args.poll_interval,
        heartbeat_interval=args.heartbeat_interval,
    )


if __name__ == "__main__":
    main()
