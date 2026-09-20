#!/usr/bin/env python3
"""Probe: does writing to claude's stdin DURING a turn surface inside that turn?

Everything in the "native mid-turn injection" design rests on this question and
nobody has tested it: asdaaas only ever writes at turn boundaries.

Method: spawn claude exactly as ClaudeBackend does, plus --replay-user-messages.
Send prompt 1, which parks the model in a ~60s CPU-bound foreground Bash call.
Once the model is observed inside that tool call, write prompt 2 to stdin. The
turn boundary is the stream-json `result` frame (stop_reason is not carried on
assistant frames in this stream).

Result, 2026-09-20 16:20 PDT (Astro):
    2.9s   model enters the Bash call
    6.9s   prompt 2 written to stdin, mid-turn and mid-tool
   62.4s   tool returns; replay shows the tool_result, THEN the injected message
   65.7s   model: "BANANA — the command finished..."; then result = end of turn 1
So an injected message is held until the running tool call returns and is then
delivered inside the same turn. Mid-turn delivery, not mid-tool preemption —
the same granularity as the BASH_ENV hook, without the shell.

Two earlier runs were inconclusive and are worth knowing about: a `sleep` was
backgrounded by the harness (foreground sleeps are blocked) so the turn ended
before injection, and a short loop finished before the timer fired. Hence the
state-triggered injection below rather than a wall-clock one.

Costs one short claude session on the account that runs it.
"""
import asyncio
import json
import os
import time

CLAUDE = os.path.expanduser("~/.local/bin/claude")
CWD = "/tmp/astro_probe_cwd"
STREAM_LIMIT = 64 * 1024 * 1024

PROMPT_1 = (
    "Run exactly this bash command in the FOREGROUND and nothing else: "
    "python3 -c \"t=0\nfor i in range(700000000): t+=i\nprint('PARKED',t)\" . "
    "Do not background it, do not run any other tool. Wait for it. "
    "Then reply with one short sentence."
)
PROMPT_2 = "INTERJECTION PROBE: if you can see this message, say the word BANANA immediately."

T0 = time.monotonic()


def log(tag, detail=""):
    print(f"[{time.monotonic() - T0:6.1f}s] {tag} {detail}"[:300], flush=True)


def user_msg(text):
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"


async def main():
    os.makedirs(CWD, exist_ok=True)
    cmd = [
        CLAUDE,
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--replay-user-messages",
        "--dangerously-skip-permissions",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=CWD,
        limit=STREAM_LIMIT,
    )
    log("SPAWNED", f"pid={proc.pid}")

    proc.stdin.write(user_msg(PROMPT_1).encode())
    await proc.stdin.drain()
    log("SENT", "prompt 1 (parks in a long foreground Bash call)")

    injected = False
    saw_banana_before_end_turn = None
    end_turns = 0

    in_tool = asyncio.Event()

    async def inject_later():
        nonlocal injected
        # Inject on OBSERVED state: once the model is inside its tool call.
        await in_tool.wait()
        await asyncio.sleep(4)
        proc.stdin.write(user_msg(PROMPT_2).encode())
        await proc.stdin.drain()
        injected = True
        log("INJECTED", "prompt 2 written to stdin mid-turn")

    asyncio.create_task(inject_later())

    deadline = time.monotonic() + 110
    while time.monotonic() < deadline:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
        except asyncio.TimeoutError:
            log("...", "no frame for 10s")
            continue
        if not line:
            break
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue

        ftype = frame.get("type")
        if ftype == "user":
            # This is what --replay-user-messages gives us.
            content = (frame.get("message") or {}).get("content")
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", b.get("type", "")) for b in content if isinstance(b, dict)
                )
            log("REPLAY(user)", repr(str(content)[:90]))
        elif ftype == "assistant":
            msg = frame.get("message") or {}
            for b in msg.get("content") or []:
                bt = b.get("type")
                if bt == "text":
                    text = b.get("text", "")
                    log("ASSISTANT text", repr(text[:90]))
                    if "BANANA" in text.upper() and end_turns == 0:
                        saw_banana_before_end_turn = True
                elif bt == "tool_use":
                    log("ASSISTANT tool", f"{b.get('name')} {str(b.get('input'))[:60]}")
                    in_tool.set()
        elif ftype == "result":
            end_turns += 1
            log("RESULT = END OF TURN", f"#{end_turns} injected_before_this={injected}")
            if end_turns >= 2 or (end_turns >= 1 and injected):
                break

    log("VERDICT", f"injected={injected} end_turns={end_turns} "
                   f"reacted_mid_turn={bool(saw_banana_before_end_turn)}")
    try:
        proc.terminate()
    except ProcessLookupError:
        pass


asyncio.run(main())
