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

    async def deliver_turn(self, gathered: GatherResult, *,
                           cancel_event=None,
                           interjection_enabled: bool = False,
                           last_response_ts: float = None,
                           last_was_foreground: bool = True,
                           on_streaming_meta=None) -> 'DeliverResult | None':
        """Build prompt from gathered items and deliver to backend.

        Returns DeliverResult if there was content to deliver, or None
        if nothing was gathered (caller should handle idle path).

        Side effects: sends prompt to backend, writes conversation log,
        updates health, runs interjection watcher during response.
        """
        import asyncio
        import time
        from asdaaas import (
            format_doorbell, _is_midturn_message, _midturn_flag,
            context_left_tag, write_conversation, write_health,
            read_gaze, poll_adapter_inboxes, read_observer_state,
            MessageTimer, StreamingThoughts,
        )

        agent_name = self.agent_name
        bells = gathered.doorbells
        in_room_msgs = gathered.messages

        prompt_parts = []

        if bells:
            bell_lines = [format_doorbell(bell) for bell in bells]
            prompt_parts.extend(bell_lines)
            print(f"[asdaaas] Doorbells ({len(bells)}): {[b.get('id', '?') for b in bells]}")

        if in_room_msgs:
            obs_midturn = read_observer_state()
            for msg in in_room_msgs:
                sender = msg.get("from", "unknown")
                adapter = msg.get("adapter", "unknown")
                text = msg.get("text", "").strip()
                if obs_midturn is not None:
                    msg_ts = msg.get("_received_ts") or msg.get("ts")
                    if not isinstance(msg_ts, (int, float)):
                        midturn = False
                    elif obs_midturn.get("state") == "BUSY":
                        midturn = True
                    elif obs_midturn.get("state") == "IDLE":
                        midturn = msg_ts < obs_midturn.get("since", 0)
                    else:
                        midturn = False
                else:
                    midturn = _is_midturn_message(
                        msg, last_response_ts, last_was_foreground,
                        self.backend.last_activity_ts)
                flag = _midturn_flag(msg) if midturn else ""
                prompt_parts.append(f"<{sender} (via {adapter}){flag}> {text}")

        if not prompt_parts:
            return None

        # Build and send prompt
        self.total_tokens = self.backend.refresh_tokens()
        self.gaze = read_gaze(agent_name)
        prompt_text = "\n".join(prompt_parts) + context_left_tag(
            self.total_tokens, self.context_window,
            self.turns_since_compaction, gaze=self.gaze)

        has_bells = bool(bells)
        has_msgs = bool(in_room_msgs)
        if has_bells and has_msgs:
            print(f"[asdaaas] COALESCED: {len(bells)} doorbell(s) + {len(in_room_msgs)} message(s) in single prompt")
        elif has_msgs and len(in_room_msgs) > 1:
            print(f"[asdaaas] BATCH: {len(in_room_msgs)} messages coalesced into single prompt")

        msg_id = (in_room_msgs[-1] if in_room_msgs else bells[-1]).get(
            "id", f"t{int(time.time()*1000)}")
        timer = MessageTimer(agent_name, msg_id)
        print(f"[asdaaas] IN: {prompt_text[:120]}")

        await self.backend.drain_stale()
        timer.mark("prompt_sent")
        write_health(agent_name, "working",
                     f"processing {'coalesced' if has_bells and has_msgs else 'doorbells' if has_bells else 'prompt'}"
                     f" ({len(prompt_parts)} items)", self.total_tokens, self.context_window)
        msg_handle = await self.backend.send_prompt(prompt_text)
        write_conversation(agent_name, "user", prompt_text)

        # Streaming thoughts
        self.gaze = read_gaze(agent_name)
        st = StreamingThoughts(agent_name, self.gaze)

        # Interjection watcher
        _ij_watcher = None
        if interjection_enabled:
            from interjection import interjection_watcher
            _ij_watcher = asyncio.create_task(
                interjection_watcher(agent_name,
                                     lambda: poll_adapter_inboxes(agent_name, self.awareness),
                                     poll_interval=2.0))

        result = await self.backend.collect_response(
            msg_handle, on_meta=on_streaming_meta,
            on_speech_chunk=st.on_chunk,
            on_tool_call=st.on_tool_call,
            cancel_event=cancel_event)

        if _ij_watcher:
            _ij_watcher.cancel()
            try:
                await _ij_watcher
            except asyncio.CancelledError:
                pass

        timer.mark("prompt_complete")

        # Delivery receipt check
        if hasattr(self.backend, 'delivery_confirmed') and not self.backend.delivery_confirmed:
            print(f"[asdaaas] DELIVERY_FAILURE: agent={agent_name} prompt_len={len(prompt_text)} reason=no_user_message_chunk")
            write_health(agent_name, "active", "delivery_failure", self.total_tokens, self.context_window)

        self.total_tokens = self.backend.total_tokens
        self.turns_since_compaction += 1

        dr = DeliverResult()
        dr.speech = result.speech if result.speech else ""
        dr.thoughts = result.thoughts if hasattr(result, 'thoughts') and result.thoughts else ""
        dr.total_tokens = self.total_tokens
        # Store extras on result for post_turn to use
        dr._timer = timer
        dr._has_bells = has_bells
        dr._has_msgs = has_msgs
        dr._bells = bells
        dr._in_room_msgs = in_room_msgs
        dr._prompt_text = prompt_text

        return dr
