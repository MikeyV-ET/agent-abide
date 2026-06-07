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
        self._context_window: int = 200000
        self._claude_path: Optional[str] = None
        self._api_key: Optional[str] = api_key

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
        if yolo:
            cmd.append("--dangerously-skip-permissions")
        if model:
            cmd.extend(["--model", model])
        if session_id:
            cmd.extend(["--session-id", session_id])

        # API key auth: set env var and use --bare mode
        env = os.environ.copy()
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

        return self._session_id

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
                usage = frame.get("usage", {})
                turn_input = usage.get("input_tokens", 0)
                turn_output = usage.get("output_tokens", 0)
                cache_read = usage.get("cache_read_input_tokens", 0)
                cache_create = usage.get("cache_creation_input_tokens", 0)
                cost_usd = frame.get("total_cost_usd", 0.0)
                stop_reason = frame.get("stop_reason", "")

                turn_total = turn_input + turn_output + cache_read + cache_create
                self._total_tokens += turn_total

                # Extract context window from modelUsage if available
                model_usage = frame.get("modelUsage", {})
                for model_info in model_usage.values():
                    cw = model_info.get("contextWindow", 0)
                    if cw > 0:
                        self._context_window = cw

                if on_meta:
                    on_meta(self._total_tokens)

                if not frame.get("is_error", False):
                    result_text = frame.get("result", "")
                    if result_text and not speech_chunks:
                        speech_chunks.append(result_text)
                break
            else:
                self._process_frame(frame, speech_chunks, thought_chunks,
                                    on_speech_chunk, on_tool_call, on_meta)

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
                elif block_type == "thinking":
                    thought_text = block.get("thinking", "")
                    if thought_text:
                        thought_chunks.append(thought_text)
                elif block_type == "tool_use":
                    tool_name = block.get("name", "")
                    if on_tool_call and tool_name:
                        on_tool_call(tool_name)

            usage = message.get("usage", {})
            if usage and on_meta:
                turn_input = usage.get("input_tokens", 0)
                turn_output = usage.get("output_tokens", 0)
                cache_read = usage.get("cache_read_input_tokens", 0)
                cache_create = usage.get("cache_creation_input_tokens", 0)
                turn_total = turn_input + turn_output + cache_read + cache_create
                if turn_total > 0:
                    on_meta(self._total_tokens + turn_total)

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

    def refresh_tokens(self) -> int:
        """Return current accumulated token count.

        Claude Code tracks tokens per-turn via result frames. There is no
        external file to read between turns — the accumulated count from
        collect_response is the best available.
        """
        return self._total_tokens

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

        if not line:
            return None

        try:
            return json.loads(line.decode("utf-8").strip())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
