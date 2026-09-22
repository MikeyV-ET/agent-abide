"""
claude_backend.py -- ClaudeBackend: AgentBackend implementation for Claude Code CLI.

Speaks the Claude Code NDJSON stdio protocol:
  - Launch: claude --input-format stream-json --output-format stream-json --verbose
  - Output: line-delimited JSON with type field (system, assistant, result, stream_event, etc.)
  - Input: user messages as NDJSON with {type: "user", message: {role: "user", content: "..."}}
  - Completion: result frame terminates each turn

Key differences from GrokBackend:
  - NDJSON instead of JSON-RPC 2.0
  - Token tracking is per-turn (we accumulate)
  - No explicit session create/load RPCs (session via --session-id flag)
  - No compaction (Claude manages its own context)
  - result frame is the completion signal (no prompt_complete dance)
"""

import asyncio
import json
import os
import shutil
import time
from typing import Any, Callable, Optional

from agent_backend import AgentBackend, ResponseResult

# Max NDJSON frame size we will accept on stdout (asyncio default is 64 KiB).
STREAM_LIMIT_BYTES = 64 * 1024 * 1024

# How far back in the transcript to look for the last usage-bearing line.
SEED_SCAN_BYTES = 2 * 1024 * 1024
# A compact_boundary lands near the tail right after compaction, but the
# post-compaction turn can push it back quickly; scan wider than the seed.
COMPACT_SCAN_BYTES = 8 * 1024 * 1024


def _occupancy_from_usage(usage) -> int:
    """Tokens sitting in context for ONE request, from its usage block.

    Anthropic partitions the prompt into uncached + cache_read + cache_create;
    the sum plus this request's output is what occupies the window. This is a
    per-request figure — never add two of them together.
    """
    if not isinstance(usage, dict):
        return 0
    return (
        usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("output_tokens", 0)
    )


class ClaudeBackend(AgentBackend):
    """AgentBackend implementation for Claude Code CLI.
    
    Auth: pass api_key to constructor, or set ANTHROPIC_API_KEY in env.
    When api_key is provided, launches Claude Code in --bare mode (no
    OAuth, no keychain -- API key only). Without api_key, uses whatever
    auth Claude Code has configured (Max subscription, OAuth, etc.).
    """

    def __init__(self, api_key: Optional[str] = None):
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._session_id: Optional[str] = None
        self._model_id: str = "unknown"
        self._total_tokens: int = 0
        self._injected_texts: list = []
        self._context_window: int = 1000000  # Opus-class default; modelUsage overrides
        self._claude_path: Optional[str] = None
        self._api_key: Optional[str] = api_key
        # --- grok-parity surface (asdaaas touches these on some paths) ---
        self._start_kwargs: dict = {}
        self._allowed_always: set[str] = set()
        self._permission_handler: Optional[Callable] = None
        # --- AA history: this backend owns native -> aa.stream -> hot ---
        self._agent_home = None
        self._agent_name: Optional[str] = None
        self._hot_ingest: bool = False
        # --- binary state observation (ClaudeInProcessObserver) ---
        self._observer = None
        # --- compaction: uuid of the last compact_boundary already reported ---
        self._last_compaction_uuid: Optional[str] = None

    async def start(self, agent_cwd: str, model: Optional[str] = None,
                    session_id: Optional[str] = None, yolo: bool = True,
                    **kwargs) -> str:
        self._claude_path = shutil.which("claude") or str(
            __import__("pathlib").Path.home() / ".local" / "bin" / "claude"
        )

        cmd = [
            self._claude_path,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
        ]
        # Mid-turn stdin inject (Astro probe ddb0e7e): echo user msgs back on
        # stdout so asdaaas/TUI can record them. Safe with flag off of inject.
        # Gate: interjection_enabled (agents.json) or explicit claude_stdin_interject.
        # Default OFF unless interjections are on — matches dogfood "flag off" spirit
        # while turning on automatically with Eric's interjection_enabled flip.
        if kwargs.get("interjection_enabled") or kwargs.get("claude_stdin_interject", False):
            cmd.append("--replay-user-messages")
        if yolo:
            cmd.append("--dangerously-skip-permissions")
        if model:
            cmd.extend(["--model", model])
        if session_id:
            # --session-id on an id that already has a session file does NOT
            # resume -- it silently starts a fresh conversation. Use --resume
            # for sessions Claude already knows about.
            if self._session_file_exists(session_id):
                cmd.extend(["--resume", session_id])
            else:
                cmd.extend(["--session-id", session_id])

        # API key auth: set env var and use --bare mode
        env = self._build_child_env(
            agent_cwd,
            agent_name=kwargs.get("agent_name"),
            interjection_enabled=bool(kwargs.get("interjection_enabled")),
        )
        api_key = self._api_key or os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
            cmd.append("--bare")

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=agent_cwd,
            env=env,
            # asyncio's StreamReader defaults to a 64 KiB line limit; Claude
            # NDJSON frames (large tool results, full result text) exceed it
            # and readline() then raises ValueError, killing the turn.
            limit=STREAM_LIMIT_BYTES,
        )

        # Wait for process to be ready (not crash immediately)
        await asyncio.sleep(1.0)
        if self._proc.returncode is not None:
            stderr_out = ""
            if self._proc.stderr:
                try:
                    stderr_out = (await asyncio.wait_for(
                        self._proc.stderr.read(2000), timeout=1.0
                    )).decode("utf-8", errors="replace")
                except (asyncio.TimeoutError, Exception):
                    pass
            raise RuntimeError(
                f"Claude process exited immediately (code {self._proc.returncode}): {stderr_out}"
            )

        # With --input-format stream-json, the init frame arrives after the
        # first user message is sent. We extract session/model lazily in
        # collect_response when we see the system init frame.
        self._session_id = session_id or "pending"
        self._stashed_frame = None
        self._start_kwargs = dict(model=model, yolo=yolo, session_id=session_id,
                                  agent_cwd=agent_cwd, **kwargs)

        return self._session_id

    @staticmethod
    def _build_child_env(agent_cwd: str, agent_name: str = None,
                         interjection_enabled: bool = False) -> dict:
        """Environment for the claude child process (mirrors GrokBackend).

        AGENT_HOME/AGENT_NAME let interjection_hook.sh find the right queue.
        BASH_ENV installs the hook itself: it is sourced by every ``bash -c``
        the binary runs, and prepends any queued messages to that tool call's
        stdout. Without it, queued interjections sit on disk until the turn
        ends and nothing records that a message arrived.
        """
        from pathlib import Path as _Path

        env = {
            **os.environ,
            "AGENT_HOME": agent_cwd,
            "ASDAAAS_DIR": str(_Path(agent_cwd) / "asdaaas"),
        }
        if agent_name:
            env["AGENT_NAME"] = agent_name
        if interjection_enabled and agent_name:
            hook_path = _Path(__file__).parent / "interjection_hook.sh"
            if hook_path.exists():
                env["BASH_ENV"] = str(hook_path)
        return env

    @staticmethod
    def _session_file_exists(session_id: str) -> bool:
        """True if Claude Code already has a session log for this id.

        Session ids are UUIDs and globally unique, so glob across all project
        dirs rather than recomputing Claude's cwd-escaping scheme.
        """
        from pathlib import Path as _Path
        base = _Path.home() / ".claude" / "projects"
        if not base.is_dir():
            return False
        try:
            return any(base.glob(f"{session_id}.jsonl")) or any(
                base.glob(f"*/{session_id}.jsonl")
            )
        except OSError:
            return False

    # ---- binary state observation ----

    def set_observer(self, observer):
        """Set the in-process observer (mirrors GrokBackend.set_observer).

        Claude has no stdout notification plane to forward, so the observer
        tails the session transcript itself; this handle exists so asdaaas can
        reach it on restart/shutdown the same way it does for grok.
        """
        self._observer = observer

    @property
    def session_file(self) -> Optional[str]:
        """Path to this session's Claude Code transcript jsonl, if findable.

        Same resolution the hot-stream ingest uses, so both read one file.
        """
        if not self._agent_home:
            return None
        try:
            from stream_adapters.claude import find_live_session
        except Exception:
            return None
        sid = self._session_id if self._session_id != "pending" else None
        try:
            path = find_live_session(self._agent_home, sid)
        except Exception:
            return None
        return str(path) if path else None

    # ---- session limit detection ----

    @staticmethod
    def _is_cli_authored(frame: dict) -> bool:
        """True for messages Claude Code itself wrote, never for model speech.

        Real limits arrive as CLI-authored assistant messages: model "<synthetic>",
        isApiErrorMessage true, error "rate_limit" (e.g. "You've hit your session
        limit · resets 8:40pm (America/Los_Angeles)"). Model speech can say
        anything — including quoting that exact sentence — and must never park.
        """
        if not isinstance(frame, dict):
            return False
        message = frame.get("message") or {}
        return (
            message.get("model") == "<synthetic>"
            or bool(frame.get("isApiErrorMessage"))
            or bool(frame.get("error"))
        )

    @staticmethod
    def _limit_info_from_result(frame: dict):
        """Limit info from a result frame, or None.

        Only error results count. On a normal turn frame["result"] is the
        model's final text, so scanning it re-introduces the speech false
        positive through the back door.
        """
        if not isinstance(frame, dict):
            return None
        if not (frame.get("is_error") or frame.get("error") or frame.get("errors")):
            return None
        from session_limit import inspect_limit_text

        blob = " ".join(
            str(x)
            for x in (frame.get("result"), frame.get("error"), frame.get("errors"))
            if x
        )
        info = inspect_limit_text(blob, source="claude_result")
        return info if info.detected else None

    # ---- AA history ingest (mirrors GrokBackend) ----

    def configure_aa_history(self, agent_home, agent_name: str, *,
                             enabled: bool = True) -> None:
        """Enable AA hot.jsonl ingest owned by this backend.

        Acquisition: Claude session jsonl under ~/.claude/projects/.
        Normalization: stream_adapters.claude (map/wrap -> aa.stream).
        Write: aa_stream.append_hot_events.
        """
        from pathlib import Path as _Path
        self._agent_home = _Path(agent_home)
        self._agent_name = agent_name
        self._hot_ingest = bool(enabled)

    def sync_hot_stream(self, *, max_lines: Optional[int] = None,
                        max_bytes: Optional[int] = None) -> dict:
        """Pull new native session bytes since checkpoint -> history/hot.jsonl.

        Safe to call often (checkpointed via sources/claude.json).
        """
        if not self._hot_ingest or not self._agent_home or not self._agent_name:
            return {"status": "skipped", "reason": "hot ingest not configured"}
        try:
            from stream_adapters.claude import tail_claude_once
        except Exception as e:
            return {"status": "error", "error": f"import stream_adapters.claude: {e}"}
        sid = self._session_id if self._session_id != "pending" else None
        try:
            return tail_claude_once(
                self._agent_home,
                self._agent_name,
                session_id=sid,
                max_lines=max_lines,
                max_bytes=max_bytes,
            )
        except Exception as e:
            return {"status": "error", "error": str(e)}


    async def inject_user_message(self, text: str) -> bool:
        """Write a user message to stdin mid-turn (Claude Code holds until tool ends).

        Requires --replay-user-messages at launch. Returns True if written.
        Exact text is recorded in _injected_texts so replayed type:user frames
        can be recognized as interjections (not the original prompt).
        """
        if not text or not str(text).strip():
            return False
        if not self._proc or not self._proc.stdin:
            return False
        if self._proc.returncode is not None:
            return False
        try:
            msg = json.dumps({
                "type": "user",
                "message": {"role": "user", "content": text},
            }) + "\n"
            self._proc.stdin.write(msg.encode("utf-8"))
            await self._proc.stdin.drain()
            self._injected_texts.append(text)
            # cap list
            if len(self._injected_texts) > 50:
                self._injected_texts = self._injected_texts[-30:]
            # Label for TUI / speech SoR (was_injected was unused — Astro 05148cf review)
            try:
                self._record_stdin_interjection(text)
            except Exception as e:
                print(f"[claude_backend] interjection record failed: {e}")
            return True
        except Exception as e:
            print(f"[claude_backend] inject_user_message failed: {e}")
            return False

    def _record_stdin_interjection(self, text: str) -> None:
        """Put a stdin-injected message into hot.jsonl as an interjection.

        The TUI renders body.kind == "interjection" as its InterjectionBlock
        (tui_history.aa_event_to_tui_update), which is how Eric sees that a
        message arrived while the agent was working rather than as a new turn.

        Same thread as the hot ingest (both run on asdaaas's event loop, and
        neither awaits between reading and writing stream_seq_next), so the
        sequence counter cannot interleave.
        """
        if not text or not self._agent_home or not self._agent_name:
            return

        # Catch the hot ingest up to the transcript FIRST. The TUI orders by
        # stream_seq, and the ingest lags the transcript: without this the
        # interjection takes the next seq while speech and tool calls that
        # happened earlier are still unread, so they get ingested afterwards
        # with higher numbers and the panel draws above things that preceded
        # it. (Seen live: seq 2168 at 21:20:12 above seq 2170 from 21:20:06.)
        try:
            self.sync_hot_stream()
        except Exception as e:
            # Worth losing ordering over, not worth losing the record over.
            print(f"[claude_backend] pre-record hot sync failed: {e}")

        from aa_stream import (
            append_hot_events,
            build_event,
            default_hot_meta,
            ensure_aa_stream_layout,
            read_hot_meta,
            resolve_history_dir,
            write_hot_meta,
        )
        from stream_adapters.claude import NATIVE_CLAUDE

        fs_dir = ensure_aa_stream_layout(resolve_history_dir(self._agent_home), self._agent_name)
        meta = read_hot_meta(fs_dir) or default_hot_meta(self._agent_name, fs_dir)
        seq = int(meta.get("stream_seq_next") or 0)
        sid = self._session_id if self._session_id not in (None, "pending") else ""

        event = build_event(
            agent=self._agent_name,
            backend="claude",
            session_id=sid,
            stream_seq=seq,
            native_schema=NATIVE_CLAUDE,
            native_event={"type": "asdaaas-stdin-interjection", "text": text},
            class_="message",
            phase="full",
            role="user",
            body={"kind": "interjection", "text": text},
            source={"path": "asdaaas:stdin-inject", "offset": 0},
        )
        append_hot_events(fs_dir, [event])
        meta["stream_seq_next"] = seq + 1
        write_hot_meta(fs_dir, meta)
        # Side file so session-jsonl ingest can skip the same text (avoid double TUI paint)
        try:
            side = fs_dir / "injected_stdin.jsonl"
            with open(side, "a", encoding="utf-8") as f:
                import json as _json, time as _time
                f.write(_json.dumps({"ts": _time.time(), "text": text}) + "\n")
        except Exception:
            pass

    def was_injected(self, text: str) -> bool:
        """True if text matches a recent stdin interject (exact)."""
        if not text:
            return False
        texts = getattr(self, "_injected_texts", None) or []
        return any(text == t or text.strip() == t.strip() for t in texts)

    @property
    def session_limited(self) -> bool:
        return bool(getattr(self, "_session_limited", False))

    @property
    def session_limit_info(self):
        return getattr(self, "_session_limit_info", None)

    async def send_prompt(self, text: str) -> Any:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("Claude backend not started")

        # Check process is still alive before writing
        if self._proc.returncode is not None:
            raise RuntimeError(
                f"Claude process already exited (code {self._proc.returncode})"
            )

        # Claude Code stream-json input format
        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": text}
        }) + "\n"
        self._proc.stdin.write(msg.encode("utf-8"))
        await self._proc.stdin.drain()
        return None  # NDJSON doesn't use request IDs

    async def collect_response(
        self,
        handle: Any,
        on_speech_chunk: Optional[Callable[[str], None]] = None,
        on_tool_call: Optional[Callable[[str], None]] = None,
        on_meta: Optional[Callable[[int], None]] = None,
        keepalive_timeout: float = 30.0,
        max_wall_clock: float = 600.0,
        cancel_event=None,
    ) -> ResponseResult:
        speech_chunks = []
        thought_chunks = []
        stop_reason = ""
        cost_usd = 0.0

        wall_deadline = time.monotonic() + max_wall_clock
        last_frame_time = time.monotonic()

        # Process any stashed frame from init
        if hasattr(self, '_stashed_frame') and self._stashed_frame:
            frame = self._stashed_frame
            self._stashed_frame = None
            self._process_frame(frame, speech_chunks, thought_chunks,
                                on_speech_chunk, on_tool_call, on_meta)

        while True:
            remaining_keepalive = keepalive_timeout - (time.monotonic() - last_frame_time)
            remaining_wall = wall_deadline - time.monotonic()
            wait_timeout = max(0.1, min(remaining_keepalive, remaining_wall))

            if remaining_keepalive <= 0 or remaining_wall <= 0:
                break

            frame = await self._read_frame(timeout=wait_timeout)
            if frame is None:
                break

            last_frame_time = time.monotonic()
            frame_type = frame.get("type", "")

            if frame_type == "result":
                try:
                    info = self._limit_info_from_result(frame)
                    if info is not None:
                        self._session_limited = True
                        self._session_limit_info = info
                        stop_reason = "session_limit"
                        print(
                            "[claude_backend] SESSION LIMIT detected (reset=%s)"
                            % (info.reset_unix,)
                        )
                except Exception as e:
                    print("[claude_backend] session_limit inspect failed: %s" % e)

                usage = frame.get("usage", {})
                turn_input = usage.get("input_tokens", 0)
                turn_output = usage.get("output_tokens", 0)
                cache_read = usage.get("cache_read_input_tokens", 0)
                cache_create = usage.get("cache_creation_input_tokens", 0)
                cost_usd = frame.get("total_cost_usd", 0.0)
                stop_reason = frame.get("stop_reason", "")

                # Context *occupancy* (not lifetime cumulative).
                # Anthropic partitions the prompt: uncached + cache_read + cache_create.
                # Sum ≈ tokens sitting in context for this request. Do NOT += across turns
                # and do NOT treat cache_read as "extra" on top of a running total.
                prompt_tokens = turn_input + cache_read + cache_create
                # After the turn, occupancy ≈ prompt + this output.
                # Never clobber a known occupancy with 0 (result frames sometimes
                # omit usage → totalTokens=0 → empty context_left + crap TUI %).
                #
                # The result frame's usage is an AGGREGATE over every request the
                # turn made, so it overstates occupancy by roughly the number of
                # tool calls. Per-message occupancy from _process_frame is the
                # truthful figure; only fall back to this when no message in the
                # turn carried usage.
                occupancy = prompt_tokens + turn_output
                if self._total_tokens <= 0 and occupancy > 0:
                    self._total_tokens = occupancy

                # Extract context window from modelUsage if available
                model_usage = frame.get("modelUsage", {}) or {}
                for model_info in model_usage.values():
                    if not isinstance(model_info, dict):
                        continue
                    cw = model_info.get("contextWindow", 0) or model_info.get("context_window", 0)
                    if cw > 0:
                        self._context_window = int(cw)
                # Fallback: env or known large windows
                if self._context_window <= 200000:
                    import os
                    env_cw = os.environ.get("CLAUDE_CONTEXT_WINDOW") or os.environ.get("CONTEXT_WINDOW")
                    if env_cw and env_cw.isdigit():
                        self._context_window = int(env_cw)

                if on_meta:
                    on_meta(self._total_tokens)

                if not frame.get("is_error", False):
                    result_text = frame.get("result", "")
                    if result_text and not speech_chunks:
                        speech_chunks.append(result_text)
                try:
                    self.sync_hot_stream()
                except Exception:
                    pass
                break
            else:
                self._process_frame(frame, speech_chunks, thought_chunks,
                                    on_speech_chunk, on_tool_call, on_meta)
                # Live TUI reads history/hot.jsonl. Session jsonl grows during
                # the turn (complete assistant/tool lines, not token deltas).
                # Sync often so paint is not stuck until turn boundary.
                try:
                    self.sync_hot_stream()
                except Exception:
                    pass

        if getattr(self, "_session_limited", False):
            stop_reason = "session_limit"
        # No scan of joined speech here: it is the model's own words, and an
        # agent explaining a limit is not an agent at one. (Parked twice for an
        # hour on 2026-09-21 for writing "I was parked on a usage limit".)

        return ResponseResult(
            speech="".join(speech_chunks),
            thoughts="".join(thought_chunks),
            total_tokens=self._total_tokens,
            model_id=self._model_id,
            stop_reason=stop_reason,
            cost_usd=cost_usd,
        )

    def _process_frame(self, frame, speech_chunks, thought_chunks,
                       on_speech_chunk, on_tool_call, on_meta):
        """Process a single non-result frame."""
        frame_type = frame.get("type", "")

        if frame_type == "assistant":
            message = frame.get("message", {})
            for block in message.get("content", []):
                block_type = block.get("type", "")
                if block_type == "text":
                    text = block.get("text", "")
                    if text:
                        speech_chunks.append(text)
                        if on_speech_chunk:
                            on_speech_chunk(text)
                        if self._is_cli_authored(frame):
                            try:
                                from session_limit import inspect_limit_text
                                info = inspect_limit_text(text, source="claude_cli")
                                if info.detected:
                                    self._session_limited = True
                                    self._session_limit_info = info
                            except Exception:
                                pass
                elif block_type == "thinking":
                    thought_text = block.get("thinking", "")
                    if thought_text:
                        thought_chunks.append(thought_text)
                elif block_type == "tool_use":
                    tool_name = block.get("name", "")
                    if on_tool_call and tool_name:
                        on_tool_call(tool_name)

            # Each assistant message is one API request, and its usage already
            # describes the WHOLE context that request carried. Adding requests
            # together counts the same tokens once per tool call — measured on a
            # live transcript, a 29-request turn summed to 22x its occupancy.
            occupancy = _occupancy_from_usage(message.get("usage"))
            if occupancy > 0:
                self._total_tokens = occupancy
                if on_meta:
                    on_meta(occupancy)

        elif frame_type == "stream_event":
            event = frame.get("event", {})
            delta = event.get("delta", {})
            delta_type = delta.get("type", "")
            if delta_type == "text_delta":
                text = delta.get("text", "")
                if text:
                    speech_chunks.append(text)
                    if on_speech_chunk:
                        on_speech_chunk(text)
            elif delta_type == "thinking_delta":
                text = delta.get("thinking", "")
                if text:
                    thought_chunks.append(text)

        elif frame_type == "system" and frame.get("subtype") == "init":
            self._session_id = frame.get("session_id", self._session_id)
            self._model_id = frame.get("model", self._model_id)

        # rate_limit_event, user echo, etc. -- skip silently

    def _seed_tokens_from_session(self, session_file: str = None) -> int:
        """Recover occupancy from the transcript when the count is cold.

        _total_tokens is per-process, so after a restart it is 0 — and
        context_left_tag() returns an empty string on 0, so the agent gets no
        telemetry line at all until the first turn completes. The last assistant
        line in the session jsonl already knows the answer. Mirrors
        GrokBackend._seed_tokens_from_session, whose source is updates.jsonl.

        Never lowers a live count; returns the occupancy in effect afterwards.
        """
        if self._total_tokens > 0:
            return self._total_tokens

        path = session_file or self.session_file
        if not path or not os.path.exists(path):
            return self._total_tokens

        try:
            with open(path, "rb") as f:
                size = f.seek(0, os.SEEK_END)
                f.seek(max(0, size - SEED_SCAN_BYTES))
                chunk = f.read().decode("utf-8", errors="replace")
        except OSError:
            return self._total_tokens

        for line in reversed(chunk.split("\n")):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # truncated head of the scan window, or a partial write
            if obj.get("type") != "assistant":
                continue
            occupancy = _occupancy_from_usage((obj.get("message") or {}).get("usage"))
            if occupancy > 0:
                self._total_tokens = occupancy
                return occupancy

        return self._total_tokens

    def refresh_tokens(self) -> int:
        """Return current token occupancy; also catch up hot.jsonl.

        Claude has no updates.jsonl — session jsonl is the native SoR.
        Main-loop refresh_tokens is our between-turn chance to tail it
        (Grok does the same from refresh_tokens).
        """
        try:
            self.sync_hot_stream()
        except Exception:
            pass
        if self._total_tokens <= 0:
            # Cold after a restart — recover from the transcript rather than
            # reporting 0, which suppresses the context_left tag entirely.
            try:
                self._seed_tokens_from_session()
            except Exception:
                pass
        return self._total_tokens

    #: Measured live: a manual /compact of an 878k context took durationMs
    #: 115040. The 30s default would have declared it failed while it worked.
    compaction_poll_seconds: int = 240

    def _latest_compact_boundary(self) -> Optional[dict]:
        """Newest `compact_boundary` record in the session transcript, or None.

        Claude Code writes one of these every time it compacts, auto or manual:

            {"type": "system", "subtype": "compact_boundary",
             "compactMetadata": {"trigger": "manual", "preTokens": 878559,
                                 "postTokens": 9961, "durationMs": 115040, ...}}

        preTokens/postTokens are exact, which is why this is preferred over the
        usage-based estimate in _seed_tokens_from_session.
        """
        path = self.session_file
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                size = f.seek(0, os.SEEK_END)
                f.seek(max(0, size - COMPACT_SCAN_BYTES))
                chunk = f.read().decode("utf-8", errors="replace")
        except OSError:
            return None

        for line in reversed(chunk.split("\n")):
            line = line.strip()
            if not line or "compact_boundary" not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # truncated head of the scan window, or a partial write
            if obj.get("subtype") == "compact_boundary" and obj.get("compactMetadata"):
                return obj
        return None

    def pop_compaction_event(self) -> tuple[bool, Optional[int], int]:
        """(landed, tokens_after, tokens_before) for a not-yet-reported compaction.

        Claude's answer to GrokBackend's auto_compact_completed frame. Without
        this the base class returns (False, None, 0), and turn_engine's

            tokens_before = event_tb or tokens_before
            self.total_tokens = event_ta or self.total_tokens

        silently keeps the PRE-compaction numbers. Live on 2026-09-22 that told
        me "Context reduced from 878095 to 878095 tokens" with a context_left
        tag of "0.0k till autocompaction", on a context that had just fallen to
        9,961 -- the numbers were on disk the whole time.

        Each boundary is reported once (deduped by uuid), so the poll loop can
        call this repeatedly while it waits. Also lowers _total_tokens, which is
        otherwise stale until the next assistant frame carries fresh usage.
        """
        boundary = self._latest_compact_boundary()
        if not boundary:
            return False, None, 0

        uuid = boundary.get("uuid")
        if uuid and uuid == self._last_compaction_uuid:
            return False, None, 0  # already reported this one

        meta = boundary.get("compactMetadata") or {}
        post = meta.get("postTokens")
        pre = meta.get("preTokens")
        if not isinstance(post, int) or not isinstance(pre, int):
            return False, None, 0

        self._last_compaction_uuid = uuid
        self._total_tokens = post
        return True, post, pre

    async def drain_stale(self) -> tuple[int, str]:
        drained = 0
        speech_chunks = []

        while True:
            frame = await self._read_frame(timeout=0.1)
            if frame is None:
                break
            drained += 1
            if frame.get("type") == "assistant":
                for block in frame.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        speech_chunks.append(block.get("text", ""))

        return drained, "".join(speech_chunks)

    async def request_compaction(self) -> bool:
        return False  # Claude Code manages its own context

    def set_permission_handler(self, handler: Callable) -> None:
        """Accept a mentor-approval handler for interface parity.

        Claude decides permissions in-process via --permission-mode, so there
        is no per-tool callback to route. Stored, not consulted.
        """
        self._permission_handler = handler

    async def set_reasoning_effort(self, level: str) -> None:
        """Record requested effort. Claude takes --effort at launch only."""
        self._start_kwargs["reasoning_effort"] = level

    async def shutdown(self):
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
    def model_id(self) -> str:
        return self._model_id

    @property
    def total_tokens(self) -> int:
        return self._total_tokens

    @property
    def context_window(self) -> int:
        return self._context_window

    @context_window.setter
    def context_window(self, value: int):
        self._context_window = int(value)

    async def _read_frame(self, timeout: float = 30.0) -> Optional[dict]:
        if not self._proc or not self._proc.stdout:
            return None
        try:
            line = await asyncio.wait_for(
                self._proc.stdout.readline(),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            return None
        except (ValueError, asyncio.LimitOverrunError) as e:
            # Frame exceeded the stream buffer. Degrade to skipping this frame
            # rather than propagating and killing the whole turn.
            print(f"[claude_backend] oversized frame skipped: {e}")
            return {"type": "_oversized"}

        if not line:
            return None

        try:
            return json.loads(line.decode("utf-8").strip())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
