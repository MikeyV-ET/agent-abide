"""
agent_backend.py — Abstract backend interface for agent LLM subprocess management.

asdaaas manages agents through backends. Each backend speaks a different
protocol (grok JSON-RPC, Claude Code NDJSON, etc.) but exposes the same
interface to asdaaas core.

What stays the same regardless of backend:
  - Gaze, awareness, doorbells, adapters, command watchdog
  - Health file updates
  - Attention structure
  - Pending queue
  - All adapter routing

What changes per backend:
  - Subprocess launch command
  - Wire protocol (how prompts are sent, how responses are parsed)
  - Session management
  - Token tracking format
  - Compaction mechanism
"""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional


class TurnCancelled(Exception):
    """Raised when an in-progress turn is cancelled via cancel_event."""
    pass


@dataclass
class ResponseResult:
    """Standardized response from any backend."""
    speech: str
    thoughts: str
    total_tokens: int       # cumulative tokens used (backend must track)
    model_id: str           # model identifier
    stop_reason: str        # why the turn ended
    cost_usd: float = 0.0  # per-turn cost (if available)


class AgentBackend(ABC):
    """Abstract backend for agent LLM subprocess management.
    
    Each backend wraps a subprocess (grok, claude, etc.) and translates
    between its native protocol and the standardized interface that
    asdaaas core uses.
    """

    @abstractmethod
    async def start(self, agent_cwd: str, model: Optional[str] = None,
                    session_id: Optional[str] = None) -> str:
        """Spawn subprocess, initialize, return session_id.
        
        Args:
            agent_cwd: Working directory for the agent
            model: Model override (backend-specific, e.g. "coding-mix-latest")
            session_id: Existing session to resume (None = new session)
            
        Returns:
            session_id (str) for the active session
        """

    @abstractmethod
    async def send_prompt(self, text: str) -> Any:
        """Send a prompt to the agent. Returns a handle for collect_response.
        
        The handle is backend-specific (e.g., JSON-RPC id for grok,
        None for Claude since NDJSON doesn't use request IDs).
        """

    @abstractmethod
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
        """Collect the agent's response after send_prompt.
        
        Args:
            handle: The handle returned by send_prompt
            on_speech_chunk: Called with each text chunk as it streams
            on_tool_call: Called with tool name when a tool starts
            on_meta: Called with cumulative token count during response
            keepalive_timeout: Seconds of silence before timing out
            max_wall_clock: Absolute maximum wait time
            cancel_event: If set, this event is checked each frame read cycle.
                When the event fires, TurnCancelled is raised immediately.
            
        Returns:
            ResponseResult with speech, thoughts, and metadata
        """

    @abstractmethod
    async def drain_stale(self) -> tuple[int, str]:
        """Drain any stale frames from stdout without blocking.
        
        Returns:
            (drained_count, recovered_speech) — speech recovered from
            stale frames, if any.
        """

    @abstractmethod
    async def request_compaction(self) -> bool:
        """Request context compaction.
        
        Returns True if compaction was triggered, False if not supported
        or not needed. Backends that manage their own context (like
        Claude Code) should return False.
        """

    @abstractmethod
    async def shutdown(self):
        """Clean shutdown of the subprocess."""

    @property
    @abstractmethod
    def proc(self) -> Optional[asyncio.subprocess.Process]:
        """The underlying subprocess (for health monitoring, PID, etc.)."""

    @property
    @abstractmethod
    def session_id(self) -> Optional[str]:
        """Current session ID."""

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Current model identifier."""

    @abstractmethod
    def refresh_tokens(self) -> int:
        """Read current token count from the backend's authoritative source.

        Called between turns to get fresh token data without sending a prompt.
        Each backend reads from its own source (e.g. updates.jsonl for grok,
        session API for Claude). Returns the updated total_tokens value.
        """

    def pop_compaction_event(self) -> tuple[bool, 'Optional[int]']:
        """Return (True, tokens_after) if compaction was detected since last check.

        Default returns (False, None). Backends that can detect compaction
        events (e.g. GrokBackend via auto_compact_completed) override this.
        """
        return False, None

    @property
    @abstractmethod
    def total_tokens(self) -> int:
        """Cumulative token count for the session."""

    @property
    @abstractmethod
    def context_window(self) -> int:
        """Context window size for the current model."""
