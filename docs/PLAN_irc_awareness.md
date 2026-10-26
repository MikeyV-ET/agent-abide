# Plan: IRC as net-response + awareness (no gaze hose)

Eric + Trip-G, 2026-10-26. TUI/SA/thiasai stay **show-all**. IRC/mail/Slack are **net response** (last speech only), localmail-shaped. Multi-way is an exception on that letter, not a second architecture.

## Model

**Awareness** (keep the name). Channels are **foreground** or **background**.

| Mode | Inbound | Outbound |
|------|---------|----------|
| **foreground** | Ordinary user turn + telemetry | Last `agent_message_chunk` written as a letter to that address |
| **background** `doorbell` | Notify (today’s heavier form OK) | No auto letter |
| **background** `pending` | Queue until fg | — |
| **background** `drop` | Discard | — |

No **gaze hose**. Attention is the inbound label (`from=#gen4`). Reply is **write a file to that address** (same pattern as localmail). asdaaas does not PRIVMSG.

**Show-all** (TUI, SA, thiasai session): always the journal. Not a send target. Display may paint a `#gen4` letter as a chat line; that is chrome, not routing.

**Net response:** last `agent_message_chunk` before `turn_completed`. Same event type as openers; distinguished by **position**, not a schema field. Tools and thinking never leave the session.

**Inbound letter** = the doorbell **is** the message, plus telemetry: channel, who, flags, seat, speak vs `noted`. Foreground → cheap user turn (like Eric via TUI). Background → localmail-ish notify.

**IRC adapter** = routing protocol, not a `hot.jsonl` parser.

- Letter tagged `#gen4` → **PRIVMSG** immediately (you see it).
- RR + flags decide **who gets a foreground turn** (unmask). Do not hold PRIVMSG.
- Collect letters back until flags empty / noted pass.

**Flags:** a channel post (speak) sets speaker=0 and OR 1 on everyone else. `noted` lowers you. Next doorbell = next flagged name in the shuffled circle. Research/tools do not post, so they do not raise flags.

## Where it lives

| Piece | Home |
|--------|------|
| Last chunk = speech | asdaaas `collect_response` / `turn_engine` (today **joins all chunks** into outbox) |
| Letter write (`to=#gen4`) | asdaaas outbox, like localmail |
| Fg vs bg | `awareness.json` — add **foreground**; stop using `gaze.json` as hose |
| PRIVMSG, nicks, socket | `irc_adapter` |
| RR flags, unmask, telemetry on doorbell | `irc_adapter` (already has the algebra) |
| Show-all + Room pane | TUI |
| `#gen4.log` | miniircd (SoR for “what was said”) |

Do **not** build a hot.jsonl miner.

## Cuts (order)

1. **Always PRIVMSG speech; RR only doorbells.** Remove outbound hold. Stale-seat timeout already exists (3 min).
2. **Last chunk only** to the speech outbox (`segments[-1]` / last chunk after last tool). Opener stays in the journal.
3. **Awareness foreground.** `#gen4` can be fg without `gaze.speech=irc`. Inbound match is casefolded (`#Gen4`==`#gen4`). Gaze remains for TUI/SA until those are “show-all only.”
4. **Telemetry on the inbox JSON** (channel, from, flags, seat, noted). Foreground path = `kind=message` user turn, not doorbell-only.
5. **TUI:** Room pane already paints PRIVMSG. Paint fg telemetry; optional paint of outbox writes. Awareness UI: fg vs bg. Unique nicks already coded (restart TUIs).
6. **Docs:** `howto/gaze.md` + ARCHITECTURE gaze-wins paragraph → awareness fg/bg. Thought channels stay dropped.

## Test

- Bounce **irc_adapter** (process is 2026-10-23 15:21).
- Set Holt + Squiggy awareness **foreground `#gen4`** (gazes are TUI today).
- Eric line in Room pane → only first seat wakes; PRIVMSG of last chunk visible to Eric **as it is sent**; second seat gets unmask with new speeches only (n−1), not `noted` lines.
- Tools on Squiggy’s tab, not on `#gen4`.
- Two TUI Room panes both see operator + agent PRIVMSGs.

Holt asdaaas is still an old binary (`f012423`); bounce if gaze/awareness code must match.

## Non-goals (this pass)

- MCP `irc_say` / `irc_read` (doorbell-with-body is enough).
- Per-utterance target picker (extra DOF = gaze again).
- Nested anchors / thiasai editor (separate).
- Hand-raise `flag floor` (RR replaced it; can delete later).
- Silent turn with zero tokens (binary `reasoning_only` retry — Holt 2026-10-26). Keep `noted` / `.`.

## Friday residue (done in code, not all live)

Thought-channel drop, unique TUI nicks, operator-echo skip fix, gaze casefold, RR flag machine, delta n−1 receipts, `irc_round.json` header, 3 min stale seat. Live processes/gazes do not all match HEAD.

## Status

Plan only, 2026-10-26. Eric asked for the writeup at ~63% context. Implement when he says go, starting at cut 1.
