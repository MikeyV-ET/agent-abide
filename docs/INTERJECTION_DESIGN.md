# Mid-Turn Message Interjection via BASH_ENV

## Problem

Agents receive messages only between turns. During long turns with many tool calls, incoming messages (Eric typing, localmail, IRC) queue up and aren't delivered until the turn completes. This creates a poor conversational experience — Eric sends a message, the agent doesn't see it for minutes.

The grok binary's `x.ai/interject` method exists but is pager-internal only. Both `agent stdio` and `agent serve` modes return -32601 for all x.ai/* extensions (34 tested in stdio, 28 in WebSocket — zero accessible). We cannot inject messages through the binary's API.

## Mechanism

The binary spawns `/bin/bash -c "..."` for every `run_terminal_command` tool call. These shells are non-interactive (`$- = hBc`). Non-interactive bash sources `$BASH_ENV` before executing any command.

If asdaaas sets `BASH_ENV` in the binary's environment, every shell tool call will source our hook script. The hook checks a queue directory for pending messages and prepends them to stdout. The model sees the interjection as part of the tool call result.

## Proof of Concept (verified 2026-06-29)

```bash
# Hook script
cat > /path/to/interjection_hook.sh << 'HOOK'
if ls ~/agents/$AGENT_NAME/asdaaas/interjections/*.txt 2>/dev/null | head -1 | grep -q .; then
    echo ""
    echo "=== [asdaaas interjection] ==="
    cat ~/agents/$AGENT_NAME/asdaaas/interjections/*.txt 2>/dev/null
    rm -f ~/agents/$AGENT_NAME/asdaaas/interjections/*.txt
    echo "=== [end interjection] ==="
fi
HOOK

# Test: message appears in tool result
mkdir -p ~/agents/Trip/asdaaas/interjections
echo "Eric says: check IRC" > ~/agents/Trip/asdaaas/interjections/msg1.txt
BASH_ENV=/path/to/interjection_hook.sh /bin/bash -c 'echo "normal output"'

# Output:
# === [asdaaas interjection] ===
# Eric says: check IRC
# === [end interjection] ===
# normal output

# Second call with empty queue: no injection, clean output
```

## Coverage

From Trip's session data (~12,900 tool calls):

| Category | Count | % | Interceptable? |
|----------|------:|--:|:---:|
| `run_terminal_command` (shell) | 7,460 | 58% | Yes |
| `read_file` | 1,995 | 15% | No |
| `search_replace` | 1,492 | 12% | No |
| `grep` | 1,013 | 8% | No |
| `todo_write` | 544 | 4% | No |
| Other binary-internal | 394 | 3% | No |

58% of tool calls pass through a shell we control. The 42% binary-internal tools are unreachable, but the next shell tool call catches any queued messages. Worst case latency: the gap between consecutive shell tool calls.

## Architecture

```
asdaaas.py
  │
  ├── sets BASH_ENV + AGENT_NAME in env
  ├── spawns: grok agent stdio
  │     │
  │     ├── tool_call: run_terminal_command
  │     │     └── /bin/bash -c "..." ← sources BASH_ENV
  │     │           └── interjection_hook.sh checks queue
  │     │                 └── prepends messages to stdout
  │     │
  │     ├── tool_call: read_file ← no shell, no hook
  │     ├── tool_call: grep ← no shell, no hook
  │     └── ...
  │
  └── queue_interjection(agent, text)
        └── writes to ~/agents/<Name>/asdaaas/interjections/
```

## Implementation Plan

### 1. Interjection hook script
**File:** `core/interjection_hook.sh`

- Sources on every `run_terminal_command`
- Reads `$AGENT_NAME` from env (set by asdaaas)
- Checks `~/agents/$AGENT_NAME/asdaaas/interjections/` for `.txt` files
- If found: prints interjection block to stdout, deletes consumed files
- If empty: no output, zero overhead (one `ls` call)
- Must be fast — runs on every single shell tool call

### 2. Queue function
**In:** `core/asdaaas.py` or `core/comms.py`

```python
def queue_interjection(agent_name: str, text: str):
    """Queue a message for mid-turn delivery via BASH_ENV hook."""
    interject_dir = agent_dir(agent_name) / "asdaaas" / "interjections"
    interject_dir.mkdir(parents=True, exist_ok=True)
    msg_file = interject_dir / f"interject_{int(time.time()*1000)}.txt"
    msg_file.write_text(text)
```

### 3. Environment setup in asdaaas.py
When spawning the binary, add to the environment:
```python
env = {**os.environ, 
       "BASH_ENV": str(HOOK_SCRIPT_PATH),
       "AGENT_NAME": agent_name}
```

### 4. Integration with adapters
When a message arrives mid-turn (observer says BUSY), instead of queuing for next turn, call `queue_interjection()`. The message appears in the next shell tool call's output.

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Hook adds latency to every shell call | Keep hook minimal (one `ls` check). Benchmark. |
| Message appears mid-output, confusing model | XML delimiters + system framing + AGENTS.md docs |
| Race: two tool calls consume same message | Use atomic rename: write `.tmp`, rename to `.txt` |
| Binary doesn't pass BASH_ENV to children | Verified: binary inherits env from parent, shells inherit from binary |
| Message too large for tool result | Cap interjection size, queue remainder for next call |
| 42% of tool calls are unreachable | Acceptable — next shell call catches overflow. Median gap is short. |

## Prerequisite: Delivery Receipt for All Messages

Interjection requires delivery receipt — confirming the binary actually processed a message. But this shouldn't be interjection-specific. Asdaaas should verify delivery of ALL messages (doorbells, adapter messages, continues) by checking updates.jsonl.

**Current state:** Asdaaas fires a prompt and assumes the binary processed it. No verification. Can't distinguish "agent saw it, didn't ack" from "binary never processed it."

**Proposed:** Asdaaas watches updates.jsonl for message content appearing in `user_message_chunk` events (normal delivery) or `tool_call_update.rawOutput.output_for_prompt` (interjection delivery). This gives three states:

| State | Meaning | Action |
|-------|---------|--------|
| Delivered + acked | Agent saw it and confirmed | Clear doorbell |
| Delivered + not acked | Binary processed it, agent didn't ack | Don't redeliver, flag |
| Not delivered | Binary dropped it | Redeliver |

The third case is invisible today. Delivery receipt must be implemented before interjection — it's the foundation for knowing whether fire-into-stdout actually worked.

**Implementation:** Asdaaas already reads updates.jsonl for token counts and health. Extend this to track message content hashes against pending doorbells. When content appears in updates.jsonl, mark as delivered.

## Interjection Ack Path

Interjected messages carry doorbell IDs, same as normal delivery. The agent acks them via the standard command queue. Format:

```
<interjection>
[system: messages arrived during your tool call]
[localmail (id=bell_abc123, ts=Mon Jun 29 18:55 PDT) from Jr] hey, check this fix
</interjection>
```

Same ID, same ack mechanism, different delivery channel. If the agent doesn't ack after delivery receipt confirms processing, the doorbell persists but is not redelivered (agent saw it but didn't ack yet — may ack later).

## Design Decisions (resolved with Sr, 2026-06-29)

1. **stdout, not stderr.** Stdout is the guaranteed path into the model's context. Stderr goes to the binary's own logs — may or may not reach the model.

2. **Batch delivery.** Drain the entire queue atomically each time the hook fires. Parallel tool calls could fragment a message across results otherwise. Read all, delete all, prepend all as one block.

3. **Delimiter format.** Use XML-style delimiters with system framing:
```
<interjection>
[system: messages arrived during your tool call]
[localmail from Jr] ...
[localmail from Q] ...
</interjection>
```

4. **Fast empty path.** The hook runs on EVERY shell tool call. Empty-queue path must be ~1ms: `test -d` + `ls` check, no python, no JSON. Only do work when messages are present.

5. **Observer as delivery mode switch.** Observer BUSY = queue for interjection (mid-turn delivery via BASH_ENV). Observer IDLE = deliver normally via doorbell (between-turn delivery). Clean separation: the observer is the single decision point for delivery mode.

6. **AGENTS.md documentation.** Add a section to agent AGENTS.md explaining that `<interjection>` blocks may appear in tool call results and should be acted on. Models need to know this happens so they're not confused by unexpected content in tool output.

## History

- 2026-05-05: PROMPT_COMMAND approach committed (d341306 in agents repo) but never verified. Found shell is non-interactive, PROMPT_COMMAND doesn't fire.
- 2026-06-29: Full x.ai/* extension surface probed — zero accessible in stdio or WebSocket. Interjection via binary API confirmed impossible.
- 2026-06-29: BASH_ENV mechanism discovered, proof of concept verified. Tool call distribution analyzed (58% shell, 42% binary-internal).
- 2026-06-29: This design doc written.
- 2026-06-29: Sr reviewed. Resolved all open questions: stdout, batch, fast empty path, observer as delivery switch, XML delimiters, AGENTS.md docs.
- 2026-06-29: Eric review. Added delivery receipt as prerequisite (all messages, not just interjections). Added interjection ack path (carry doorbell IDs).