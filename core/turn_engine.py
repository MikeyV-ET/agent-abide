"""
turn_engine.py — Turn state machine for ASDAAAS.
=================================================
Decomposes the main() loop into testable phase methods.
Each phase operates on explicit state rather than closures
over main()'s local variables.

Phase cycle per turn:
  1. gather_pending() → GatherResult
  2. deliver_turn(gathered) → DeliverResult
  3. post_turn(deliver_result) → PostTurnResult

Special paths:
  - handle_compaction() — agent-initiated or auto-compaction
  - handle_orientation() — post-compaction first turn
  - handle_idle() — delay/sleep logic
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from asdaaas_env import AsdaaasEnv

if TYPE_CHECKING:
    from asdaaas import CommandWatchdog, PendingQueue


@dataclass
class GatherResult:
    """What was collected during the gather phase."""
    doorbells: list = field(default_factory=list)
    messages: list = field(default_factory=list)       # in-room adapter messages
    bg_doorbells: list = field(default_factory=list)    # background-mode messages
    commands: list = field(default_factory=list)
    pending_msgs: list = field(default_factory=list)    # from pending queue
    timeout_bells: list = field(default_factory=list)   # attention timeouts
    has_content: bool = False  # True if anything was gathered worth sending


@dataclass
class DeliverResult:
    """What came back from delivering a turn."""
    speech: str = ""
    thoughts: str = ""
    total_tokens: int = 0
    interjections_delivered: int = 0
    tool_calls: int = 0


@dataclass
class PostTurnResult:
    """What happened during post-turn processing."""
    commands_processed: list = field(default_factory=list)
    health_written: bool = False
    continue_queued: bool = False
    interjections_drained: int = 0


class TurnEngine:
    """Turn state machine for one asdaaas agent.

    Holds per-agent state that persists across turns. Phase methods
    operate on this state and return typed results for test inspection.
    """

    def __init__(self, env: AsdaaasEnv, agent_name: str, backend,
                 context_window: int = 200000,
                 watchdog: 'CommandWatchdog | None' = None,
                 pending_queue: 'PendingQueue | None' = None):
        self.env = env
        self.agent_name = agent_name
        self.backend = backend
        self.context_window = context_window
        self.watchdog = watchdog
        self.pending_queue = pending_queue

        # Per-session state
        self.total_tokens: int = 0
        self.turns_since_compaction: int = 2  # start as "available"
        self.next_turn_delay: float = 0
        self.delay_until_event: bool = False
        self.delay_text: Optional[str] = None

        # Gaze and awareness (loaded from disk each turn)
        self.gaze: dict = {}
        self.awareness: dict = {}

        # Doorbell suppression tracking
        self.last_delivered_bell_ids: set = set()

    def agent_dir(self) -> Path:
        """Per-agent asdaaas directory."""
        return self.env.agents_home / self.agent_name / "asdaaas"

    async def gather_pending(self) -> GatherResult:
        """Gather all pending doorbells, messages, and commands.

        Polls doorbells (with suppression), adapter inboxes, attention
        timeouts, and pending queue. Classifies messages into in-room
        vs background. Updates self.gaze, self.awareness, and
        self.last_delivered_bell_ids as side effects.

        Attention timeouts and responses are delivered directly to the
        backend's stdin during gather (async side effects).
        """
        from asdaaas import (
            read_awareness, read_gaze, poll_doorbells, poll_attentions,
            check_attention_timeouts, poll_adapter_inboxes, poll_inbox,
            match_attention, resolve_attention, matches_gaze,
            get_background_mode,
        )

        agent_name = self.agent_name

        # Refresh awareness and gaze from disk
        self.awareness = read_awareness(agent_name)
        self.gaze = read_gaze(agent_name)

        result = GatherResult()

        # 2a. Poll doorbells with suppression (issue_0039)
        all_bells = poll_doorbells(agent_name, self.awareness)
        if self.last_delivered_bell_ids:
            still_pending = {b.get("id") for b in all_bells} & self.last_delivered_bell_ids
            bells = [b for b in all_bells if b.get("id") not in self.last_delivered_bell_ids]
            if still_pending:
                print(f"[asdaaas] Suppressed {len(still_pending)} un-acked bell(s): "
                      f"{list(still_pending)}")
            self.last_delivered_bell_ids = still_pending
        else:
            bells = all_bells

        if bells:
            for bell in bells:
                bell_req_id = bell.get("request_id", "")
                if bell_req_id and self.watchdog:
                    self.watchdog.acknowledge(bell_req_id)

        result.doorbells = bells

        # 2b. Poll attentions + timeouts + pending queue
        attentions = poll_attentions(agent_name)
        timeout_bells = check_attention_timeouts(agent_name, attentions)
        if timeout_bells:
            for tb in timeout_bells:
                self.backend.proc.stdin.write((tb["text"] + "\n").encode())
                await self.backend.proc.stdin.drain()
                print(f"[asdaaas] ATTENTION TIMEOUT delivered to {agent_name}: {tb['msg_id']}")
            attentions = poll_attentions(agent_name)
        result.timeout_bells = timeout_bells

        pending_msgs = []
        if self.pending_queue:
            pending_msgs = self.pending_queue.drain_for_gaze(self.gaze)
            if pending_msgs:
                print(f"[asdaaas] PENDING: delivering {len(pending_msgs)} queued message(s) (gaze matched)")
        result.pending_msgs = pending_msgs

        messages = poll_adapter_inboxes(agent_name, self.awareness)
        legacy_msgs = poll_inbox(agent_name)
        messages.extend(legacy_msgs)
        messages = pending_msgs + messages

        # 2c. Classify: in-room vs background
        in_room_msgs = []
        bg_doorbell_msgs = []

        for msg in messages:
            text = msg.get("text", "").strip()
            sender = msg.get("from", "unknown")
            adapter = msg.get("adapter", "unknown")

            if not text:
                continue

            # Attention matching (higher priority than gaze filtering)
            if attentions:
                matched_attn = match_attention(agent_name, attentions, sender)
                if matched_attn:
                    response_bell = resolve_attention(matched_attn, text)
                    response_text = response_bell["text"]
                    self.backend.proc.stdin.write((response_text + "\n").encode())
                    await self.backend.proc.stdin.drain()
                    print(f"[asdaaas] ATTENTION RESPONSE delivered to {agent_name}: {matched_attn['msg_id']}")
                    attentions = poll_attentions(agent_name)
                    continue

            # Gaze filtering
            self.gaze = read_gaze(agent_name)

            if not matches_gaze(msg, self.gaze):
                mode = get_background_mode(msg, self.awareness)

                if mode == "drop":
                    print(f"[asdaaas] DROP: {sender} (via {adapter}) -- not in gaze room")
                    continue
                elif mode == "pending":
                    if self.pending_queue:
                        self.pending_queue.add(msg)
                    print(f"[asdaaas] PENDING: queued {sender} (via {adapter})"
                          f" -- {self.pending_queue.total if self.pending_queue else '?'} total pending")
                    continue
                else:  # doorbell
                    bg_doorbell_msgs.append(msg)
                    continue

            in_room_msgs.append(msg)

        result.messages = in_room_msgs
        result.bg_doorbells = bg_doorbell_msgs
        result.has_content = bool(bells or in_room_msgs or bg_doorbell_msgs)

        return result
