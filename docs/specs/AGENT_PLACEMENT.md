# Agent placement — viewport, seat, home

**Status:** concept (Eric 2026-10-08). Not implementing today.  
**Related:** [`aa_stream/SESSION_EPOCHS.md`](./aa_stream/SESSION_EPOCHS.md), gaze, thiasai dual viewports.

Grok bot got this right: **what is on a device is a local representation of the agent**, so the same bot can show up on a phone or a laptop. The cost is that xAI owns the real thing (session, memory, inference).

We want that fluidity **without** handing the agent to a vendor. An agent in thiasai can be *on* several systems at once, and **active inference** can move while the conversation stays put.

---

## Four planes (place independently)

| Plane | Job | Today | Grok bot |
|-------|-----|-------|----------|
| **Surface** | Where Eric talks (phone app, TUI, glass, SA) | Gaze already routes speech | Any logged-in device |
| **Home / SoR** | Who the agent *is*: V1 speech, V2 stream, V3 epochs, notes, METHOD | Path on one disk (`~/agents/Squiggy`) | xAI cloud |
| **Control** | asdaaas: doorbells, delay, commands, occupancy | One process on one host | xAI |
| **Seat** | Active inference + tools/cwd for this turn | Same host as control + grok UUID | xAI GPU |

Gaze already splits **surface** from the other three. Session epochs split **seat UUID** from **home**. Still glued: home, control, and seat live on one machine.

The phone story is a **placement**, not a new identity:

1. Conversation on a native thiasai app (surface = phone).
2. Move **seat** to this machine (inference + this filesystem, this grok/claude/codex).
3. Maybe a third machine (guest VM, another host).
4. Back to the phone — same V1 thread, different seat.

Nothing in that requires the grok session UUID, the asdaaas pid, or the phone to be the same object.

“On multiple systems” is occupancy: the agent may have a **set** of systems (phone, WSL, guest, a lab box) while **one** seat is doing active inference. Tools and files can stay on the interesting machine even when the surface is elsewhere. That is gaze + seating taken off a single host.

---

## What we must not fuse

- Surface ≠ seat. Talking on the phone does not mean the model runs on the phone.
- Seat ≠ home. A new grok/claude/codex epoch on this box does not fork the agent.
- Home ≠ vendor cloud. V1/V3/carry are ours; native journals are disposable caches on whatever seat last ran.

Carry (post-compaction slots 2+3) is what lets a **seat move**: the next machine instantiates a new epoch with the same priors. The phone never needs `updates.jsonl`.

---

## Gentle modularity (no rewrite)

1. Keep gaze as the surface pointer. A thiasai phone app is another gaze target, same as TUI/SA.
2. Keep home as files we already have (V1, notes, continuity). Next cut is **home reachable from more than one host** (thiasai store / sync of the small SoR, not the 3.4G grok dir).
3. Seat placement: spawn/resume backend on a chosen host, seed with compact-prior, record the seat in V3 (`host`, `backend`, `epoch`).
4. Control can stay “one asdaaas” for a long time if doorbells and gaze can **find** the live seat. Migrating asdaaas itself is later.

Vendor lock shows up the moment home or carry lives only inside a binary session. Epochs + V1 are the escape hatch we just named.
