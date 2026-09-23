"""
grok_backend.py -- GrokBackend: AgentBackend implementation for grok agent stdio.

Speaks the grok JSON-RPC 2.0 stdio protocol:
  - Launch: grok agent stdio [-m model]
  - Wire format: JSON-RPC 2.0 (one JSON object per line)
  - Session management: explicit create (session/new) / load (session/load)
  - Prompt: session/prompt RPC with prompt array (stdin pipe)
  - Output: tailed from updates.jsonl + events.jsonl (FileEventSource)
  - Speech: agent_message_chunk in updates.jsonl
  - Thoughts: agent_thought_chunk in updates.jsonl
  - Tool calls: tool_call / tool_call_update in updates.jsonl
  - Completion: turn_ended in events.jsonl
  - Token tracking: _meta.totalTokens in updates.jsonl frames
  - Compaction: /compact command via session/prompt
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, IO, Optional

from agent_backend import AgentBackend, ResponseResult, TurnCancelled
from stdout_catalog import StdoutWireRecorder

# Delay after turn_ended to let final updates.jsonl writes flush
POST_TURN_DRAIN_DELAY_S = 0.15


class FileEventSource:
    """Turn-window reader for updates.jsonl + events.jsonl.

    When a ``GrokNativeBus`` is attached (phase 2), collect shares the bus
    ear: ``read_new_lines`` pumps the bus and drains the collect buffer.
    Without a bus, falls back to private file handles seeked to EOF (legacy).
    """

    def __init__(self, session_dir: Path, bus=None):
        self._session_dir = Path(session_dir)
        self._updates_path = self._session_dir / "updates.jsonl"
        self._events_path = self._session_dir / "events.jsonl"
        self._updates_fp: Optional[IO] = None
        self._events_fp: Optional[IO] = None
        self._bus = bus

    def set_bus(self, bus) -> None:
        self._bus = bus

    def open(self, timeout: float = 30.0):
        """Attach collect window at current tip. Call BEFORE sending a prompt."""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._updates_path.exists() and self._events_path.exists():
                break
            time.sleep(0.5)
        for p in (self._updates_path, self._events_path):
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch()
        if self._bus is not None:
            self._bus.begin_collect_window()
            return
        self._updates_fp = open(self._updates_path, "r")
        self._events_fp = open(self._events_path, "r")
        self._updates_fp.seek(0, 2)
        self._events_fp.seek(0, 2)

    def read_new_lines(self) -> tuple[list[dict], list[dict]]:
        """Non-blocking read of new complete lines from both files."""
        if self._bus is not None:
            return self._bus.read_for_collect()
        updates = []
        if self._updates_fp:
            for line in self._updates_fp:
                line = line.strip()
                if line:
                    try:
                        updates.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        events = []
        if self._events_fp:
            for line in self._events_fp:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return updates, events

    def close(self):
        """Close file handles."""
        if self._updates_fp:
            self._updates_fp.close()
            self._updates_fp = None
        if self._events_fp:
            self._events_fp.close()
            self._events_fp = None


class GrokBackend(AgentBackend):
    """AgentBackend implementation for grok agent stdio (JSON-RPC 2.0)."""

    def __init__(self, grok_sessions_dir: Optional[Path] = None,
                 grok_binary: Optional[str] = None):
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._session_id: Optional[str] = None
        self._model_id: str = "unknown"
        self._total_tokens: int = 0
        self._compaction_event: Optional[dict] = None  # set by refresh_tokens when auto_compact_completed seen
        self._compaction_tokens_before: int = 0  # from event's tokens_before field
        self._compaction_tokens_after: int = 0   # from event's tokens_after field
        self._context_window: int = 200000
        self._last_activity_ts: float = 0.0  # epoch ts of most recent updates.jsonl frame
        self._pending_tool_calls: set[str] = set()  # toolCallIds with no completed update yet
        # AA history: this backend owns native→aa.stream→hot acquisition
        self._agent_home: Optional[Path] = None
        self._agent_name: Optional[str] = None
        self._hot_ingest: bool = False
        self._hot_sync_every_n_frames: int = 1  # sync after each non-empty batch
        self._hot_frames_since_sync: int = 0
        self._delivery_confirmed: bool = False  # set True when user_message_chunk received
        self._rpc_id: int = 0
        self._grok_sessions_dir = grok_sessions_dir or Path.home() / ".grok" / "sessions"
        self._grok_binary = grok_binary or "grok"
        self._file_source: Optional[FileEventSource] = None
        self._stdout_task: Optional[asyncio.Task] = None
        self._permission_handler: Optional[Callable] = None
        self._allowed_always: set[str] = set()  # tool kinds auto-approved
        self._permission_pending: bool = False  # set while awaiting mentor decision
        self._start_kwargs: dict = {}  # cached kwargs for cancel_and_restart
        self._observer = None  # InProcessObserver, set via set_observer()

    def set_observer(self, observer):
        """Set the in-process observer for stdout event forwarding."""
        self._observer = observer

    def _rpc_request(self, method: str, params: Optional[dict] = None) -> str:
        self._rpc_id += 1
        msg: dict = {"jsonrpc": "2.0", "method": method, "id": self._rpc_id}
        if params is not None:
            msg["params"] = params
        return json.dumps(msg) + "\n"

    @staticmethod
    def _rpc_notification(method: str, params: Optional[dict] = None) -> str:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        return json.dumps(msg) + "\n"

    async def _send(self, msg: str):
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("Grok backend not started")
        self._proc.stdin.write(msg.encode("utf-8"))
        await self._proc.stdin.drain()

    async def _read_frame(self, timeout: float = 60.0) -> Optional[dict]:
        if not self._proc or not self._proc.stdout:
            return None
        chunks = []
        try:
            async def _read():
                while True:
                    try:
                        chunk = await self._proc.stdout.readuntil(b'\n')
                        chunks.append(chunk)
                        break
                    except asyncio.LimitOverrunError as e:
                        chunk = await self._proc.stdout.read(e.consumed)
                        chunks.append(chunk)
                    except asyncio.IncompleteReadError as e:
                        if e.partial:
                            chunks.append(e.partial)
                        if not chunks:
                            return None
                        break

            await asyncio.wait_for(_read(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

        data = b"".join(chunks)
        if not data:
            return None
        return json.loads(data.decode("utf-8").strip())

    async def _wait_for_response(self, expected_id: int, timeout: float = 60.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            frame = await self._read_frame(timeout=remaining)
            if frame is None:
                raise RuntimeError("stdio process closed stdout")
            if frame.get("id") == expected_id:
                return frame
        raise TimeoutError(f"No response for id={expected_id} within {timeout}s")

    def set_permission_handler(self, handler: Callable):
        """Set async callback for tool permission requests.

        handler(params: dict) -> str: receives request_permission params,
        returns an option_id (e.g. "allow-once", "allow-always", "reject-once").
        """
        self._permission_handler = handler

    async def _process_stdout(self):
        """Read stdout, log frames (catalog+timeline), and handle interactive gates.

        This is the Y-channel tap: every line from the binary's stdout is
        recorded before any parsing, giving the observer visibility into the
        control channel (gates, permissions, session updates).

        Recording (2026-08-28): not a full raw dump. Atomic exemplars live in
        stdout_catalog.jsonl; stdout_log.jsonl is a timeline of
        {ts, ref} pointers. See core/stdout_catalog.py.

        Design constraints (from LSP proxy experience — see
        docs/Y_CHANNEL_LSP_LESSONS.md for full analysis):

        1. NEVER BLOCK THIS LOOP. All tap work (logging, parsing) must be
           non-blocking. Blocking here risks pipe buffer deadlock — both
           directions fill their OS buffers (~64KB), permanent hang.
        2. Y is a PROXY, not a tee. Gate auto-responses inject data on stdin;
           this method is an active participant in the protocol, not passive.
        3. Validate complete messages (newline-delimited) before forwarding.
        """
        recorder = None
        if hasattr(self, '_session_dir') and self._session_dir:
            try:
                recorder = StdoutWireRecorder(self._session_dir)
                if not recorder.active:
                    recorder = None
            except Exception:
                recorder = None
        try:
            buf = b""
            while self._proc and self._proc.stdout:
                data = await self._proc.stdout.read(65536)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    # Catalog+timeline before any parsing
                    if recorder is not None:
                        try:
                            recorder.record(
                                time.time(),
                                line.decode("utf-8", errors="replace"),
                            )
                        except Exception:
                            pass
                    try:
                        frame = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    method = frame.get("method", "")
                    if method == "session/request_permission":
                        await self._handle_permission_request(frame)
                    elif method == "_x.ai/exit_plan_mode":
                        await self._handle_plan_review(frame)
                    elif method == "_x.ai/ask_user_question":
                        await self._handle_ask_user(frame)
                    elif "id" in frame and method:
                        print(f"[grok_backend] unhandled request: method={method} id={frame['id']} params={json.dumps(frame.get('params', {}))[:300]}")
                    # Forward all stdout frames to observer for state tracking
                    if getattr(self, "_observer", None) and method:
                        try:
                            self._observer.process_stdout_event(frame)
                        except Exception:
                            pass
        except (asyncio.CancelledError, OSError):
            pass
        finally:
            if recorder is not None:
                try:
                    recorder.close()
                except Exception:
                    pass

    async def _handle_permission_request(self, frame: dict):
        """Handle a session/request_permission JSON-RPC request from the binary."""
        rpc_id = frame.get("id")
        params = frame.get("params", {})
        tool_call = params.get("toolCall", {})
        kind = tool_call.get("kind", "unknown")

        # Check allow_always cache
        if kind in self._allowed_always:
            option_id = "allow-always"
        elif self._permission_handler:
            self._permission_pending = True
            try:
                option_id = await self._permission_handler(params)
            finally:
                self._permission_pending = False
        else:
            # No handler -- auto-reject
            option_id = "reject-once"

        # Cache allow_always decisions
        if option_id == "allow-always" and kind != "unknown":
            self._allowed_always.add(kind)

        # Send response back to binary
        response = json.dumps({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "outcome": {"outcome": "selected", "optionId": option_id}
            }
        }) + "\n"
        await self._send(response)

    async def _handle_plan_review(self, frame: dict):
        """Auto-approve plan review requests.

        The binary sends _x.ai/exit_plan_mode when exit_plan_mode is called,
        blocking until the user approves/rejects. In headless mode (asdaaas),
        we auto-approve since no human is at the keyboard.
        """
        rpc_id = frame.get("id")
        params = frame.get("params", {})
        print(f"[grok_backend] plan review requested (id={rpc_id}, params={json.dumps(params)[:200]}) — auto-approving")
        response = json.dumps({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "outcome": {"outcome": "selected", "optionId": "approve"}
            }
        }) + "\n"
        await self._send(response)

    async def _handle_ask_user(self, frame: dict):
        """Auto-respond to ask_user_question requests.

        The binary sends _x.ai/ask_user_question when the model calls
        ask_user_question, blocking until the user selects an option.
        In headless mode, we auto-select the first option (typically
        the recommended one) and note it was auto-selected.
        """
        rpc_id = frame.get("id")
        params = frame.get("params", {})
        print(f"[grok_backend] ask_user_question requested (id={rpc_id}, params={json.dumps(params)[:300]}) — auto-selecting first option")
        response = json.dumps({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "outcome": {"outcome": "selected", "optionId": "approve"}
            }
        }) + "\n"
        await self._send(response)

    # ---- AgentBackend interface ----

    async def start(self, agent_cwd: str, model: Optional[str] = None,
                    session_id: Optional[str] = None, yolo: bool = True,
                    sandbox: Optional[str] = None,
                    allow_rules: Optional[list[str]] = None,
                    deny_rules: Optional[list[str]] = None,
                    permission_mode: Optional[str] = None,
                    reasoning_effort: Optional[str] = None,
                    interjection_enabled: bool = False,
                    agent_name: Optional[str] = None,
                    pid_namespace: bool = False) -> str:
        # Cache kwargs for cancel_and_restart
        self._start_kwargs = dict(model=model, yolo=yolo, sandbox=sandbox,
                                  allow_rules=allow_rules, deny_rules=deny_rules,
                                  permission_mode=permission_mode, reasoning_effort=reasoning_effort,
                                  interjection_enabled=interjection_enabled, agent_name=agent_name,
                                  pid_namespace=pid_namespace)
        # PID namespace isolation: agent can only see/signal its own processes
        cmd = []
        if pid_namespace:
            cmd = ["unshare", "--user", "--pid", "--fork", "--mount-proc", "--"]
        cmd.append(self._grok_binary)
        if sandbox:
            cmd.extend(["--sandbox", sandbox])
        if yolo and not permission_mode:
            permission_mode = "bypassPermissions"
        if permission_mode:
            cmd.extend(["--permission-mode", permission_mode])
        for rule in (allow_rules or []):
            cmd.extend(["--allow", rule])
        for rule in (deny_rules or []):
            cmd.extend(["--deny", rule])
        cmd.append("agent")
        if model:
            cmd.extend(["-m", model])
        if reasoning_effort:
            cmd.extend(["--reasoning-effort", reasoning_effort])
        cmd.append("stdio")

        # Process env: always export AGENT_HOME (resolved agent home = cwd).
        # Hooks (compaction, interjection) must not rebuild ~/agents/$NAME.
        proc_env = {
            **os.environ,
            "AGENT_HOME": agent_cwd,
            "ASDAAAS_DIR": str(Path(agent_cwd) / "asdaaas"),
        }
        if agent_name:
            proc_env["AGENT_NAME"] = agent_name
        if interjection_enabled and agent_name:
            hook_path = Path(__file__).parent / "interjection_hook.sh"
            if hook_path.exists():
                proc_env["BASH_ENV"] = str(hook_path)

        print(f"[asdaaas] Spawning grok binary...")
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=agent_cwd,
            env=proc_env,
        )

        # Initialize JSON-RPC
        print(f"[asdaaas] Initializing protocol...")
        await self._send(self._rpc_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "asdaaas", "version": "0.2"},
        }))
        await self._wait_for_response(self._rpc_id, timeout=30)
        await self._send(self._rpc_notification("notifications/initialized"))

        # Create or load session
        if session_id:
            print(f"[asdaaas] Loading session {session_id[:12]}...")
            await self._send(self._rpc_request("session/load", {
                "sessionId": session_id,
                "cwd": agent_cwd,
                "mcpServers": [],
            }))
        else:
            print(f"[asdaaas] Creating new session...")
            await self._send(self._rpc_request("session/new", {
                "cwd": agent_cwd,
                "mcpServers": [],
            }))

        resp = await self._wait_for_response(self._rpc_id, timeout=120)
        self._session_id = resp.get("result", {}).get("sessionId", session_id or "unknown")

        # Model: CLI/agents.json arg first; summary fills in if omitted
        self._model_id = model or "unknown"
        try:
            encoded_cwd = agent_cwd.replace("/", "%2F")
            summary_path = self._grok_sessions_dir / encoded_cwd / self._session_id / "summary.json"
            with open(summary_path) as f:
                summary = json.load(f)
            summary_model = summary.get("current_model_id", "") or ""
            if self._model_id == "unknown" and summary_model:
                self._model_id = summary_model
            elif summary_model and not model:
                self._model_id = summary_model
        except (FileNotFoundError, json.JSONDecodeError, TypeError, AttributeError):
            pass

        print(f"[asdaaas] Session ready: {self._session_id[:12]}")

        # Open file event source for output reading
        encoded_cwd = agent_cwd.replace("/", "%2F")
        session_dir = self._grok_sessions_dir / encoded_cwd / self._session_id
        self._session_dir = session_dir

        # Phase 2: shared native bus (one updates.jsonl cursor → hot + collect)
        self._native_bus = None
        start_off = 0
        if getattr(self, "_hot_ingest", False) and getattr(self, "_agent_home", None):
            try:
                from hot_spine import measure_behind
                st = measure_behind(
                    agent_home=Path(self._agent_home),
                    agent=str(self._agent_name or "agent"),
                    native_path=session_dir / "updates.jsonl",
                )
                if st.ok:
                    start_off = int(st.checkpoint_offset or 0)
            except Exception:
                start_off = 0
            try:
                from grok_native_bus import GrokNativeBus
                bus = GrokNativeBus(
                    session_dir,
                    agent_home=self._agent_home,
                    agent_name=str(self._agent_name or "agent"),
                    session_id=self._session_id,
                    start_offset=start_off,
                )
                # Catch up hot through shared ear before collect window
                bus.catch_up()
                self._native_bus = bus
                print(
                    f"[grok_native_bus] up offset={bus.updates.offset} "
                    f"behind={bus.updates.behind()}"
                )
            except Exception as e:
                print(f"[grok_backend] native bus: {e}")
                self._native_bus = None

        self._file_source = FileEventSource(session_dir, bus=self._native_bus)
        self._file_source.open()
        self._seed_tokens_from_session()
        # Hot spine: always-on pump/reconcile (uses bus when present)
        if getattr(self, "_hot_ingest", False):
            try:
                from hot_spine import start_hot_spine_for_backend
                start_hot_spine_for_backend(self)
            except Exception as e:
                print(f"[grok_backend] hot_spine arm: {e}")

        # Process stdout in background to prevent pipe buffer from filling.
        # Also intercepts session/request_permission when yolo is off.
        self._stdout_task = asyncio.create_task(self._process_stdout())

        # Permission mode (yolo/bypassPermissions) is now set via CLI flag
        # --permission-mode bypassPermissions, no post-startup prompt needed.

        return self._session_id

    async def send_prompt(self, text: str) -> Any:
        """Send a prompt and confirm binary receipt via updates.jsonl.

        Writes the JSON-RPC session/prompt to stdin, then waits for the
        binary to echo it as user_message_chunk in updates.jsonl.  This
        closed-loop confirmation prevents duplicate deliveries — the binary
        processes prompts sequentially, so receipt confirmation naturally
        serializes sends even if a previous turn is still in progress.
        """
        self._delivery_confirmed = False
        await self._send(self._rpc_request("session/prompt", {
            "sessionId": self._session_id,
            "prompt": [{"type": "text", "text": text}],
        }))
        await self._wait_for_receipt()
        return self._rpc_id

    async def set_reasoning_effort(self, level: str) -> None:
        """Change reasoning effort in-place via session/set_model.

        Sends the current model ID with a new reasoningEffort in _meta.
        The binary sends a response and a model_changed notification on
        stdout, both consumed by _process_stdout (fire-and-forget).
        """
        await self._send(self._rpc_request("session/set_model", {
            "sessionId": self._session_id,
            "modelId": self._model_id,
            "_meta": {"reasoningEffort": level},
        }))

    async def _wait_for_receipt(self, timeout: float = 300.0):
        """Wait for user_message_chunk in updates.jsonl confirming receipt.

        The binary writes user_message_chunk when it dequeues a prompt from
        stdin.  Waiting for this frame ensures the binary has received and
        started processing the prompt before we return to the caller.

        Any frames consumed while waiting (leftover speech from a previous
        turn, _meta updates, compaction events) are processed for metadata
        tracking; speech content is discarded as it belongs to a prior turn.
        """
        if not self._file_source:
            return

        start = time.monotonic()
        deadline = start + timeout
        while time.monotonic() < deadline:
            updates, events = self._file_source.read_new_lines()

            for frame in updates:
                self._last_activity_ts = time.time()
                params = frame.get("params", {})
                update = params.get("update", {})
                su = update.get("sessionUpdate", "")

                if su == "user_message_chunk":
                    elapsed = time.monotonic() - start
                    if elapsed > 1.0:
                        print(f"[grok_backend] Receipt confirmed after {elapsed:.1f}s"
                              " (previous turn was still in progress)")
                    self._pending_tool_calls.clear()  # new turn — prior tools done
                    self._delivery_confirmed = True
                    # Push user turn into hot NOW so aa-dev TUI (hot-only) sees
                    # the submitted turn without waiting for first agent frames
                    # (prod tails updates and looks "immediate"; Eric 15s lag).
                    try:
                        self.sync_hot_stream()
                    except Exception as e:
                        print(f"[grok_backend] sync_hot on receipt: {e}")
                    w = getattr(self, "_updates_hot_watcher", None)
                    if w is not None:
                        try:
                            w.kick()
                        except Exception:
                            pass
                    return  # Receipt confirmed

                # Track tool calls from prior turn still visible in updates
                if su == "tool_call":
                    tool_id = update.get("toolCallId")
                    if tool_id:
                        self._pending_tool_calls.add(tool_id)
                elif su == "tool_call_update":
                    tool_id = update.get("toolCallId")
                    if tool_id and update.get("status") == "completed":
                        self._pending_tool_calls.discard(tool_id)

                # Track compaction events while waiting
                if su == "auto_compact_completed":
                    tokens_after = update.get("tokens_after")
                    tokens_before = update.get("tokens_before")
                    if tokens_before:
                        self._compaction_tokens_before = tokens_before
                    if tokens_after:
                        self._compaction_tokens_after = tokens_after
                        self._total_tokens = tokens_after
                    self._compaction_event = frame

                # Track tokens from _meta (skip after compaction, issue_0029)
                if not self._compaction_event:
                    meta = params.get("_meta", {})
                    if meta.get("totalTokens"):
                        self._total_tokens = meta["totalTokens"]

            # turn_ended from a prior turn may appear — just consume it
            await asyncio.sleep(0.05)

        self._delivery_confirmed = False
        print(f"[grok_backend] WARNING: receipt confirmation timed out after {timeout}s")

    async def collect_response(
        self,
        handle: Any,
        on_speech_chunk: Optional[Callable[[str], None]] = None,
        on_tool_call: Optional[Callable[[str], None]] = None,
        on_meta: Optional[Callable[[int], None]] = None,
        keepalive_timeout: float = 30.0,
        max_wall_clock: float = 600.0,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> ResponseResult:
        return await self._collect_from_files(
            on_speech_chunk=on_speech_chunk, on_tool_call=on_tool_call,
            on_meta=on_meta, keepalive_timeout=keepalive_timeout,
            max_wall_clock=max_wall_clock, cancel_event=cancel_event,
        )

    async def _collect_from_files(
        self,
        on_speech_chunk: Optional[Callable[[str], None]] = None,
        on_tool_call: Optional[Callable[[str], None]] = None,
        on_meta: Optional[Callable[[int], None]] = None,
        keepalive_timeout: float = 30.0,
        max_wall_clock: float = 600.0,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> ResponseResult:
        """Collect response by tailing updates.jsonl + events.jsonl.

        Content (speech, thoughts, tool calls) comes from updates.jsonl.
        Turn completion comes from turn_ended in events.jsonl.
        """
        if not self._file_source:
            raise RuntimeError("FileEventSource not initialized")

        speech_chunks: list[str] = []
        thought_chunks: list[str] = []
        stop_reason = ""
        pending_tool_calls: set[str] = set()
        last_activity = time.monotonic()
        wall_deadline = time.monotonic() + max_wall_clock

        while True:
            if cancel_event and cancel_event.is_set():
                raise TurnCancelled("cancel_event set during collect_response")

            now = time.monotonic()
            if now > wall_deadline:
                stop_reason = stop_reason or "wall_clock_timeout"
                break

            updates, events = self._file_source.read_new_lines()

            # Check for turn_ended in events.jsonl
            for ev in events:
                if ev.get("type") == "turn_ended":
                    stop_reason = ev.get("outcome", "completed")
                    # Brief drain to catch final updates.jsonl writes
                    await asyncio.sleep(POST_TURN_DRAIN_DELAY_S)
                    final_updates, _ = self._file_source.read_new_lines()
                    updates.extend(final_updates)
                    self._process_update_frames(
                        updates, speech_chunks, thought_chunks,
                        pending_tool_calls, on_speech_chunk, on_tool_call, on_meta,
                    )
                    # Second drain: speech/turn_completed sometimes land after
                    # turn_ended (Squiggy hot stuck on open tool_call).
                    await asyncio.sleep(POST_TURN_DRAIN_DELAY_S)
                    more, _ = self._file_source.read_new_lines()
                    if more:
                        self._process_update_frames(
                            more, speech_chunks, thought_chunks,
                            pending_tool_calls, on_speech_chunk, on_tool_call, on_meta,
                        )
                    self._final_hot_sync()
                    return ResponseResult(
                        speech="".join(speech_chunks),
                        thoughts="".join(thought_chunks),
                        total_tokens=self._total_tokens,
                        model_id=self._model_id,
                        stop_reason=stop_reason,
                    )

            if updates or events:
                last_activity = time.monotonic()

            self._process_update_frames(
                updates, speech_chunks, thought_chunks,
                pending_tool_calls, on_speech_chunk, on_tool_call, on_meta,
            )

            # Keepalive check — extend while permission OR tools are in flight.
            # Long shell/search_replace with no intermediate frames used to trip
            # 30s keepalive; collect returned empty while binary still wrote
            # tool_result + final speech + turn_completed → hot stuck mid-tool.
            tools_pending = bool(pending_tool_calls) or bool(
                getattr(self, "_pending_tool_calls", None)
            )
            if (
                time.monotonic() - last_activity > keepalive_timeout
                and not self._permission_pending
                and not tools_pending
            ):
                stop_reason = stop_reason or "keepalive_timeout"
                break

            await asyncio.sleep(0.05)

        self._final_hot_sync()
        return ResponseResult(
            speech="".join(speech_chunks),
            thoughts="".join(thought_chunks),
            total_tokens=self._total_tokens,
            model_id=self._model_id,
            stop_reason=stop_reason,
        )

    def _process_update_frames(
        self,
        frames: list[dict],
        speech_chunks: list[str],
        thought_chunks: list[str],
        pending_tool_calls: set[str],
        on_speech_chunk: Optional[Callable[[str], None]],
        on_tool_call: Optional[Callable[[str], None]],
        on_meta: Optional[Callable[[int], None]],
    ):
        """Process update frames from updates.jsonl."""
        for frame in frames:
            # Track wall-clock time of most recent frame for midturn detection.
            # Even when collect_response returns on wall_clock_timeout, this
            # timestamp shows the agent was still active.
            self._last_activity_ts = time.time()

            params = frame.get("params", {})
            update = params.get("update", {})
            su = update.get("sessionUpdate", "")

            if su == "agent_message_chunk":
                c = update.get("content", {})
                text = c.get("text", "") if isinstance(c, dict) else ""
                if text:
                    speech_chunks.append(text)
                    if on_speech_chunk:
                        on_speech_chunk(text)

            elif su == "agent_thought_chunk":
                c = update.get("content", {})
                text = c.get("text", "") if isinstance(c, dict) else ""
                if text:
                    thought_chunks.append(text)

            elif su == "tool_call":
                tool_id = update.get("toolCallId")
                if tool_id:
                    pending_tool_calls.add(tool_id)
                    self._pending_tool_calls.add(tool_id)
                if speech_chunks and not speech_chunks[-1].endswith("\n\n"):
                    speech_chunks.append("\n\n")
                if on_tool_call:
                    on_tool_call(update.get("title", ""))

            elif su == "tool_call_update":
                tool_id = update.get("toolCallId")
                if tool_id and update.get("status") == "completed":
                    pending_tool_calls.discard(tool_id)
                    self._pending_tool_calls.discard(tool_id)

            # Preserve compaction events — collect_response reads from the
            # same FileEventSource as refresh_tokens.  If the compaction event
            # lands during the post-turn drain, the file pointer advances past
            # it and refresh_tokens / pop_compaction_event never see it.
            elif su == "auto_compact_completed":
                tokens_after = update.get("tokens_after")
                tokens_before = update.get("tokens_before")
                if tokens_before:
                    self._compaction_tokens_before = tokens_before
                if tokens_after:
                    self._compaction_tokens_after = tokens_after
                    self._total_tokens = tokens_after
                self._compaction_event = frame

            # Token tracking from _meta — independent of sessionUpdate dispatch
            # (a frame can have BOTH a sessionUpdate AND _meta). Skip if
            # compaction event already set authoritative tokens_after (issue_0029).
            if not self._compaction_event:
                meta = params.get("_meta", {})
                if meta.get("totalTokens"):
                    self._total_tokens = meta["totalTokens"]
                    if on_meta:
                        on_meta(self._total_tokens)

        # Backend-owned aa.stream ingest (native updates → hot.jsonl)
        self._maybe_sync_hot_after_frames(len(frames))

    def _seed_tokens_from_session(self) -> None:
        """Prime _total_tokens from updates.jsonl after session/load.

        FileEventSource seeks to EOF so refresh_tokens() only sees *new*
        frames. After restart the first prompt would otherwise get
        total_tokens=0 and context_left_tag() returns empty (no telemetry).
        Scan the tail of updates.jsonl for the last known count.
        """
        if not self._session_dir:
            return
        path = self._session_dir / "updates.jsonl"
        try:
            if not path.exists() or path.stat().st_size == 0:
                return
            # Last ~1MB is enough; sessions can be huge
            max_bytes = 1_000_000
            with open(path, "rb") as f:
                size = path.stat().st_size
                f.seek(max(0, size - max_bytes))
                chunk = f.read().decode("utf-8", errors="replace")
            last = None
            for line in chunk.splitlines():
                if "totalTokens" not in line and "tokens_after" not in line:
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    continue
                params = frame.get("params") or {}
                update = params.get("update") or {}
                su = update.get("sessionUpdate", "")
                if su == "auto_compact_completed":
                    ta = update.get("tokens_after")
                    if ta:
                        last = int(ta)
                        continue
                meta = update.get("_meta") or params.get("_meta") or {}
                if isinstance(meta, dict) and meta.get("totalTokens"):
                    last = int(meta["totalTokens"])
                    continue
                # Some frames nest _meta on result
                result = frame.get("result") or {}
                rmeta = result.get("_meta") if isinstance(result, dict) else None
                if isinstance(rmeta, dict) and rmeta.get("totalTokens"):
                    last = int(rmeta["totalTokens"])
            if last and last > 0:
                self._total_tokens = last
                print(f"[grok_backend] Seeded token count from session: {last}")
        except Exception as e:
            print(f"[grok_backend] WARN: token seed failed: {e}")


    def configure_aa_history(
        self,
        agent_home: Path | str,
        agent_name: str,
        *,
        enabled: bool = True,
    ) -> None:
        """Enable AA hot.jsonl ingest owned by this backend.

        Acquisition: grok updates.jsonl (session dir or live finder).
        Normalization: stream_adapters.grok (map/wrap → aa.stream).
        Write: aa_stream.append_hot_events.
        """
        self._agent_home = Path(agent_home)
        self._agent_name = agent_name
        self._hot_ingest = bool(enabled)
        # Watcher/spine arm at session ready (start_hot_spine_for_backend) when
        # native path is known. Early start without session_dir was a no-op or
        # a lucky find_live_updates — spine owns the always-on path now.
        if not self._hot_ingest:
            old = getattr(self, "_hot_spine", None)
            if old is not None:
                try:
                    old.stop()
                except Exception:
                    pass
                self._hot_spine = None

    def sync_hot_stream(
        self,
        *,
        max_lines: Optional[int] = None,
        max_bytes: Optional[int] = None,
    ) -> dict:
        """Pull new native updates since checkpoint → normalize → history/hot.jsonl.

        Safe to call often (checkpointed). Shares sources/grok.json with any
        legacy full_stream_hook so the two cannot double-append.
        """
        if not getattr(self, "_hot_ingest", False):
            return {"status": "skipped", "reason": "hot ingest not configured"}
        if not getattr(self, "_agent_home", None) or not getattr(self, "_agent_name", None):
            return {"status": "skipped", "reason": "hot ingest not configured"}
        bus = getattr(self, "_native_bus", None)
        if bus is not None:
            try:
                r = bus.pump(max_lines=max_lines)
                # Normalize to tail_grok_once-like keys for callers/logs
                hot = (r or {}).get("hot") if isinstance(r, dict) else None
                if isinstance(hot, dict) and hot.get("status") == "ok":
                    return {
                        **hot,
                        "via": "bus",
                        "updates_lines": r.get("updates_lines"),
                        "behind": r.get("behind"),
                    }
                if isinstance(r, dict) and r.get("behind", 0) == 0 and not r.get("updates_lines"):
                    return {
                        "status": "ok",
                        "lines_ingested": 0,
                        "via": "bus",
                        "behind": 0,
                        "offset_after": bus.updates.offset,
                    }
                return r if isinstance(r, dict) else {"status": "ok", "via": "bus"}
            except Exception as e:
                return {"status": "error", "error": f"bus.pump: {e}"}

        try:
            from stream_adapters.grok import tail_grok_once
        except Exception as e:
            return {"status": "error", "error": f"import stream_adapters.grok: {e}"}

        source = None
        if self._session_dir:
            cand = Path(self._session_dir) / "updates.jsonl"
            if cand.exists():
                source = cand
        try:
            r = tail_grok_once(
                self._agent_home,
                self._agent_name,
                session_id=self._session_id,
                source=source,
                max_lines=max_lines,
                max_bytes=max_bytes,
            )
            # Nudge watcher so debounced path doesn't wait idle after we already synced
            w = getattr(self, "_updates_hot_watcher", None)
            if w is not None and isinstance(r, dict) and r.get("status") == "ok":
                # clear dirty - already synced; optional no-op
                pass
            return r
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _maybe_sync_hot_after_frames(self, n_frames: int) -> None:
        """During live collect, keep hot.jsonl caught up for TUI/SA readers."""
        # Tests often construct via __new__ without __init__ — be defensive.
        if not getattr(self, "_hot_ingest", False) or n_frames <= 0:
            return
        self._hot_frames_since_sync = getattr(self, "_hot_frames_since_sync", 0) + n_frames
        every = max(1, getattr(self, "_hot_sync_every_n_frames", 1))
        if self._hot_frames_since_sync >= every:
            self._hot_frames_since_sync = 0
            self.sync_hot_stream()
            w = getattr(self, "_updates_hot_watcher", None)
            if w is not None:
                try:
                    w.kick()
                except Exception:
                    pass

    def _final_hot_sync(self) -> None:
        """End-of-collect catch-up + spine kick (reconcile is source of truth)."""
        if not getattr(self, "_hot_ingest", False):
            return
        spine = getattr(self, "_hot_spine", None)
        if spine is not None:
            try:
                spine.reconcile(catch_up=True)
                spine.kick()
                return
            except Exception as e:
                print(f"[grok_backend] hot_spine final: {e}")
        try:
            self.sync_hot_stream()
        except Exception as e:
            print(f"[grok_backend] final hot sync: {e}")
        w = getattr(self, "_updates_hot_watcher", None)
        if w is not None:
            try:
                w.kick()
            except Exception:
                pass

    def refresh_tokens(self) -> int:
        """Read latest from updates.jsonl to get current token count.

        Processes both _meta.totalTokens frames AND auto_compact_completed
        events. When a compaction event is found, tokens_after is used as
        the authoritative count and the event is stored for pop_compaction_event().
        """
        if not self._file_source:
            return self._total_tokens

        updates, _ = self._file_source.read_new_lines()
        compaction_seen = False
        for frame in updates:
            self._last_activity_ts = time.time()

            # Detect compaction completion event FIRST — _meta frames that
            # follow carry stale pre-compaction totalTokens (issue_0029).
            su = frame.get("params", {}).get("update", {}).get("sessionUpdate", "")
            if su == "auto_compact_completed":
                tokens_after = frame.get("params", {}).get("update", {}).get("tokens_after")
                tokens_before = frame.get("params", {}).get("update", {}).get("tokens_before")
                if tokens_before:
                    self._compaction_tokens_before = tokens_before
                if tokens_after:
                    self._compaction_tokens_after = tokens_after
                    self._total_tokens = tokens_after
                self._compaction_event = frame
                compaction_seen = True
            elif not compaction_seen:
                meta = frame.get("params", {}).get("_meta", {})
                if meta.get("totalTokens"):
                    self._total_tokens = meta["totalTokens"]

        # Between turns: catch up hot even when FileEventSource saw nothing new
        # (e.g. hook disabled; TUI reading hot only).
        self.sync_hot_stream()

        return self._total_tokens

    def pop_compaction_event(self) -> tuple[bool, Optional[int], int]:
        """Return (True, tokens_after, tokens_before) if compaction detected.

        Clears the flag so subsequent calls return (False, None, 0) until
        another compaction event appears in updates.jsonl.

        tokens_before is a snapshot taken before refresh_tokens() processed
        the compaction event, so it reflects the actual pre-compaction count.
        """
        if self._compaction_event:
            tokens_after = self._compaction_tokens_after
            tokens_before = self._compaction_tokens_before
            self._compaction_event = None
            self._compaction_tokens_before = 0
            self._compaction_tokens_after = 0
            return True, tokens_after, tokens_before
        return False, None, 0

    async def drain_stale(self) -> tuple[int, str]:
        if not self._file_source:
            return 0, ""

        updates, events = self._file_source.read_new_lines()
        speech_chunks = []

        for frame in updates:
            params = frame.get("params", {})
            update = params.get("update", {})
            su = update.get("sessionUpdate", "")
            if su == "agent_message_chunk":
                c = update.get("content", {})
                text = c.get("text", "") if isinstance(c, dict) else ""
                if text:
                    speech_chunks.append(text)
            # Preserve compaction events even during drain — refresh_tokens()
            # won't see these frames (file pointer already advanced).
            elif su == "auto_compact_completed":
                tokens_after = update.get("tokens_after")
                tokens_before = update.get("tokens_before")
                if tokens_before:
                    self._compaction_tokens_before = tokens_before
                if tokens_after:
                    self._compaction_tokens_after = tokens_after
                    self._total_tokens = tokens_after
                self._compaction_event = frame
            # Token tracking from _meta — independent of sessionUpdate dispatch.
            # Skip after compaction (stale values, issue_0029).
            if not self._compaction_event:
                meta = params.get("_meta", {})
                if meta.get("totalTokens"):
                    self._total_tokens = meta["totalTokens"]

        return len(updates) + len(events), "".join(speech_chunks)

    async def request_compaction(self) -> bool:
        """Send /compact, collect response, send probe, return True."""
        # Send compact command
        await self._send(self._rpc_request("session/prompt", {
            "sessionId": self._session_id,
            "prompt": [{"type": "text", "text": "/compact"}],
        }))
        result = await self._collect_from_files(
            keepalive_timeout=180.0, max_wall_clock=300.0,
        )

        # Drain any stale frames
        await self.drain_stale()

        # Send probe to get real post-compaction token count
        probe_text = "[Compaction complete. You are resuming from a compacted context.]"
        await self._send(self._rpc_request("session/prompt", {
            "sessionId": self._session_id,
            "prompt": [{"type": "text", "text": probe_text}],
        }))
        probe_result = await self._collect_from_files(
            keepalive_timeout=60.0, max_wall_clock=300.0,
        )

        return True

    async def cancel_and_restart(self, agent_cwd: str) -> str:
        """Kill the current process mid-turn and restart with session/load.
        
        Used for mid-turn cancel. The partial turn is lost but the session
        state up to the last complete turn is preserved.
        
        Returns the session_id after reload.
        """
        session_id = self._session_id
        if not session_id:
            raise RuntimeError("No session to restart")
        
        # Kill the current process (also closes file source)
        await self.shutdown()
        
        # Restart with the same session and original launch params
        kwargs = dict(self._start_kwargs)
        kwargs["session_id"] = session_id
        return await self.start(agent_cwd, **kwargs)

    async def shutdown(self):
        if self._stdout_task:
            self._stdout_task.cancel()
            self._stdout_task = None
        if self._file_source:
            self._file_source.close()
            self._file_source = None
        if self._proc:
            if self._proc.stdin:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass

    @property
    def proc(self) -> Optional[asyncio.subprocess.Process]:
        return self._proc

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def session_dir(self) -> Optional[Path]:
        return getattr(self, '_session_dir', None)

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def has_pending_tool_calls(self) -> bool:
        """Check updates.jsonl for open tool calls (turn still in progress)."""
        if self._file_source:
            updates, _ = self._file_source.read_new_lines()
            for frame in updates:
                self._last_activity_ts = time.time()
                params = frame.get("params", {})
                update = params.get("update", {})
                su = update.get("sessionUpdate", "")
                if su == "tool_call":
                    tool_id = update.get("toolCallId")
                    if tool_id:
                        self._pending_tool_calls.add(tool_id)
                elif su == "tool_call_update":
                    tool_id = update.get("toolCallId")
                    if tool_id and update.get("status") == "completed":
                        self._pending_tool_calls.discard(tool_id)
                elif su == "user_message_chunk":
                    self._pending_tool_calls.clear()
        return bool(self._pending_tool_calls)

    @property
    def delivery_confirmed(self) -> bool:
        return self._delivery_confirmed

    @property
    def last_activity_ts(self) -> float:
        return self._last_activity_ts

    @property
    def total_tokens(self) -> int:
        return self._total_tokens

    @total_tokens.setter
    def total_tokens(self, value: int):
        self._total_tokens = value

    @property
    def context_window(self) -> int:
        return self._context_window

    @context_window.setter
    def context_window(self, value: int):
        self._context_window = value
