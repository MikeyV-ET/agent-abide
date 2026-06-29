"""
mock_binary.py -- MockBinary: Scriptable AgentBackend for E2E testing.

Replaces GrokBackend in tests. No subprocess, no LLM calls. The test
defines a scenario (list of steps), and MockBinary executes them in order
when asdaaas calls collect_response.

Usage:
    scenario = [
        NormalResponse(speech="Hello.", tokens=5000),
        ToolCallOnly(retry_duration=2.0, resolve_speech="Done."),
        Compaction(tokens_before=150000, tokens_after=30000),
    ]
    mock = MockBinary(scenario)
    await main("TestAgent", backend=mock)
"""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from agent_backend import AgentBackend, ResponseResult, TurnCancelled


# ---------------------------------------------------------------------------
# Scenario step types
# ---------------------------------------------------------------------------

@dataclass
class NormalResponse:
    """Clean turn: agent produces speech."""
    speech: str = "OK."
    tokens: int = 5000


@dataclass
class ToolCallOnly:
    """Turn ends with tool call, no visible text -- triggers binary retry.

    From asdaaas's perspective, collect_response just takes longer and
    returns empty speech (the binary handles retries internally).
    retry_duration is how long collect_response blocks (simulating the
    binary's internal retry loop). resolve_speech is what eventually
    comes back (empty string = the retry never resolved with text).
    """
    retry_duration: float = 2.0
    resolve_speech: str = ""
    tokens: int = 5000


@dataclass
class DoomLoop:
    """Consecutive non-zero exits -- doom_loop_detected."""
    exit_count: int = 5
    tokens: int = 5000


@dataclass
class CommandWriter:
    """Turn where the agent writes command queue files during its response.

    Simulates what a real agent does when it uses tool calls to write
    commands like {"action": "compact"}, {"action": "gaze", ...}, etc.
    The commands list is written to the agent's command queue directory
    during collect_response, before the speech is returned.
    """
    speech: str = "OK."
    tokens: int = 5000
    commands: list = field(default_factory=list)


@dataclass
class Compaction:
    """Simulate auto-compaction."""
    tokens_before: int = 150000
    tokens_after: int = 30000


@dataclass
class EmptyResponse:
    """Turn with no speech or tools."""
    tokens: int = 5000


@dataclass
class SlowResponse:
    """Response that takes wall-clock time (for keepalive timeout testing)."""
    speech: str = "Thinking..."
    delay: float = 5.0
    tokens: int = 5000


@dataclass
class LongToolCallResponse:
    """Turn that blocks for a long time with visible tool_call activity.

    Simulates a real agent doing wait_commands_or_subagents or other long
    blocking tool calls. The binary writes tool_call (Pending) to
    updates.jsonl, blocks for `duration` seconds (writing periodic
    tool_call_update entries to show activity), then returns speech.

    This reproduces the Jr stale-continue bug: asdaaas should not queue
    continues while the binary has an open tool call, but it does because
    it only checks the receipt timeout.
    """
    speech: str = "Done with long work."
    tokens: int = 5000
    duration: float = 10.0
    activity_interval: float = 2.0  # write a tool_call_update every N seconds
    early_return_after: float = 0.0  # if > 0, collect_response returns early (simulates wall_clock_timeout)


@dataclass
class SplitCommandWriter:
    """Turn that writes commands at two different times during the response.

    Simulates a real agent that writes delay:0 early (first tool call), then
    does long work (subagents/tool calls), then writes delay:600 late. Tests
    whether the post-response drain picks up both commands or races.
    """
    speech: str = "Done with split work."
    tokens: int = 5000
    early_commands: list = field(default_factory=list)
    delay_between: float = 2.0  # seconds between early and late commands
    late_commands: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# MockBinary
# ---------------------------------------------------------------------------

class MockBinary(AgentBackend):
    """Scriptable AgentBackend that executes scenario steps without a subprocess."""

    def __init__(self, scenario: list, context_window: int = 200000,
                 startup_delay: float = 0.0):
        self._scenario = list(scenario)
        self._step_index = 0
        self._session_id: Optional[str] = None
        self._session_dir: Optional[Path] = None
        self._total_tokens: int = 0
        self._context_window: int = context_window
        self._model_id: str = "mock-model"
        self._prompt_count: int = 0
        self._last_prompt: str = ""
        self._all_prompts: list[str] = []
        self._compaction_event: Optional[dict] = None
        self._compaction_tokens_before: int = 0
        self._compaction_tokens_after: int = 0
        self._last_activity_ts: float = 0.0
        self._pending_tool_calls: set[str] = set()
        self._startup_delay: float = startup_delay
        self._agent_cwd: Optional[str] = None

    # -- Event writing helpers --

    def _write_update(self, update: dict):
        """Append a session update event to updates.jsonl."""
        self._last_activity_ts = time.time()
        if not self._session_dir:
            return
        frame = {
            "timestamp": int(time.time()),
            "method": "session/update",
            "params": {
                "sessionId": self._session_id,
                "update": update,
            },
        }
        path = self._session_dir / "updates.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(frame) + "\n")

    def _write_update_with_meta(self, update: dict, tokens: int):
        """Append a session update with _meta.totalTokens."""
        self._last_activity_ts = time.time()
        if not self._session_dir:
            return
        frame = {
            "timestamp": int(time.time()),
            "method": "session/update",
            "params": {
                "sessionId": self._session_id,
                "update": update,
                "_meta": {"totalTokens": tokens},
            },
        }
        path = self._session_dir / "updates.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(frame) + "\n")

    def _write_event(self, event: dict):
        """Append a lifecycle event to events.jsonl."""
        if not self._session_dir:
            return
        path = self._session_dir / "events.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(event) + "\n")

    def _write_speech(self, text: str, tokens: int):
        """Write agent_message_chunk + turn_ended."""
        if text:
            self._write_update_with_meta({
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            }, tokens)
        self._write_event({"type": "turn_ended", "outcome": "completed"})
        self._total_tokens = tokens

    # -- AgentBackend interface --

    async def start(self, agent_cwd: str, model: Optional[str] = None,
                    session_id: Optional[str] = None, **kwargs) -> str:
        self._session_id = session_id or str(uuid.uuid4())
        self._agent_cwd = agent_cwd

        # Simulate slow session load (large sessions take >30s in real grok)
        if self._startup_delay > 0:
            await asyncio.sleep(self._startup_delay)

        # Create session dir with empty files
        encoded_cwd = agent_cwd.replace("/", "%2F")
        sessions_base = Path.home() / ".grok" / "sessions"
        self._session_dir = sessions_base / encoded_cwd / self._session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)

        for fname in ("updates.jsonl", "events.jsonl"):
            fpath = self._session_dir / fname
            if not fpath.exists():
                fpath.touch()

        if model:
            self._model_id = model

        return self._session_id

    async def send_prompt(self, text: str) -> Any:
        self._prompt_count += 1
        self._last_prompt = text
        self._all_prompts.append(text)
        self._pending_tool_calls.clear()  # new turn — prior tools done

        # Write the prompt as a user_message_chunk so audit tools can see it
        self._write_update({
            "sessionUpdate": "user_message_chunk",
            "content": {"type": "text", "text": text},
        })

        return self._prompt_count

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
        if self._step_index >= len(self._scenario):
            # No more steps -- return empty response
            self._write_speech("", self._total_tokens)
            return ResponseResult(
                speech="", thoughts="",
                total_tokens=self._total_tokens,
                model_id=self._model_id,
                stop_reason="completed",
            )

        step = self._scenario[self._step_index]
        self._step_index += 1

        if isinstance(step, NormalResponse):
            return await self._do_normal(step, on_speech_chunk, on_meta, cancel_event)
        elif isinstance(step, CommandWriter):
            return await self._do_command_writer(step, on_speech_chunk, on_meta, cancel_event)
        elif isinstance(step, ToolCallOnly):
            return await self._do_tool_call_only(step, on_speech_chunk, on_meta, cancel_event, on_tool_call)
        elif isinstance(step, DoomLoop):
            return await self._do_doom_loop(step, on_meta)
        elif isinstance(step, Compaction):
            return await self._do_compaction(step, on_meta)
        elif isinstance(step, EmptyResponse):
            return await self._do_empty(step, on_meta)
        elif isinstance(step, LongToolCallResponse):
            return await self._do_long_tool_call(step, on_speech_chunk, on_meta, cancel_event, on_tool_call)
        elif isinstance(step, SlowResponse):
            return await self._do_slow(step, on_speech_chunk, on_meta, cancel_event)
        elif isinstance(step, SplitCommandWriter):
            return await self._do_split_command(step, on_speech_chunk, on_meta, cancel_event)
        else:
            raise ValueError(f"Unknown scenario step type: {type(step)}")

    async def _do_normal(self, step: NormalResponse, on_speech_chunk, on_meta, cancel_event):
        if cancel_event and cancel_event.is_set():
            raise TurnCancelled("cancel_event set")

        self._write_speech(step.speech, step.tokens)

        if on_speech_chunk and step.speech:
            on_speech_chunk(step.speech)
        if on_meta:
            on_meta(step.tokens)

        return ResponseResult(
            speech=step.speech, thoughts="",
            total_tokens=step.tokens,
            model_id=self._model_id,
            stop_reason="completed",
        )

    async def _do_command_writer(self, step: CommandWriter, on_speech_chunk, on_meta, cancel_event):
        if cancel_event and cancel_event.is_set():
            raise TurnCancelled("cancel_event set")

        # Write command files to the agent's command queue (simulates tool calls)
        if self._agent_cwd:
            cmd_dir = Path(self._agent_cwd) / "asdaaas" / "commands"
            cmd_dir.mkdir(parents=True, exist_ok=True)
            import secrets
            for cmd in step.commands:
                ts = int(time.time() * 1000)
                rand = secrets.token_hex(4)
                path = cmd_dir / f"cmd_{ts}_{rand}.json"
                with open(path, "w") as f:
                    json.dump(cmd, f)
                # Small delay so filenames sort correctly
                await asyncio.sleep(0.005)

        self._write_speech(step.speech, step.tokens)

        if on_speech_chunk and step.speech:
            on_speech_chunk(step.speech)
        if on_meta:
            on_meta(step.tokens)

        return ResponseResult(
            speech=step.speech, thoughts="",
            total_tokens=step.tokens,
            model_id=self._model_id,
            stop_reason="completed",
        )

    async def _do_tool_call_only(self, step: ToolCallOnly, on_speech_chunk, on_meta, cancel_event, on_tool_call=None):
        # Simulate the binary's internal retry loop -- asdaaas just sees a
        # long collect_response that returns empty or resolved speech.
        tool_id = str(uuid.uuid4())
        self._write_update({
            "sessionUpdate": "tool_call",
            "toolCallId": tool_id,
            "title": "mock_tool",
        })

        if on_tool_call:
            on_tool_call("mock_tool")

        # Block for retry_duration (simulating binary retries)
        if step.retry_duration > 0:
            try:
                await asyncio.sleep(step.retry_duration)
            except asyncio.CancelledError:
                raise TurnCancelled("cancelled during retry simulation")

        if cancel_event and cancel_event.is_set():
            raise TurnCancelled("cancel_event set during retry")

        # Tool completes
        self._write_update({
            "sessionUpdate": "tool_call_update",
            "toolCallId": tool_id,
            "status": "completed",
        })

        # Resolve with speech (may be empty -- that's the no_visible_content case)
        self._write_speech(step.resolve_speech, step.tokens)

        if on_speech_chunk and step.resolve_speech:
            on_speech_chunk(step.resolve_speech)
        if on_meta:
            on_meta(step.tokens)

        return ResponseResult(
            speech=step.resolve_speech, thoughts="",
            total_tokens=step.tokens,
            model_id=self._model_id,
            stop_reason="completed",
        )

    async def _do_doom_loop(self, step: DoomLoop, on_meta):
        self._write_update({
            "sessionUpdate": "doom_loop_detected",
            "exit_count": step.exit_count,
        })
        self._write_event({"type": "turn_ended", "outcome": "doom_loop"})
        self._total_tokens = step.tokens

        return ResponseResult(
            speech="", thoughts="",
            total_tokens=step.tokens,
            model_id=self._model_id,
            stop_reason="doom_loop",
        )

    async def _do_compaction(self, step: Compaction, on_meta):
        self._write_update({
            "sessionUpdate": "auto_compact_completed",
            "tokens_before": step.tokens_before,
            "tokens_after": step.tokens_after,
        })

        # Store for pop_compaction_event
        self._compaction_event = True
        self._compaction_tokens_before = step.tokens_before
        self._compaction_tokens_after = step.tokens_after
        self._total_tokens = step.tokens_after

        self._write_event({"type": "turn_ended", "outcome": "completed"})

        if on_meta:
            on_meta(step.tokens_after)

        return ResponseResult(
            speech="", thoughts="",
            total_tokens=step.tokens_after,
            model_id=self._model_id,
            stop_reason="completed",
        )

    async def _do_empty(self, step: EmptyResponse, on_meta):
        self._write_speech("", step.tokens)
        if on_meta:
            on_meta(step.tokens)

        return ResponseResult(
            speech="", thoughts="",
            total_tokens=step.tokens,
            model_id=self._model_id,
            stop_reason="completed",
        )

    async def _do_long_tool_call(self, step: LongToolCallResponse, on_speech_chunk, on_meta, cancel_event, on_tool_call=None):
        """Simulate a long-running tool call with periodic activity updates.

        Writes tool_call (Pending) → periodic tool_call_updates → tool_call (Completed) → speech.
        The binary is clearly busy the entire time (updates.jsonl has activity).

        When early_return_after > 0, simulates GrokBackend's wall_clock_timeout:
        collect_response returns early with tool calls still pending. The tool
        completes asynchronously after `duration` seconds. This reproduces the
        real Jr stale-continue bug where asdaaas iterates while tools are running.
        """
        if cancel_event and cancel_event.is_set():
            raise TurnCancelled("cancel_event set")

        tool_id = f"long_tool_{uuid.uuid4().hex[:8]}"

        # Write initial tool_call (Pending)
        self._pending_tool_calls.add(tool_id)
        self._write_update_with_meta({
            "sessionUpdate": "tool_call",
            "toolCallId": tool_id,
            "title": "wait_commands_or_subagents",
            "rawInput": {"task_ids": ["mock-subagent-001"], "mode": "wait_all", "timeout_ms": 600000},
        }, step.tokens)

        if on_tool_call:
            on_tool_call("wait_commands_or_subagents")

        if step.early_return_after > 0:
            # Simulate wall_clock_timeout: return early, tool still pending.
            # Schedule async completion after remaining duration.
            async def _complete_later():
                remaining = step.duration - step.early_return_after
                elapsed = 0.0
                while elapsed < remaining:
                    sleep_time = min(step.activity_interval, remaining - elapsed)
                    await asyncio.sleep(sleep_time)
                    elapsed += sleep_time
                    self._write_update_with_meta({
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": tool_id,
                        "status": "in_progress",
                        "title": f"Subagent still running ({step.early_return_after + elapsed:.0f}s elapsed)",
                    }, step.tokens)
                # Tool completes
                self._pending_tool_calls.discard(tool_id)
                self._write_update_with_meta({
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tool_id,
                    "status": "completed",
                    "title": "wait_commands_or_subagents completed",
                }, step.tokens)
                self._write_speech(step.speech, step.tokens)

            # Wait for early_return_after period, then return
            await asyncio.sleep(step.early_return_after)
            asyncio.get_event_loop().create_task(_complete_later())
            return ResponseResult(
                speech="", thoughts="",
                total_tokens=step.tokens,
                model_id=self._model_id,
                stop_reason="wall_clock_timeout",
            )

        # Original synchronous path: block for full duration
        elapsed = 0.0
        while elapsed < step.duration:
            sleep_time = min(step.activity_interval, step.duration - elapsed)
            try:
                await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                raise TurnCancelled("cancelled during long tool call")
            elapsed += sleep_time

            if cancel_event and cancel_event.is_set():
                raise TurnCancelled("cancel_event set during long tool call")

            self._write_update_with_meta({
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_id,
                "status": "in_progress",
                "title": f"Subagent still running ({elapsed:.0f}s elapsed)",
            }, step.tokens)

        # Tool completes
        self._pending_tool_calls.discard(tool_id)
        self._write_update_with_meta({
            "sessionUpdate": "tool_call_update",
            "toolCallId": tool_id,
            "status": "completed",
            "title": "wait_commands_or_subagents completed",
        }, step.tokens)

        self._write_speech(step.speech, step.tokens)

        if on_speech_chunk and step.speech:
            on_speech_chunk(step.speech)
        if on_meta:
            on_meta(step.tokens)

        return ResponseResult(
            speech=step.speech, thoughts="",
            total_tokens=step.tokens,
            model_id=self._model_id,
            stop_reason="completed",
        )

    async def _do_slow(self, step: SlowResponse, on_speech_chunk, on_meta, cancel_event):
        # Delay before producing speech (tests keepalive timeout)
        try:
            await asyncio.sleep(step.delay)
        except asyncio.CancelledError:
            raise TurnCancelled("cancelled during slow response")

        if cancel_event and cancel_event.is_set():
            raise TurnCancelled("cancel_event set during slow response")

        self._write_speech(step.speech, step.tokens)

        if on_speech_chunk and step.speech:
            on_speech_chunk(step.speech)
        if on_meta:
            on_meta(step.tokens)

        return ResponseResult(
            speech=step.speech, thoughts="",
            total_tokens=step.tokens,
            model_id=self._model_id,
            stop_reason="completed",
        )

    async def _do_split_command(self, step: SplitCommandWriter, on_speech_chunk, on_meta, cancel_event):
        if cancel_event and cancel_event.is_set():
            raise TurnCancelled("cancel_event set")

        if self._agent_cwd:
            cmd_dir = Path(self._agent_cwd) / "asdaaas" / "commands"
            cmd_dir.mkdir(parents=True, exist_ok=True)
            import secrets

            # Write early commands (e.g. delay:0) — these arrive before collect_response returns
            for cmd in step.early_commands:
                ts = int(time.time() * 1000)
                rand = secrets.token_hex(4)
                path = cmd_dir / f"cmd_{ts}_{rand}.json"
                with open(path, "w") as f:
                    json.dump(cmd, f)
                await asyncio.sleep(0.005)

            # Schedule late commands to arrive AFTER collect_response returns.
            # This reproduces the real race: agent's last delay command isn't on
            # disk when post-response drain runs.
            async def write_late_commands():
                await asyncio.sleep(step.delay_between)
                for cmd in step.late_commands:
                    ts = int(time.time() * 1000)
                    rand = secrets.token_hex(4)
                    path = cmd_dir / f"cmd_{ts}_{rand}.json"
                    with open(path, "w") as f:
                        json.dump(cmd, f)
                    await asyncio.sleep(0.005)

            # Fire-and-forget: late commands arrive after we return
            asyncio.create_task(write_late_commands())

        self._write_speech(step.speech, step.tokens)

        if on_speech_chunk and step.speech:
            on_speech_chunk(step.speech)
        if on_meta:
            on_meta(step.tokens)

        return ResponseResult(
            speech=step.speech, thoughts="",
            total_tokens=step.tokens,
            model_id=self._model_id,
            stop_reason="completed",
        )

    async def drain_stale(self) -> tuple[int, str]:
        return 0, ""

    async def request_compaction(self) -> bool:
        # If there's a Compaction step next, execute it
        if (self._step_index < len(self._scenario)
                and isinstance(self._scenario[self._step_index], Compaction)):
            step = self._scenario[self._step_index]
            self._step_index += 1
            await self._do_compaction(step, None)
            return True
        return False

    async def shutdown(self):
        pass

    @property
    def proc(self) -> None:
        return None

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def session_dir(self) -> Optional[Path]:
        return self._session_dir

    @property
    def model_id(self) -> str:
        return self._model_id

    def refresh_tokens(self) -> int:
        return self._total_tokens

    def pop_compaction_event(self) -> tuple[bool, Optional[int], int]:
        if self._compaction_event:
            after = self._compaction_tokens_after
            before = self._compaction_tokens_before
            self._compaction_event = None
            self._compaction_tokens_before = 0
            self._compaction_tokens_after = 0
            return True, after, before
        return False, None, 0

    @property
    def has_pending_tool_calls(self) -> bool:
        return bool(self._pending_tool_calls)

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

    # -- Test introspection helpers --

    @property
    def steps_remaining(self) -> int:
        return max(0, len(self._scenario) - self._step_index)

    @property
    def last_prompt(self) -> str:
        return self._last_prompt

    @property
    def prompt_count(self) -> int:
        return self._prompt_count

    @property
    def all_prompts(self) -> list[str]:
        return list(self._all_prompts)