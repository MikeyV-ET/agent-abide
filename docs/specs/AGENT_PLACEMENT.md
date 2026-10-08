# Agent placement — viewport, seat, home

**Status:** concept (Eric 2026-10-08). Not implementing today.  
**Related:** [`aa_stream/SESSION_EPOCHS.md`](./aa_stream/SESSION_EPOCHS.md), gaze, thiasai dual viewports.

Grok bot got this right: **what is on a device is a local representation of the agent**, so the same bot can show up on a phone or a laptop. Their inference still runs on **one** virtual machine in their cloud; every device is a client of that VM. The cost is that xAI owns the VM.

Ours is a different topology. There is no one remote box. **The virtual machine is the set of places es has instantiated.** Phone, this WSL, guest, a lab host — together they *are* the computer. Sync is how that overlay stays one machine.

We want that fluidity **without** handing the agent to a vendor. Eric’s version (2026-10-08): the agent is **on all of the machines**. The problem is keeping those replicas in sync — not packing a suitcase and moving one seat.

---

## Four planes (place independently)

| Plane | Job | Today | Grok bot |
|-------|-----|-------|----------|
| **Surface** | Where Eric talks (phone app, TUI, glass, SA) | Gaze already routes speech | Any logged-in device |
| **Home / SoR** | Who the agent *is*: V1 speech, V2 stream, V3 epochs, notes, METHOD | Path on one disk (`~/agents/Squiggy`) | xAI cloud |
| **Control** | asdaaas: doorbells, delay, commands, occupancy | One process on one host | xAI |
| **Seat** | Where this turn’s inference runs (an organ of the overlay VM) | Same host as control + grok UUID | Always one xAI VM |

Gaze already splits **surface** from the other three. Session epochs split **seat UUID** from **home**. Still glued: home, control, and seat live on one machine.

## On all machines (the intended picture)

Es is not visiting. Es **lives** on the phone, this WSL, the guest, a third box — each has a local representation that is allowed to be live (talk, see files, take doorbells). Grok bot does that by making every device a client of one cloud agent. We do it by **replicating home + control** and defining a sync plane we own.

The hard problem is **sync**, not travel:

| What | Sync shape |
|------|------------|
| V1 speech / conversation | Append-only log or CRDT; every surface shows the same thread |
| Notes, METHOD, gaze, delay | Small files; same as thiasai collab (patch/rev, not whole-doc LWW) |
| V3 continuity | Who has which epoch; converge |
| Native grok/claude/codex journals | **Do not sync.** Disposable per replica. Carry/compact-prior is how a replica that was behind infers. |

Active inference can run on any instantiation; **which one is ihm’s choice** (same class as gaze and delay). That is not a copy of grok-bot’s “one VM, many clients.” The instantiations are organs of one body. A turn here can use this filesystem; a turn on the phone can stay on the phone; a tool on the guest is the guest — all the same agent, one synced home.

Two uncoordinated grok binaries dumping into one V1 will fork. Coordination is the overlay: one agent, many organs, explicit seat for a given turn. Cheap to move because the body is already there.

Thiasai already has the collab pattern for documents (Yjs / change-log ladder). Agent-as-peer on that kind of store is the same idea with V1 as the shared tape.

Phone + this machine + guest: one conversation object, three live representations, doorbells land wherever, inference runs where the interesting files are *this turn*.

---

## What we must not fuse

- Surface ≠ seat. Talking on the phone does not mean the model runs on the phone.
- Seat ≠ home. A new grok/claude/codex epoch on this box does not fork the agent.
- Home ≠ vendor cloud. V1/V3/carry are ours; native journals are disposable caches on whatever seat last ran.

Carry (post-compaction slots 2+3) is how a replica that was offline, or a new backend epoch, **joins the sync** without swallowing another machine’s native journal. The phone never needs `updates.jsonl`.

---

## Gentle modularity (no rewrite)

1. Keep gaze as the surface pointer. A thiasai phone app is another gaze target, same as TUI/SA.
2. Home is a **replicated object** (V1, notes, continuity) — thiasai store / CRDT / change-log, not rsync of `~/.grok/sessions`.
3. Each machine may run control. Doorbells and speech append to the shared tape; one elected writer for inference.
4. A new epoch on any replica seeds from compact-prior + the synced home, then publishes back.

Vendor lock shows up the moment home or carry lives only inside a binary session. Epochs + V1 are the escape hatch we just named.
