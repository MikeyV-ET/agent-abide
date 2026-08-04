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
    commands_processed: int = 0
    commands_requeued: int = 0
    interjections_drained: int = 0
    agent_wrote_delay: bool = False
    speech_delivered: bool = False


@dataclass
class IdleResult:
    """What happened during idle/delay/continue handling."""
    action: str = "continue"  # "continue" (loop back), "sleep" (idle poll)
    delay_interrupted: bool = False
    continue_queued: bool = False
    observer_state: Optional[str] = None


class TurnEngine:
    """Turn state machine for one asdaaas agent.

    Holds per-agent state that persists across turns. Phase methods
    operate on this state and return typed results for test inspection.
    """

    def __init__(self, env: AsdaaasEnv, agent_name: str, backend,
                 context_window: int = 200000,
                 watchdog: 'CommandWatchdog | None' = None,
                 pending_queue: 'PendingQueue | None' = None,
                 observer_state_file: Optional[str] = None,
                 compaction_threshold: Optional[float] = None):
        self.env = env
        self.agent_name = agent_name
        self.backend = backend
        self.context_window = context_window
        self.compaction_threshold = compaction_threshold
        self.watchdog = watchdog
        self.pending_queue = pending_queue
        self.observer_state_file = observer_state_file

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

        # Post-turn state
        self.last_response_ts: float = None
        self.last_was_foreground: bool = True
        self.consecutive_empty_doorbell: int = 0
        self.interjection_enabled: bool = False

        # Compaction state
        self._prev_tokens: int = 0
        self.compact_pending = None
        self.compact_pending_turns: int = 0

        # Reasoning effort (set by main loop, passed to context_left_tag)
        self.reasoning_effort_info: Optional[tuple] = None  # (level, turns_remaining) or None

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
        self.awareness = read_awareness(agent_name, env=self.env)
        self.gaze = read_gaze(agent_name, env=self.env)

        result = GatherResult()

        # 2a. Poll doorbells with suppression (issue_0039)
        all_bells = poll_doorbells(agent_name, self.awareness, env=self.env)
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
        attentions = poll_attentions(agent_name, env=self.env)
        timeout_bells = check_attention_timeouts(agent_name, attentions, env=self.env)
        if timeout_bells:
            for tb in timeout_bells:
                self.backend.proc.stdin.write((tb["text"] + "\n").encode())
                await self.backend.proc.stdin.drain()
                print(f"[asdaaas] ATTENTION TIMEOUT delivered to {agent_name}: {tb['msg_id']}")
            attentions = poll_attentions(agent_name, env=self.env)
        result.timeout_bells = timeout_bells

        pending_msgs = []
        if self.pending_queue:
            pending_msgs = self.pending_queue.drain_for_gaze(self.gaze)
            if pending_msgs:
                print(f"[asdaaas] PENDING: delivering {len(pending_msgs)} queued message(s) (gaze matched)")
        result.pending_msgs = pending_msgs

        messages = poll_adapter_inboxes(agent_name, self.awareness, env=self.env)
        legacy_msgs = poll_inbox(agent_name, env=self.env)
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
                    attentions = poll_attentions(agent_name, env=self.env)
                    continue

            # Gaze filtering
            self.gaze = read_gaze(agent_name, env=self.env)

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
            read_gaze, poll_adapter_inboxes,
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
            obs_midturn = self.read_observer_state()
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
                        msg, self.last_response_ts, self.last_was_foreground,
                        self.backend.last_activity_ts)
                flag = _midturn_flag(msg) if midturn else ""
                prompt_parts.append(f"<{sender} (via {adapter}){flag}> {text}")

        if not prompt_parts:
            return None

        # Build and send prompt
        self.total_tokens = self.backend.refresh_tokens()
        self.gaze = read_gaze(agent_name, env=self.env)
        prompt_text = "\n".join(prompt_parts) + context_left_tag(
            self.total_tokens, self.context_window,
            self.turns_since_compaction, gaze=self.gaze,
            reasoning_effort_info=self.reasoning_effort_info,
            compaction_threshold=self.compaction_threshold)

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
                     f" ({len(prompt_parts)} items)", self.total_tokens, self.context_window, env=self.env)
        msg_handle = await self.backend.send_prompt(prompt_text)
        write_conversation(agent_name, "user", prompt_text, env=self.env)

        # Streaming thoughts
        self.gaze = read_gaze(agent_name, env=self.env)
        st = StreamingThoughts(agent_name, self.gaze)

        # Interjection watcher
        _ij_watcher = None
        if interjection_enabled:
            from interjection import interjection_watcher
            _ij_watcher = asyncio.create_task(
                interjection_watcher(agent_name,
                                     lambda: poll_adapter_inboxes(agent_name, self.awareness, env=self.env),
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
            write_health(agent_name, "active", "delivery_failure", self.total_tokens, self.context_window, env=self.env)

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

    async def post_turn(self, deliver_result: DeliverResult) -> PostTurnResult:
        """Process post-response commands, route speech, handle empty responses.

        Drains commands written during the response (ack, delay),
        cleans up continue doorbells, drains interjection queue,
        routes speech/thoughts to outbox, tracks empty response
        backoff and doom loop detection.

        Updates self.next_turn_delay, self.delay_until_event,
        self.delay_text, self.last_response_ts, self.last_was_foreground,
        self.consecutive_empty_doorbell.
        """
        import asyncio
        import json
        import os
        import tempfile
        import time
        from asdaaas import (
            poll_commands, ack_doorbells, agent_dir,
            _cleanup_continue_doorbells, queue_continue_doorbell,
            write_conversation, write_to_outbox, write_health,
            write_profile, read_gaze,
            EMPTY_DOORBELL_BACKOFF_AFTER, EMPTY_DOORBELL_BACKOFF_PER,
            EMPTY_DOORBELL_BACKOFF_MAX, CONTINUE_DOOM_CHECK_AFTER,
            CONTINUE_MAX_CONSECUTIVE,
        )

        agent_name = self.agent_name
        ptr = PostTurnResult()

        timer = deliver_result._timer
        has_bells = deliver_result._has_bells
        has_msgs = deliver_result._has_msgs
        bells = deliver_result._bells
        in_room_msgs = deliver_result._in_room_msgs

        # ---- Post-response command drain ----
        post_cmds = poll_commands(agent_name, env=self.env)
        requeue = []
        for pc in post_cmds:
            pa = pc.get("action", "")
            piggy = pc.get("ack", [])
            if piggy:
                ack_doorbells(agent_name, piggy, env=self.env)
            if pa == "ack":
                ack_doorbells(agent_name, pc.get("handled", []), env=self.env)
            elif pa == "delay":
                dv = pc.get("seconds", 0)
                self.delay_text = pc.get("text") or None
                if dv == "until_event":
                    self.delay_until_event = True
                    self.next_turn_delay = 0
                else:
                    self.next_turn_delay = float(dv)
                    self.delay_until_event = False
                ptr.agent_wrote_delay = True
            elif pa in ("compact", "gaze", "awareness", "reasoning_effort"):
                requeue.append(pc)

        if requeue:
            cmd_dir = agent_dir(agent_name, env=self.env) / "commands"
            cmd_dir.mkdir(parents=True, exist_ok=True)
            for rc in requeue:
                fd, tmp = tempfile.mkstemp(dir=str(cmd_dir), suffix=".json", prefix="cmd_requeue_")
                with os.fdopen(fd, "w") as f:
                    json.dump(rc, f)

        ptr.commands_processed = len(post_cmds) - len(requeue)
        ptr.commands_requeued = len(requeue)
        if ptr.commands_processed:
            print(f"[asdaaas] Post-response: drained {ptr.commands_processed} command(s)" +
                  (f", requeued {ptr.commands_requeued}" if ptr.commands_requeued else ""))

        requeue = []
        _cleanup_continue_doorbells(agent_name, env=self.env)

        # ---- Late command poll ----
        if not self.delay_until_event and self.next_turn_delay == 0:
            await asyncio.sleep(0.5)
            late_cmds = poll_commands(agent_name, env=self.env)
            for lc in late_cmds:
                la = lc.get("action", "")
                lpiggy = lc.get("ack", [])
                if lpiggy:
                    ack_doorbells(agent_name, lpiggy, env=self.env)
                if la == "delay":
                    ldv = lc.get("seconds", 0)
                    self.delay_text = lc.get("text") or None
                    if ldv == "until_event":
                        self.delay_until_event = True
                        self.next_turn_delay = 0
                    else:
                        self.next_turn_delay = float(ldv)
                        self.delay_until_event = False
                    ptr.agent_wrote_delay = True
                elif la == "ack":
                    ack_doorbells(agent_name, lc.get("handled", []), env=self.env)
                elif la in ("compact", "gaze", "awareness", "reasoning_effort"):
                    requeue.append(lc)
            if late_cmds:
                print(f"[asdaaas] Late command poll: {len(late_cmds)} command(s)")
                if requeue:
                    cmd_dir = agent_dir(agent_name, env=self.env) / "commands"
                    cmd_dir.mkdir(parents=True, exist_ok=True)
                    for rc in requeue:
                        fd, tmp = tempfile.mkstemp(dir=str(cmd_dir), suffix=".json", prefix="cmd_requeue_")
                        with os.fdopen(fd, "w") as f:
                            json.dump(rc, f)

        # ---- Interjection drain ----
        if self.interjection_enabled:
            from interjection import drain_interjection_queue
            leftover = drain_interjection_queue(agent_name, env=self.env)
            if leftover:
                # Always write unique doorbells — queue_continue_doorbell silently
                # drops text when cont_* already exists (Squiggy interjection loss).
                from asdaaas import queue_interjection_doorbell
                for msg_text in leftover:
                    queue_interjection_doorbell(agent_name, msg_text, env=self.env)
                ptr.interjections_drained = len(leftover)
                print(f"[asdaaas] Drained {len(leftover)} leftover interjection(s) → doorbells")

        # ---- Foreground tracking ----
        self.last_was_foreground = has_msgs

        # ---- Speech routing / empty response handling ----
        self.gaze = read_gaze(agent_name, env=self.env)

        if deliver_result.speech.strip():
            self.last_response_ts = time.time()
            self.consecutive_empty_doorbell = 0
            write_conversation(agent_name, "assistant", deliver_result.speech, env=self.env)
            if deliver_result.thoughts.strip():
                write_conversation(agent_name, "thinking", deliver_result.thoughts, env=self.env)
            write_to_outbox(agent_name, deliver_result.speech.strip(),
                           self.gaze.get("speech"), "speech", env=self.env)
            timer.mark("outbox_done")
            if (deliver_result.thoughts.strip() and self.gaze.get("thoughts")
                    and deliver_result.thoughts.strip() != deliver_result.speech.strip()):
                write_to_outbox(agent_name, deliver_result.thoughts.strip(),
                               self.gaze.get("thoughts"), "thoughts", env=self.env)
            print(timer.log_line())
            write_profile(agent_name, timer, env=self.env)
            detail = f"responded {len(deliver_result.speech)} chars"
            if has_bells and has_msgs:
                detail = (f"coalesced response ({len(bells)} bells + "
                          f"{len(in_room_msgs)} msgs), {len(deliver_result.speech)} chars")
            write_health(agent_name, "active", detail, self.total_tokens,
                        self.context_window, observer_state=self.read_observer_state(), env=self.env)
            # After responding to a user message with speech, default to
            # waiting unless agent already wrote an explicit delay (issue_0030).
            if has_msgs and not ptr.agent_wrote_delay:
                self.delay_until_event = True
            ptr.speech_delivered = True
        else:
            # Empty response handling
            if has_bells and not has_msgs:
                self.consecutive_empty_doorbell += 1
                _cleanup_continue_doorbells(agent_name, env=self.env)
                print(f"[asdaaas] {agent_name} doorbell -> (empty) "
                      f"[consecutive={self.consecutive_empty_doorbell}]")
                write_health(agent_name, "active",
                            f"empty doorbell response (x{self.consecutive_empty_doorbell})",
                            self.total_tokens, self.context_window, env=self.env)

                if self.consecutive_empty_doorbell >= EMPTY_DOORBELL_BACKOFF_AFTER:
                    backoff = min(EMPTY_DOORBELL_BACKOFF_PER * self.consecutive_empty_doorbell,
                                 EMPTY_DOORBELL_BACKOFF_MAX)
                    print(f"[asdaaas] Backoff: {backoff}s after "
                          f"{self.consecutive_empty_doorbell} consecutive empty doorbell responses")
                    self.next_turn_delay = backoff

                # Doom loop check (observer-only, Phase 5)
                obs_doom = self.read_observer_state()
                if obs_doom is not None and obs_doom.get("doom_loop"):
                    print(f"[asdaaas] *** OBSERVER: DOOM LOOP DETECTED for {agent_name} ***")
                    print(f"[asdaaas]   Stopping continues.")
                    self.delay_until_event = True
                    write_health(agent_name, "stalled", "observer_doom_loop_detected",
                                self.total_tokens, self.context_window, env=self.env)
                    try:
                        from localmail import send_mail
                        send_mail(from_agent="asdaaas", to_agent="Sr",
                                 text=f"DOOM LOOP (observer): {agent_name} doom loop detected. Stopping continues.")
                    except Exception:
                        pass


                # Hard cap
                if self.consecutive_empty_doorbell >= CONTINUE_MAX_CONSECUTIVE:
                    print(f"[asdaaas] *** CONTINUE CAP ({CONTINUE_MAX_CONSECUTIVE}) hit for {agent_name} ***")
                    print(f"[asdaaas]   Stopping continues. Agent may be stuck.")
                    self.delay_until_event = True
                    write_health(agent_name, "stalled",
                                f"continue_cap_hit ({self.consecutive_empty_doorbell})",
                                self.total_tokens, self.context_window, env=self.env)
                    try:
                        from localmail import send_mail
                        send_mail(from_agent="asdaaas", to_agent="Sr",
                                 text=f"CONTINUE CAP: {agent_name} hit {self.consecutive_empty_doorbell} "
                                      f"consecutive empty responses. Stopped continues. Agent may be stuck.")
                    except Exception:
                        pass
            else:
                print(f"[asdaaas] {agent_name} -> (empty)")
                print(timer.log_line())
                write_profile(agent_name, timer, env=self.env)
                write_health(agent_name, "active", "empty response",
                            self.total_tokens, self.context_window, env=self.env)

        return ptr

    async def handle_compaction_detection(self, *,
                                          on_streaming_meta=None) -> bool:
        """Detect and handle auto/event compaction + orientation turn.

        Returns True if compaction was detected and orientation was sent
        (caller should 'continue' to next iteration). Returns False if
        no compaction detected (caller proceeds normally).

        Updates self.total_tokens, self.turns_since_compaction,
        self._prev_tokens, self.compact_pending.
        """
        import asyncio
        from asdaaas import (
            read_gaze, context_left_tag, write_to_outbox,
            write_health,
            poll_adapter_inboxes, poll_inbox,
        )

        agent_name = self.agent_name

        # Event-based detection (observer-only, Phase 5)
        compaction_event, event_tokens, event_tokens_before = self.backend.pop_compaction_event()

        if not (compaction_event and self.turns_since_compaction > 0):
            self._prev_tokens = self.total_tokens
            return False

        tokens_before = event_tokens_before or self._prev_tokens
        tokens_after = event_tokens or self.total_tokens
        print(f"[asdaaas] Compaction detected: {tokens_before} -> {tokens_after}")

        self.turns_since_compaction = 0
        self.compact_pending = None
        self.compact_pending_turns = 0
        self.total_tokens = tokens_after
        self._prev_tokens = self.total_tokens

        # Drain pending messages before orientation
        if self.interjection_enabled:
            held_msgs = poll_inbox(agent_name, env=self.env)
            if held_msgs:
                print(f"[asdaaas] Holding {len(held_msgs)} internal message(s) until after orientation"
                      " (adapter msgs left for interjection watcher)")
                if self.pending_queue:
                    for hm in held_msgs:
                        self.pending_queue.enqueue(hm)
        else:
            awareness = self.awareness or {}
            held_msgs = poll_adapter_inboxes(agent_name, awareness, env=self.env)
            held_msgs.extend(poll_inbox(agent_name, env=self.env))
            if held_msgs:
                print(f"[asdaaas] Holding {len(held_msgs)} message(s) until after orientation")
                if self.pending_queue:
                    for hm in held_msgs:
                        self.pending_queue.enqueue(hm)

        self.gaze = read_gaze(agent_name, env=self.env)
        orientation_text = (
            f"[Compaction complete. Context reduced from {tokens_before} to {tokens_after} tokens. "
            f"You are resuming from a compacted context. Follow your boot protocol.]"
            + context_left_tag(tokens_after, self.context_window,
                              self.turns_since_compaction, gaze=self.gaze,
                              compaction_threshold=self.compaction_threshold)
        )
        print(f"[asdaaas] Immediate orientation turn for {agent_name}")
        write_health(agent_name, "working", "post-compaction orientation",
                    tokens_after, self.context_window,
                    observer_state=self.read_observer_state(), env=self.env)
        await self.backend.drain_stale()
        orient_handle = await self.backend.send_prompt(orientation_text)

        # Interjection watcher for orientation turn
        _ij_orient = None
        if self.interjection_enabled:
            from interjection import interjection_watcher
            awareness = self.awareness or {}
            _ij_orient = asyncio.create_task(
                interjection_watcher(agent_name,
                                     lambda: poll_adapter_inboxes(agent_name, awareness, env=self.env),
                                     poll_interval=2.0))

        orient_result = await self.backend.collect_response(
            orient_handle, on_meta=on_streaming_meta,
            keepalive_timeout=60.0, max_wall_clock=300.0)

        if _ij_orient:
            _ij_orient.cancel()
            try:
                await _ij_orient
            except asyncio.CancelledError:
                pass

        self.total_tokens = self.backend.total_tokens
        self._prev_tokens = self.total_tokens
        if orient_result.speech.strip():
            write_to_outbox(agent_name, orient_result.speech.strip(),
                           self.gaze.get("speech"), "speech", env=self.env)

        # Wake agent for next turn after orientation
        from asdaaas import queue_continue_doorbell
        queue_continue_doorbell(agent_name, env=self.env)

        return True

    async def handle_compact_command(self, cmd: dict, *,
                                     on_streaming_meta=None) -> None:
        """Handle agent-initiated compact command.

        Checks cooldown, sends /compact, polls for async completion,
        sends orientation probe or queues doorbell.
        """
        import asyncio
        import json
        import os
        import tempfile
        import time
        from asdaaas import (
            agent_dir, read_gaze, context_left_tag, write_to_outbox,
            write_health, get_compaction_instructions,
            _cleanup_compact_doorbells, _queue_post_compaction_doorbell,
            COMPACTION_COOLDOWN_TURNS,
        )

        agent_name = self.agent_name
        request_id = cmd.get("request_id", "")

        if self.turns_since_compaction < COMPACTION_COOLDOWN_TURNS:
            print(f"[asdaaas] Compact rejected: cooldown ({self.turns_since_compaction} turns since last compaction)")
            bell_dir = agent_dir(agent_name, env=self.env) / "doorbells"
            bell_dir.mkdir(parents=True, exist_ok=True)
            bell = {
                "adapter": "session",
                "command": "compact",
                "priority": 3,
                "text": (f"Compaction rejected: cooldown active ({self.turns_since_compaction} turn(s) "
                         f"since last compaction). Wait "
                         f"{COMPACTION_COOLDOWN_TURNS - self.turns_since_compaction} more turn(s)."),
                "request_id": request_id,
                "ts": time.time(),
            }
            fd, tmp_path = tempfile.mkstemp(dir=str(bell_dir), suffix=".tmp", prefix="cpt_")
            with os.fdopen(fd, "w") as f:
                json.dump(bell, f)
            os.rename(tmp_path, tmp_path.replace(".tmp", ".json"))
            return

        if self.compact_pending:
            self.compact_pending = None
            self.compact_pending_turns = 0
            _cleanup_compact_doorbells(agent_name, env=self.env)
        print(f"[asdaaas] Compact: executing immediately for {agent_name}")

        try:
            tokens_before = self.total_tokens
            instructions = cmd.get("instructions") or get_compaction_instructions(agent_name, env=self.env)
            compact_prompt = f"/compact {instructions}"
            compact_handle = await self.backend.send_prompt(compact_prompt)
            compact_result = await self.backend.collect_response(
                compact_handle, keepalive_timeout=180.0, max_wall_clock=300.0)
            self.total_tokens = self.backend.total_tokens

            if self.total_tokens >= tokens_before:
                # Async — poll for token drop
                print(f"[asdaaas] Compact pending: {tokens_before} -> {self.total_tokens} "
                      "(polling for async completion)")
                compaction_landed = False
                for _poll in range(15):
                    await asyncio.sleep(2)
                    self.total_tokens = self.backend.refresh_tokens()
                    if self.total_tokens < tokens_before * 0.6:
                        compaction_landed = True
                        break
                if compaction_landed:
                    _, event_ta, event_tb = self.backend.pop_compaction_event()
                    tokens_before = event_tb or tokens_before
                    self.total_tokens = event_ta or self.total_tokens
                    print(f"[asdaaas] Compact completed (async): {tokens_before} -> {self.total_tokens}")
                    self._prev_tokens = self.total_tokens
                    self.turns_since_compaction = 0
                    _queue_post_compaction_doorbell(agent_name, tokens_before, self.total_tokens, env=self.env)
                else:
                    _, event_ta, event_tb = self.backend.pop_compaction_event()
                    tokens_before = event_tb or tokens_before
                    self.total_tokens = event_ta or self.total_tokens
                    print(f"[asdaaas] Compact still pending after 30s poll — queueing doorbell anyway")
                    _queue_post_compaction_doorbell(agent_name, tokens_before, self.total_tokens, env=self.env)
                    self.turns_since_compaction = 0
                    self._prev_tokens = self.total_tokens
            else:
                _, event_ta, event_tb = self.backend.pop_compaction_event()
                tokens_before = event_tb or tokens_before
                self.total_tokens = event_ta or self.total_tokens
                self.gaze = read_gaze(agent_name, env=self.env)
                probe_text = (
                    f"[Compaction complete. Context reduced from {tokens_before} to {self.total_tokens} tokens. "
                    f"You are resuming from a compacted context. Follow your boot protocol.]"
                    + context_left_tag(self.total_tokens, self.context_window, 0, gaze=self.gaze,
                                      compaction_threshold=self.compaction_threshold)
                )
                await self.backend.drain_stale()
                probe_handle = await self.backend.send_prompt(probe_text)
                probe_result = await self.backend.collect_response(
                    probe_handle, on_meta=on_streaming_meta,
                    keepalive_timeout=60.0, max_wall_clock=300.0)
                self.total_tokens = self.backend.total_tokens
                print(f"[asdaaas] Compact probe: real totalTokens={self.total_tokens}")
                if probe_result.speech.strip():
                    write_to_outbox(agent_name, probe_result.speech.strip(),
                                   self.gaze.get("speech"), "speech", env=self.env)
                self._prev_tokens = self.total_tokens
                self.turns_since_compaction = 0
                result_file = agent_dir(agent_name, env=self.env) / "command_result.json"
                tmp = str(result_file) + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({
                        "request_id": request_id,
                        "action": "compact",
                        "before": tokens_before,
                        "after": self.total_tokens,
                        "ts": time.time(),
                    }, f)
                os.rename(tmp, str(result_file))
                print(f"[asdaaas] Compact: {tokens_before} -> {self.total_tokens}")
                write_health(agent_name, "ready",
                            f"compacted {tokens_before}->{self.total_tokens}",
                            self.total_tokens, self.context_window, env=self.env)
                _cleanup_compact_doorbells(agent_name, env=self.env)
        except Exception as e:
            print(f"[asdaaas] Compact failed: {e}")

    async def handle_force_compact_command(self, cmd: dict, *,
                                           on_streaming_meta=None) -> None:
        """Handle operator force_compact command. Skips cooldown."""
        import asyncio
        import time
        from asdaaas import (
            read_gaze, context_left_tag, write_to_outbox, write_health,
            get_compaction_instructions,
            _cleanup_compact_doorbells, COMPACTION_COOLDOWN_TURNS,
        )

        agent_name = self.agent_name

        if self.turns_since_compaction < COMPACTION_COOLDOWN_TURNS:
            print(f"[asdaaas] Force compact: overriding cooldown ({self.turns_since_compaction} turns)")
        if self.compact_pending:
            print(f"[asdaaas] Force compact: clearing pending confirmation")
            self.compact_pending = None
            self.compact_pending_turns = 0
        print(f"[asdaaas] Force compact: executing immediately for {agent_name}")

        try:
            tokens_before = self.total_tokens
            instructions = cmd.get("instructions") or get_compaction_instructions(agent_name, env=self.env)
            compact_prompt = f"/compact {instructions}"
            compact_handle = await self.backend.send_prompt(compact_prompt)
            compact_result = await self.backend.collect_response(
                compact_handle, keepalive_timeout=180.0, max_wall_clock=300.0)
            self.total_tokens = self.backend.total_tokens

            if self.total_tokens >= tokens_before:
                print(f"[asdaaas] Force compact pending: {tokens_before} -> {self.total_tokens} (no reduction yet)")
                self._prev_tokens = self.total_tokens
            else:
                _, event_ta, event_tb = self.backend.pop_compaction_event()
                tokens_before = event_tb or tokens_before
                self.total_tokens = event_ta or self.total_tokens
                self.gaze = read_gaze(agent_name, env=self.env)
                probe_text = (
                    f"[Compaction complete. Context reduced from {tokens_before} to {self.total_tokens} tokens. "
                    f"You are resuming from a compacted context. Follow your boot protocol.]"
                    + context_left_tag(self.total_tokens, self.context_window, 0, gaze=self.gaze,
                                      compaction_threshold=self.compaction_threshold)
                )
                await self.backend.drain_stale()
                probe_handle = await self.backend.send_prompt(probe_text)
                probe_result = await self.backend.collect_response(
                    probe_handle, on_meta=on_streaming_meta,
                    keepalive_timeout=60.0, max_wall_clock=300.0)
                self.total_tokens = self.backend.total_tokens
                print(f"[asdaaas] Force compact probe: real totalTokens={self.total_tokens}")
                if probe_result.speech.strip():
                    write_to_outbox(agent_name, probe_result.speech.strip(),
                                   self.gaze.get("speech"), "speech", env=self.env)
                self._prev_tokens = self.total_tokens
                self.turns_since_compaction = 0
                _cleanup_compact_doorbells(agent_name, env=self.env)
                write_health(agent_name, "ready",
                            f"force-compacted {tokens_before}->{self.total_tokens}",
                            self.total_tokens, self.context_window, env=self.env)
                print(f"[asdaaas] Force compact: {tokens_before} -> {self.total_tokens}")
        except Exception as e:
            print(f"[asdaaas] Force compact failed: {e}")

    async def deliver_background_doorbells(self, bg_doorbells: list, *,
                                           cancel_event=None,
                                           on_streaming_meta=None) -> int:
        """Deliver background doorbell messages one at a time.

        Each message gets its own prompt+response cycle.
        Returns the number of doorbells delivered.
        """
        import time
        from asdaaas import (
            read_gaze, format_background_doorbell, context_left_tag,
            write_to_outbox, write_health,
        )

        agent_name = self.agent_name
        delivered = 0

        for msg in bg_doorbells:
            self.gaze = read_gaze(agent_name, env=self.env)
            bell_text = (format_background_doorbell(msg, agent_name=agent_name)
                        + context_left_tag(self.total_tokens, self.context_window,
                                          self.turns_since_compaction, gaze=self.gaze,
                                          compaction_threshold=self.compaction_threshold))
            print(f"[asdaaas] BACKGROUND: {bell_text[:120]}")
            await self.backend.drain_stale()
            write_health(agent_name, "working", "processing background doorbell",
                        self.total_tokens, self.context_window, env=self.env)
            bg_handle = await self.backend.send_prompt(bell_text)
            bg_result = await self.backend.collect_response(
                bg_handle, on_meta=on_streaming_meta, cancel_event=cancel_event)
            self.total_tokens = self.backend.total_tokens
            self.turns_since_compaction += 1
            self.last_response_ts = time.time()
            self.last_was_foreground = False
            if bg_result.speech.strip():
                write_to_outbox(agent_name, bg_result.speech.strip(),
                               self.gaze.get("speech"), "speech", env=self.env)
                if (bg_result.thoughts.strip() and self.gaze.get("thoughts")
                        and bg_result.thoughts.strip() != bg_result.speech.strip()):
                    write_to_outbox(agent_name, bg_result.thoughts.strip(),
                                   self.gaze.get("thoughts"), "thoughts", env=self.env)
                write_health(agent_name, "active", "background doorbell response",
                            self.total_tokens, self.context_window, env=self.env)
            delivered += 1

        return delivered

    def read_observer_state(self):
        """Read observer state file. Returns None if observer dead/stale."""
        if not self.observer_state_file:
            return None
        try:
            from binary_state_observer import BinaryStateObserver
            return BinaryStateObserver.read_state_file(self.observer_state_file)
        except Exception:
            return None

    async def handle_idle(self) -> IdleResult:
        """Handle the idle case: no messages, bells, or commands pending.

        Implements straggler command draining, delay loop, collection window,
        observer state checks, and continue doorbell queuing.
        Always results in main() continuing the loop.

        Updates self.delay_until_event, self.next_turn_delay, self.delay_text,
        self.last_was_foreground as side effects.
        """
        import asyncio
        import json
        import os
        import tempfile
        import time
        from asdaaas import (
            read_awareness, poll_commands, ack_doorbells,
            _cleanup_continue_doorbells, run_delay_loop,
            has_pending_adapter_messages, queue_continue_doorbell,
            write_health, IDLE_POLL_INTERVAL,
        )

        agent_name = self.agent_name
        result = IdleResult()

        awareness = read_awareness(agent_name, env=self.env)
        default_doorbell_enabled = awareness.get("default_doorbell", False)

        if default_doorbell_enabled and not self.delay_until_event:
            if self.next_turn_delay > 0:
                # Drain straggler commands before sleeping
                stragglers = poll_commands(agent_name, env=self.env)
                for cmd in stragglers:
                    action = cmd.get("action", "")
                    if action == "delay":
                        dv = cmd.get("seconds", 0)
                        self.delay_text = cmd.get("text") or None
                        if dv == "until_event":
                            self.delay_until_event = True
                            self.next_turn_delay = 0
                            _cleanup_continue_doorbells(agent_name, env=self.env)
                        else:
                            self.next_turn_delay = float(dv)
                            self.delay_until_event = False
                    elif action == "ack":
                        handled = cmd.get("handled", [])
                        if handled:
                            ack_doorbells(agent_name, handled, env=self.env)
                    piggyback_ack = cmd.get("ack", [])
                    if piggyback_ack:
                        ack_doorbells(agent_name, piggyback_ack, env=self.env)
                if stragglers:
                    print(f"[asdaaas] Drained {len(stragglers)} straggler command(s) before delay")
                if self.delay_until_event:
                    return result
                if self.next_turn_delay <= 0:
                    pass  # fall through to continue doorbell
                else:
                    print(f"[asdaaas] Default doorbell: delaying {self.next_turn_delay}s")
                    interrupted, reason = await run_delay_loop(
                        agent_name, self.next_turn_delay, awareness, env=self.env
                    )
                    self.next_turn_delay = 0
                    self.last_was_foreground = True
                    if interrupted:
                        print(f"[asdaaas] Delay interrupted by {reason}")
                        result.delay_interrupted = True
                        return result

            # Collection window: wait briefly for late-arriving messages
            obs_cw = self.read_observer_state()
            cw_polls = 4 if (obs_cw and obs_cw.get("state") == "IDLE") else 12
            collected = False
            for _cw in range(cw_polls):
                await asyncio.sleep(0.25)
                if has_pending_adapter_messages(agent_name, awareness, env=self.env):
                    collected = True
                    break
            if collected:
                print(f"[asdaaas] Message arrived during collection window — skipping continue")
                return result

            # Final command poll before queueing continue (issue_0034)
            from asdaaas import agent_dir as _agent_dir
            final_cmds = poll_commands(agent_name, env=self.env)
            agent_wrote_delay = False
            for fc in final_cmds:
                fa = fc.get("action", "")
                fpiggy = fc.get("ack", [])
                if fpiggy:
                    ack_doorbells(agent_name, fpiggy, env=self.env)
                if fa == "delay":
                    fdv = fc.get("seconds", 0)
                    self.delay_text = fc.get("text") or None
                    if fdv == "until_event":
                        self.delay_until_event = True
                        self.next_turn_delay = 0
                    else:
                        self.next_turn_delay = float(fdv)
                        self.delay_until_event = False
                    agent_wrote_delay = True
                elif fa == "ack":
                    ack_doorbells(agent_name, fc.get("handled", []), env=self.env)
                elif fa in ("compact", "gaze", "awareness"):
                    cmd_dir = _agent_dir(agent_name, env=self.env) / "commands"
                    cmd_dir.mkdir(parents=True, exist_ok=True)
                    fd, tmp = tempfile.mkstemp(dir=str(cmd_dir), suffix=".json", prefix="cmd_requeue_")
                    with os.fdopen(fd, "w") as f:
                        json.dump(fc, f)
            if final_cmds:
                print(f"[asdaaas] Pre-continue poll: {len(final_cmds)} command(s)")
                if self.delay_until_event or self.next_turn_delay > 0:
                    print(f"[asdaaas] Late delay command found — skipping continue")
                    result.action = "continue"
                    return result

            # Observer state checks (observer-only, Phase 5)
            obs = self.read_observer_state()
            if obs:
                obs_st = obs.get("state")
                result.observer_state = obs_st
                if obs_st == "BUSY":
                    print(f"[asdaaas] Observer: binary BUSY — skipping continue")
                    await asyncio.sleep(2.0)
                    return result
                elif obs_st == "GONE":
                    exit_code = obs.get("exit_code")
                    print(f"[asdaaas] Observer: binary GONE (exit_code={exit_code}) — stopping continues")
                    self.delay_until_event = True
                    write_health(agent_name, "stalled", f"binary_gone (exit={exit_code})",
                                self.total_tokens, self.context_window, env=self.env)
                    try:
                        from localmail import send_mail
                        send_mail(
                            from_agent="asdaaas",
                            to_agent="Sr",
                            text=f"BINARY GONE: {agent_name} process died (exit_code={exit_code}). Stopping continues.",
                        )
                    except Exception:
                        pass
                    return result
                elif obs_st == "RETRYING":
                    attempt = obs.get("retry_attempt", 0)
                    reason = obs.get("retry_reason", "unknown")
                    print(f"[asdaaas] Observer: binary RETRYING (attempt={attempt}, reason={reason}) — waiting")
                    await asyncio.sleep(2.0)
                    return result
                elif obs_st == "STUCK":
                    since = obs.get("since", 0)
                    stuck_dur = time.time() - since if since else 0
                    print(f"[asdaaas] Observer: binary STUCK ({stuck_dur:.0f}s) — skipping continue")
                    write_health(agent_name, "active", f"observer_stuck ({stuck_dur:.0f}s)",
                                self.total_tokens, self.context_window, env=self.env)
                    await asyncio.sleep(5.0)
                    return result

            if queue_continue_doorbell(agent_name, text=self.delay_text, env=self.env):
                print(f"[asdaaas] Default doorbell queued for {agent_name}")
                result.continue_queued = True
            self.delay_text = None
            return result

        # Agent idle (until_event or no default_doorbell)
        self.last_was_foreground = True
        result.action = "sleep"
        return result
