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

The hard problem is **sync**, not travel. **Agent-abide history moves with es** — that is the apparatus that makes local caching easy:

| What | Moves? | Weight |
|------|--------|--------|
| V1 speech | yes | small (Trip-G ~2M, Squiggy conv ~8M) |
| aa.stream **sealed chunks** | yes | **10–30MB holds ~300MB** of prior running history (zst) |
| aa.stream **hot.jsonl** | yes | policy 300→100 MiB; live tip |
| Live `updates.jsonl` | yes | keep **much lighter** now that aa history exists; prune without losing es |
| Notes, METHOD, gaze, continuity | yes | small |
| Native grok extras (`stdout_log`, `terminal/`, fat catalog) | no | Squiggy ~2G+ of Y-channel; not the agent |

The 3.4G grok dir was the old “you are your UUID’s files.” L1 chunks + a short live tip are “you are the tape,” cacheable on every instantiation. Live `updates.jsonl` still syncs so a seat can infer; it no longer has to *be* the lifetime store.

Active inference can run on any instantiation. **Which part of es is carrying the agent forward in this moment is under ihm’s control, and in service of whatever es is doing** — same class as gaze and delay. That is not a copy of grok-bot’s “one VM, many clients.” The instantiations are organs of one body. A turn here can use this filesystem; a turn on the phone can stay on the phone; a tool on the guest is the guest — all the same agent, one synced home.

Two uncoordinated grok binaries dumping into one V1 will fork. Coordination is the overlay: one agent, many organs, explicit seat for a given turn. Cheap to move because the body is already there.

Thiasai already has the collab pattern for documents (Yjs / change-log ladder). Agent-as-peer on that kind of store is the same idea with V1 as the shared tape.

Phone + this machine + guest: one conversation object, three live representations, doorbells land wherever, inference runs where the interesting files are *this turn*.

---

## What we must not fuse

- Surface ≠ seat. Talking on the phone does not mean the model runs on the phone.
- Seat ≠ home. A new grok/claude/codex epoch on this box does not fork the agent.
- Home ≠ vendor cloud. V1 + aa.stream (hot + L1 chunks) + a short live `updates.jsonl` are ours. Grok Y-channel extras are local caches.

Carry (post-compaction slots 2+3) is how a replica that was offline, or a new backend epoch, **joins** without swallowing another machine’s fat native dir. The phone needs the aa history tape and a light live tip — not 1.9G of `stdout_log`.

---

## Gentle modularity (no rewrite)

1. Keep gaze as the surface pointer. A thiasai phone app is another gaze target, same as TUI/SA.
2. Home is a **replicated object**: V1, aa.stream hot+chunks, notes, continuity, light live `updates.jsonl` — not rsync of `~/.grok/sessions`.
3. Each machine may run control. Doorbells and speech append to the shared tape; one elected writer for inference.
4. A new epoch on any replica seeds from compact-prior + the synced home, then publishes back.

Vendor lock shows up the moment home or carry lives only inside a binary session. Epochs + V1 are the escape hatch we just named.

---

## Why the grok journal hunt (2026-10-08)

Managing native file size is **in service of multi-endpoint sync**. The overlay VM only works if what we replicate is light.

Squiggy’s live grok dir is ~3.4G (`stdout_log` 1.95G, `updates.jsonl` 711M, …). That cannot be the backplane. The syncable home is the small SoR:

| Sync (the agent) | Keep local / prune hard |
|------|-------------|
| V1 + aa.stream hot + **L1 zst chunks** + notes + continuity | grok `stdout_log` / `terminal/` / catalog |
| Live `updates.jsonl` (short tip) | Lifetime native journal (aa history already retained it) |
| Compact-prior slots 2+3 | Checkpoints as caches |

Lighter is better because every endpoint instantiates from this tape. 10–30MB chunks are why 300MB of running history can live on a phone. Epochs + prune of live `updates.jsonl` are safe **because** aa history already has es.
