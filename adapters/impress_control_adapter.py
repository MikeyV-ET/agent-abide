#!/usr/bin/env python3
"""
Impress Control Adapter — Controls LibreOffice Impress via UNO socket.

Part of the MikeyV ASDAAAS adapter system. Holds a persistent UNO socket
connection to LibreOffice Impress. Receives commands via hub outbox,
executes them against the live presentation, returns results as inline
doorbells routed back through the hub.

Commands:
  get_state      - Current slide number, total slides, shape count
  next_slide     - Advance one slide
  prev_slide     - Go back one slide
  goto_slide     - Go to specific slide (1-indexed in params)
  read_slide     - Read all text from current or specified slide
  set_text       - Set text on a specific shape
  char_edit      - Character-by-character animated edit (blocking)
  create_shape   - Create a new text shape on a slide
  clear_slide    - Remove all shapes from a slide
  clean_editor   - Hide toolbars/sidebar for clean presentation view
  show_editor    - Restore toolbars/sidebar
  zoom           - Get or set zoom level (percent)
  status         - Adapter health check
  ping           - Liveness check

Usage:
  python3 impress_control_adapter.py
  python3 impress_control_adapter.py --port 2002 --poll-interval 0.25
  python3 impress_control_adapter.py --test  # self-test against live Impress

Sending a command (from any agent via hub):
  adapter_api.write_message(
      to="impress",
      text='{"action": "next_slide"}',
      adapter="orchestration",
      sender="Jr"
  )

Requires:
  - LibreOffice Impress running with UNO socket:
    SAL_USE_VCLPLUGIN=gtk3 GDK_BACKEND=x11 soffice --impress --norestore \\
      --accept="socket,host=localhost,port=2002;urp;" <file.pptx>
  - System python3 with UNO bindings (import uno)

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
from pathlib import Path
import tempfile

# Add comms dir to path for adapter_api
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))
import adapter_api

# UNO imports
try:
    import uno
    from com.sun.star.awt import Size as UnoSize, Point as UnoPoint
except ImportError:
    print("ERROR: UNO bindings not available.")
    print("Use system python3 (not venv). LibreOffice must be installed.")
    sys.exit(1)

# ============================================================================
# CONFIGURATION
# ============================================================================

ADAPTER_NAME = "impress"
ADAPTER_CAPABILITIES = [
    "get_state", "next_slide", "prev_slide", "goto_slide",
    "read_slide", "set_text", "char_edit", "create_shape",
    "clear_slide", "clean_editor", "show_editor", "zoom",
    "status", "ping",
]
ADAPTER_CONFIG = {
    "description": "LibreOffice Impress control via UNO socket",
    "type": "control",
    "doorbell_priority": 1,
    "default_timeout": 10,
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


# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("impress")


# ============================================================================
# UNO CONNECTION
# ============================================================================

class ImpressConnection:
    """Persistent UNO connection to LibreOffice Impress.

    Holds references to the remote ServiceManager, document, controller,
    and dispatcher. Supports reconnect on connection loss.

    Sidebar state is tracked manually (not detectable via UNO API —
    confirmed by gap testing Session 15).
    """

    def __init__(self, host="localhost", port=2002):
        self.host = host
        self.port = port
        self.smgr = None
        self.desktop = None
        self.doc = None
        self.controller = None
        self.dispatcher = None
        self.connected = False
        self._sidebar_closed = False

    def connect(self):
        """Establish UNO connection to Impress."""
        local_ctx = uno.getComponentContext()
        resolver = local_ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", local_ctx
        )
        url = (
            f"uno:socket,host={self.host},port={self.port}"
            ";urp;StarOffice.ComponentContext"
        )
        ctx = resolver.resolve(url)
        self.smgr = ctx.ServiceManager
        self.desktop = self.smgr.createInstanceWithContext(
            "com.sun.star.frame.Desktop", ctx
        )
        self.doc = self.desktop.getCurrentComponent()
        if self.doc is None:
            raise ConnectionError("Impress running but no document loaded")
        if not self.doc.supportsService(
            "com.sun.star.presentation.PresentationDocument"
        ):
            raise ConnectionError("Current document is not a presentation")
        self.controller = self.doc.getCurrentController()
        self.dispatcher = self.smgr.createInstanceWithContext(
            "com.sun.star.frame.DispatchHelper", ctx
        )
        self.connected = True
        log.info("UNO connected to %s:%d", self.host, self.port)

    def reconnect(self, max_attempts=3, delay=2.0):
        """Reconnect after connection loss. Returns True on success."""
        self.connected = False
        for attempt in range(1, max_attempts + 1):
            try:
                log.info("Reconnect attempt %d/%d...", attempt, max_attempts)
                self.connect()
                return True
            except Exception as e:
                log.warning("Attempt %d failed: %s", attempt, e)
                if attempt < max_attempts:
                    time.sleep(delay)
        log.error("All %d reconnect attempts failed", max_attempts)
        return False

    def ensure(self):
        """Ensure connection is alive. Reconnect if needed. Raises on failure."""
        if not self.connected:
            if not self.reconnect():
                raise ConnectionError("Cannot connect to Impress UNO socket")
        # Quick health check — try to read slide count
        try:
            _ = self.doc.getDrawPages().getCount()
        except Exception:
            self.connected = False
            if not self.reconnect():
                raise ConnectionError("UNO connection lost, reconnect failed")

    def get_current_slide_index(self):
        """Get 0-based index of current slide. Returns -1 on failure."""
        current = self.controller.getCurrentPage()
        pages = self.doc.getDrawPages()
        for i in range(pages.getCount()):
            if pages.getByIndex(i) == current:
                return i
        return -1

    def get_slide_count(self):
        """Get total number of slides."""
        return self.doc.getDrawPages().getCount()


# Global connection instance (set in main)
conn = None


# ============================================================================
# COMMAND HANDLERS
#
# Each handler takes a `params` dict and returns a result dict with:
#   status:  "ok" or "error"
#   summary: Short doorbell-ready string (under 256 bytes)
#   data:    Structured result data (optional)
#   error:   Error message (on failure)
# ============================================================================

def cmd_get_state(params):
    """Get current presentation state."""
    conn.ensure()
    idx = conn.get_current_slide_index()
    total = conn.get_slide_count()
    slide = conn.doc.getDrawPages().getByIndex(idx)
    shape_count = slide.getCount()
    return {
        "status": "ok",
        "summary": f"slide {idx + 1} of {total}, {shape_count} shapes",
        "data": {
            "slide": idx + 1,
            "slide_index": idx,
            "total": total,
            "shape_count": shape_count,
            "sidebar_closed": conn._sidebar_closed,
        },
    }


def cmd_next_slide(params):
    """Advance to next slide."""
    conn.ensure()
    idx = conn.get_current_slide_index()
    total = conn.get_slide_count()
    if idx >= total - 1:
        return {
            "status": "error",
            "summary": f"already on last slide ({total})",
            "error": f"Already on last slide (slide {total} of {total})",
        }
    target = idx + 1
    conn.controller.setCurrentPage(conn.doc.getDrawPages().getByIndex(target))
    return {
        "status": "ok",
        "summary": f"slide {target + 1} of {total}",
        "data": {"slide": target + 1, "total": total},
    }


def cmd_prev_slide(params):
    """Go to previous slide."""
    conn.ensure()
    idx = conn.get_current_slide_index()
    total = conn.get_slide_count()
    if idx <= 0:
        return {
            "status": "error",
            "summary": "already on first slide",
            "error": "Already on first slide",
        }
    target = idx - 1
    conn.controller.setCurrentPage(conn.doc.getDrawPages().getByIndex(target))
    return {
        "status": "ok",
        "summary": f"slide {target + 1} of {total}",
        "data": {"slide": target + 1, "total": total},
    }


def cmd_goto_slide(params):
    """Go to specific slide. Params: slide (1-indexed)."""
    conn.ensure()
    slide_num = params.get("slide", params.get("n"))
    if slide_num is None:
        return {
            "status": "error",
            "summary": "missing 'slide' param",
            "error": "Missing 'slide' parameter (1-indexed)",
        }
    slide_num = int(slide_num)
    total = conn.get_slide_count()
    idx = slide_num - 1
    if idx < 0 or idx >= total:
        return {
            "status": "error",
            "summary": f"slide {slide_num} out of range (1-{total})",
            "error": f"Slide {slide_num} out of range (total: {total})",
        }
    conn.controller.setCurrentPage(conn.doc.getDrawPages().getByIndex(idx))
    return {
        "status": "ok",
        "summary": f"slide {slide_num} of {total}",
        "data": {"slide": slide_num, "total": total},
    }


def cmd_read_slide(params):
    """Read text from slide. Params: slide (1-indexed, default current)."""
    conn.ensure()
    slide_num = params.get("slide")
    if slide_num is None:
        idx = conn.get_current_slide_index()
    else:
        idx = int(slide_num) - 1
    total = conn.get_slide_count()
    if idx < 0 or idx >= total:
        return {
            "status": "error",
            "summary": f"slide out of range",
            "error": f"Slide index {idx} out of range (total: {total})",
        }
    slide = conn.doc.getDrawPages().getByIndex(idx)
    shapes = []
    for i in range(slide.getCount()):
        shape = slide.getByIndex(i)
        if shape.supportsService("com.sun.star.drawing.Text"):
            shapes.append({
                "index": i,
                "type": shape.ShapeType,
                "text": shape.getString(),
            })
    text_preview = "; ".join(
        s["text"][:50] for s in shapes if s["text"]
    )
    return {
        "status": "ok",
        "summary": f"slide {idx + 1}: {len(shapes)} text shapes",
        "data": {
            "slide": idx + 1,
            "shapes": shapes,
            "text_preview": text_preview,
        },
    }


def cmd_set_text(params):
    """Set text on a shape. Params: slide (1-indexed), shape (0-indexed), text."""
    conn.ensure()
    slide_num = params.get("slide")
    shape_idx = int(params.get("shape", 0))
    new_text = params.get("text", "")

    if slide_num is None:
        idx = conn.get_current_slide_index()
    else:
        idx = int(slide_num) - 1

    slide = conn.doc.getDrawPages().getByIndex(idx)
    if shape_idx >= slide.getCount():
        return {
            "status": "error",
            "summary": f"shape {shape_idx} not found on slide {idx + 1}",
            "error": (
                f"Shape index {shape_idx} out of range "
                f"(slide has {slide.getCount()} shapes)"
            ),
        }
    shape = slide.getByIndex(shape_idx)
    old_text = shape.getString()
    shape.getText().setString(new_text)

    # Verify
    actual = shape.getString()
    if actual != new_text:
        return {
            "status": "error",
            "summary": "text verification failed",
            "error": f"Expected '{new_text}', got '{actual}'",
        }
    return {
        "status": "ok",
        "summary": f"text set on slide {idx + 1} shape {shape_idx}",
        "data": {
            "slide": idx + 1,
            "shape": shape_idx,
            "old_text": old_text,
            "new_text": new_text,
        },
    }


def cmd_char_edit(params):
    """Character-by-character edit for live demo effect.

    Params:
        slide:        1-indexed (default current)
        shape:        0-indexed (default 0)
        from_text:    Text to delete char by char
        to_text:      Text to type char by char
        delete_delay: Seconds between delete keystrokes (default 1.2)
        type_delay:   Seconds between type keystrokes (default 0.6)

    NOTE: This is a BLOCKING operation. Total time =
        len(from_text) * delete_delay + len(to_text) * type_delay
    """
    conn.ensure()
    slide_num = params.get("slide")
    shape_idx = int(params.get("shape", 0))
    from_text = params.get("from_text", "")
    to_text = params.get("to_text", "")
    delete_delay = float(params.get("delete_delay", 1.2))
    type_delay = float(params.get("type_delay", 0.6))

    if slide_num is None:
        idx = conn.get_current_slide_index()
    else:
        idx = int(slide_num) - 1

    slide = conn.doc.getDrawPages().getByIndex(idx)
    shape = slide.getByIndex(shape_idx)
    text_obj = shape.getText()

    # Delete phase: remove from_text character by character
    current = from_text
    for i in range(len(current), 0, -1):
        current = current[: i - 1]
        text_obj.setString(current)
        time.sleep(delete_delay)

    # Type phase: build to_text character by character
    for i in range(1, len(to_text) + 1):
        text_obj.setString(to_text[:i])
        time.sleep(type_delay)

    actual = shape.getString()
    return {
        "status": "ok" if actual == to_text else "error",
        "summary": f"edited: '{from_text}' -> '{to_text}'",
        "data": {
            "slide": idx + 1,
            "shape": shape_idx,
            "from_text": from_text,
            "to_text": to_text,
            "actual": actual,
        },
    }


def cmd_create_shape(params):
    """Create a new text shape on a slide.

    Params:
        slide:     1-indexed (default current)
        text:      Text content
        x, y:      Position in 1/100mm (defaults: 1000, 12000)
        width, height: Size in 1/100mm (defaults: 24000, 5000)
        font_size: Point size (default 94)
        bold:      Boolean (default True)
        centered:  Boolean (default True)
    """
    conn.ensure()
    slide_num = params.get("slide")
    text = params.get("text", "")
    x = int(params.get("x", 1000))
    y = int(params.get("y", 12000))
    width = int(params.get("width", 24000))
    height = int(params.get("height", 5000))
    font_size = float(params.get("font_size", 94))
    bold = params.get("bold", True)
    centered = params.get("centered", True)

    if slide_num is None:
        idx = conn.get_current_slide_index()
    else:
        idx = int(slide_num) - 1

    slide = conn.doc.getDrawPages().getByIndex(idx)
    shape = conn.doc.createInstance("com.sun.star.drawing.TextShape")
    shape.Size = UnoSize(width, height)
    shape.Position = UnoPoint(x, y)
    slide.add(shape)

    text_obj = shape.getText()
    cursor = text_obj.createTextCursor()
    cursor.CharHeight = font_size
    if bold:
        cursor.CharWeight = 150  # com.sun.star.awt.FontWeight.BOLD
    if centered:
        cursor.ParaAdjust = 3  # CENTER
        shape.TextVerticalAdjust = 1  # CENTER
    text_obj.insertString(cursor, text, False)

    shape_index = slide.getCount() - 1
    return {
        "status": "ok",
        "summary": f"shape created on slide {idx + 1} (index {shape_index})",
        "data": {
            "slide": idx + 1,
            "shape_index": shape_index,
            "text": text,
        },
    }


def cmd_clear_slide(params):
    """Remove all shapes from a slide. Params: slide (1-indexed, default current).

    Uses slide.remove(shape) — confirmed correct by gap testing (not removeByIndex).
    """
    conn.ensure()
    slide_num = params.get("slide")
    if slide_num is None:
        idx = conn.get_current_slide_index()
    else:
        idx = int(slide_num) - 1

    slide = conn.doc.getDrawPages().getByIndex(idx)
    count = slide.getCount()
    # Remove in reverse order to avoid index shifting
    for i in range(count - 1, -1, -1):
        shape = slide.getByIndex(i)
        slide.remove(shape)

    return {
        "status": "ok",
        "summary": f"cleared slide {idx + 1} ({count} shapes removed)",
        "data": {"slide": idx + 1, "shapes_removed": count},
    }


def cmd_clean_editor(params):
    """Hide toolbars and sidebar for clean presentation view.

    Sidebar state tracked manually (not detectable via UNO API —
    gap test Session 15 confirmed).
    """
    conn.ensure()
    frame = conn.controller.getFrame()
    layout = frame.LayoutManager

    hidden = []
    for toolbar in [
        "private:resource/toolbar/standardbar",
        "private:resource/toolbar/toolbar",
        "private:resource/toolbar/commontaskbar",
    ]:
        try:
            if layout.isElementVisible(toolbar):
                layout.hideElement(toolbar)
                hidden.append(toolbar)
        except Exception:
            pass

    # Close sidebar (toggle — track state ourselves)
    if not conn._sidebar_closed:
        conn.dispatcher.executeDispatch(frame, ".uno:Sidebar", "", 0, ())
        conn._sidebar_closed = True
        hidden.append("sidebar")

    return {
        "status": "ok",
        "summary": f"editor cleaned ({len(hidden)} elements hidden)",
        "data": {"hidden": hidden},
    }


def cmd_show_editor(params):
    """Show toolbars and sidebar."""
    conn.ensure()
    frame = conn.controller.getFrame()
    layout = frame.LayoutManager

    shown = []
    for toolbar in [
        "private:resource/toolbar/standardbar",
        "private:resource/toolbar/toolbar",
        "private:resource/toolbar/commontaskbar",
    ]:
        try:
            if not layout.isElementVisible(toolbar):
                layout.showElement(toolbar)
                shown.append(toolbar)
        except Exception:
            pass

    if conn._sidebar_closed:
        conn.dispatcher.executeDispatch(frame, ".uno:Sidebar", "", 0, ())
        conn._sidebar_closed = False
        shown.append("sidebar")

    return {
        "status": "ok",
        "summary": f"editor restored ({len(shown)} elements shown)",
        "data": {"shown": shown},
    }


def cmd_zoom(params):
    """Get or set zoom level. Params: value (percent). Omit for read-only."""
    conn.ensure()
    value = params.get("value", params.get("level", params.get("zoom")))
    if value is None:
        current = conn.controller.ZoomValue
        return {
            "status": "ok",
            "summary": f"zoom: {current}%",
            "data": {"zoom": current},
        }
    value = int(value)
    conn.controller.ZoomValue = value
    actual = conn.controller.ZoomValue
    return {
        "status": "ok",
        "summary": f"zoom: {actual}%",
        "data": {"zoom": actual, "requested": value},
    }


def cmd_status(params):
    """Adapter health check."""
    connected = False
    slide_info = None
    try:
        conn.ensure()
        connected = True
        slide_info = f"slide {conn.get_current_slide_index() + 1} of {conn.get_slide_count()}"
    except Exception:
        pass

    return {
        "status": "ok",
        "summary": (
            f"connected, {_command_count} cmds, {slide_info}"
            if connected
            else f"disconnected, {_command_count} cmds"
        ),
        "data": {
            "adapter": ADAPTER_NAME,
            "connected": connected,
            "uptime_s": int(time.time() - _start_time),
            "commands_handled": _command_count,
            "slide_info": slide_info,
        },
    }


# ============================================================================
# COMMAND DISPATCH
# ============================================================================

COMMANDS = {
    "get_state": cmd_get_state,
    "next_slide": cmd_next_slide,
    "prev_slide": cmd_prev_slide,
    "goto_slide": cmd_goto_slide,
    "read_slide": cmd_read_slide,
    "set_text": cmd_set_text,
    "char_edit": cmd_char_edit,
    "create_shape": cmd_create_shape,
    "clear_slide": cmd_clear_slide,
    "clean_editor": cmd_clean_editor,
    "show_editor": cmd_show_editor,
    "zoom": cmd_zoom,
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
            "NoConnectException",
            "DisposedException",
            "RuntimeException",
            "ConnectionError",
            "BrokenPipeError",
            "ConnectionResetError",
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
                "error": "UNO connection lost, all reconnect attempts failed",
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

    Scans ~/agents/<agent>/asdaaas/adapters/impress/inbox/ for each agent.
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
      [impress:<action>] <result>
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

    # Parse command — expect JSON with action + params
    try:
        cmd = json.loads(text)
        action = cmd.get("action", cmd.get("command", ""))
        params = cmd.get("params", {})
        # Support flattened format: {"action": "goto_slide", "slide": 3}
        if not params:
            params = {k: v for k, v in cmd.items() if k not in ("action", "command")}
    except (json.JSONDecodeError, AttributeError):
        # Plain text — treat as action name
        action = text.strip()
        params = {}

    log.info("CMD from %s: %s %s", sender, action, params or "")

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

    # Attempt initial UNO connection
    try:
        conn.connect()
        log.info("Initial UNO connection successful")
    except Exception as e:
        log.warning(
            "Initial UNO connection failed: %s (will retry on first command)", e
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
    log.info("Deregistered. Goodbye.")


def signal_handler(sig, frame):
    """Handle shutdown signals gracefully."""
    global _running
    log.info("Shutting down (signal %d)...", sig)
    _running = False


def main():
    global conn

    parser = argparse.ArgumentParser(
        description="MikeyV Impress Control Adapter"
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
        "--host",
        default="localhost",
        help="UNO socket host (default: localhost)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=2002,
        help="UNO socket port (default: 2002)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Self-test: connect to Impress and run get_state + read_slide",
    )
    args = parser.parse_args()

    conn = ImpressConnection(args.host, args.port)

    if args.test:
        # Self-test mode — no hub, just test UNO commands
        print("=" * 55)
        print("  Impress Control Adapter — Self-Test")
        print("=" * 55)
        try:
            conn.connect()
            print(f"  UNO: connected to {conn.host}:{conn.port}")

            result = cmd_get_state({})
            print(f"  State: {result['summary']}")

            result = cmd_read_slide({"slide": 1})
            print(f"  Slide 1: {result['summary']}")
            for s in result.get("data", {}).get("shapes", []):
                print(f"    [{s['index']}] {s['text'][:70]}")

            result = cmd_zoom({})
            print(f"  Zoom: {result['summary']}")

            print("\n  All commands: " + ", ".join(sorted(COMMANDS.keys())))
            print("=" * 55)
            print("  PASSED")
            print("=" * 55)
        except Exception as e:
            print(f"\n  FAILED: {e}")
            sys.exit(1)
        return

    # Normal adapter mode
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("=" * 55)
    print("  MikeyV Impress Control Adapter")
    print(f"  UNO: {args.host}:{args.port}")
    print(f"  Poll: {args.poll_interval}s | Heartbeat: {args.heartbeat_interval}s")
    print(f"  Commands: {', '.join(sorted(COMMANDS.keys()))}")
    print("=" * 55)

    run_adapter(
        poll_interval=args.poll_interval,
        heartbeat_interval=args.heartbeat_interval,
    )


if __name__ == "__main__":
    main()
