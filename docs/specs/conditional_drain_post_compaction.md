# Spec: Conditional Drain for Post-Compaction Orientation

**Author:** Trip  
**Date:** 2026-07-01  
**Implementer:** Sr  
**Tester:** Trip  
**Related:** issue_0033 (302d988), 393e9b5 (orientation watcher)

## Problem

When an agent compacts, messages that arrive during compaction are drained from adapter inboxes into `pending_queue` before the orientation turn starts (L2243-2248, fix for issue_0033). These messages are delivered as regular turns AFTER orientation completes.

Edge case: Eric sends a message during compaction to abort a destructive action the agent was planning. The agent runs its full boot protocol during orientation, then starts executing the pre-compaction plan. Eric's abort message arrives as a separate turn — too late.

## Desired Behavior

For agents with `interjection_enabled=True`: messages that arrived during compaction should be delivered as interjections during the orientation turn, not as post-orientation turns. This way the agent sees "STOP" during its first tool call (reading principles, notebook, etc.) and can adjust course.

For agents with `interjection_enabled=False`: keep current behavior (drain to pending_queue). No interjection pipeline available, so issue_0033 fix applies unchanged.

## Why This Doesn't Break Issue 0033

Issue 0033's bug: user message delivered as a PROMPT before the compaction-complete system message. Agent processed the user request without boot protocol.

Interjections are different — they appear INSIDE tool output, not as a competing prompt. The sequence becomes:

1. Compaction completes
2. Orientation prompt sent: "Compaction complete. Follow boot protocol."
3. Agent starts boot (reads principles — tool call)
4. Interjection appears in tool output: "STOP — don't do the destructive thing"
5. Agent sees interjection, adjusts course

The compaction-complete message is still prompt #1. The user's message is delivered inside tool output, not as a separate prompt. Boot protocol still runs. The agent just also gets warned.

## Implementation

**File:** `core/asdaaas.py`, post-compaction orientation block (~L2239-2290)

**Current code (L2243-2248):**
```python
held_msgs = poll_adapter_inboxes(agent_name, awareness)
held_msgs.extend(poll_inbox(agent_name))
if held_msgs:
    print(f"[asdaaas] Holding {len(held_msgs)} message(s) until after orientation")
    for hm in held_msgs:
        pending_queue.enqueue(hm)
```

**Proposed change:**
```python
if interjection_enabled:
    # Let interjection watcher deliver these during orientation boot.
    # Messages stay in adapter inboxes; watcher polls and queues them
    # as interjections. Agent sees them in tool call output during boot.
    # Localmail/internal messages still drain (they're not interruptible).
    held_internal = poll_inbox(agent_name)
    if held_internal:
        print(f"[asdaaas] Holding {len(held_internal)} internal message(s) until after orientation")
        for hm in held_internal:
            pending_queue.enqueue(hm)
    adapter_count = count_adapter_inbox_messages(agent_name, awareness)
    if adapter_count:
        print(f"[asdaaas] Leaving {adapter_count} adapter message(s) for interjection during orientation")
else:
    # No interjection pipeline — drain everything to pending_queue (issue_0033)
    held_msgs = poll_adapter_inboxes(agent_name, awareness)
    held_msgs.extend(poll_inbox(agent_name))
    if held_msgs:
        print(f"[asdaaas] Holding {len(held_msgs)} message(s) until after orientation")
        for hm in held_msgs:
            pending_queue.enqueue(hm)
```

**Note:** `count_adapter_inbox_messages` may need to be added — a non-destructive count of messages in adapter inboxes. Alternatively, just skip the `poll_adapter_inboxes` call entirely (messages stay in inboxes, watcher picks them up). The log line is optional but useful for debugging.

**Simpler version (no new function needed):**
```python
# Always drain internal inbox (localmail, etc.)
held_internal = poll_inbox(agent_name)
if held_internal:
    print(f"[asdaaas] Holding {len(held_internal)} internal message(s) until after orientation")
    for hm in held_internal:
        pending_queue.enqueue(hm)

if not interjection_enabled:
    # No interjection pipeline — also drain adapter inboxes (issue_0033)
    held_adapter = poll_adapter_inboxes(agent_name, awareness)
    if held_adapter:
        print(f"[asdaaas] Holding {len(held_adapter)} adapter message(s) until after orientation")
        for hm in held_adapter:
            pending_queue.enqueue(hm)
# else: adapter messages stay in inboxes for interjection watcher
```

## Test Plan (Trip)

### Path 1: interjection_enabled=True — watcher delivers during orientation

**test_compaction_messages_interjected_during_orientation:**
- Scenario: NormalResponse → Compaction → ShellToolCall (orientation)
- Before orientation: inject message into TUI adapter inbox
- Spawn interjection watcher (same as main())
- Verify: ShellToolCall output contains `<interjection>` with the message
- Verify: message is NOT in pending_queue (it was consumed by watcher+hook)

### Path 2: interjection_enabled=False — drain to pending_queue

**test_compaction_messages_drained_without_interjection:**
- Scenario: NormalResponse → Compaction → ShellToolCall (orientation)
- Before orientation: inject message into TUI adapter inbox
- Do NOT spawn interjection watcher
- Call poll_adapter_inboxes (simulating the drain)
- Verify: messages end up in pending_queue
- Verify: ShellToolCall output does NOT contain `<interjection>`

### Path 3: internal messages always drain

**test_internal_messages_always_drain:**
- Even with interjection_enabled=True, poll_inbox (localmail) messages drain to pending_queue
- They should NOT be interjected (localmail isn't an interrupt channel)

### Regression: issue_0033

**test_no_message_before_orientation_prompt:**
- With interjection_enabled=True, verify the compaction-complete message is still the first prompt
- No user message prompt appears before it
