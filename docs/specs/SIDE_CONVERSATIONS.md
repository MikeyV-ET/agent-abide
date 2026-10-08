# Side conversations — naive analysis, then review

**Status:** concept (Eric 2026-10-08). Not in TUI/asdaaas as a product yet.  
**Related:** [`aa_stream/HISTORY_VERSIONS.md`](./aa_stream/HISTORY_VERSIONS.md) V3 `sides[]`, [`aa_stream/SESSION_EPOCHS.md`](./aa_stream/SESSION_EPOCHS.md), TUI agent tabs.

Eric’s pattern: talk to a **naive** agent for a particular analysis, then have Squiggy **review that conversation**, then a higher-level talk with Squiggy **about** it. Same TUI, same agent-abide (doorbell, gaze, V1). Not a grok subagent buried inside one binary session.

## Three beats

1. **Naive seat** — fresh epoch, little or no mill/prior lock. Eric talks in a TUI tab like any agent. The point of naive is the analysis you only get *without* Squiggy’s weight.
2. **Tape** — that talk is a V1 (and a short live tip). It is an **object**, not a person. V3 already named these `sides[]`.
3. **Review** — Squiggy (or Trip-G) is handed the tape as a document. The next conversation is Squiggy’s spine, about the side, not as the side.

## What we already have

| Piece | Today |
|-------|--------|
| TUI multi-agent tabs / `[+]` | Attach a **running** roster agent. No “mint a scratch analysis.” |
| asdaaas + V1 per home | Real conversations. Roster citizens are heavy (Squiggy 3.4G). |
| V3 `sides[]` | Named in HISTORY_VERSIONS. Not a runtime spawn. |
| `memory_query` | Packs **the caller’s** record. Cross-agent is “message them,” the opposite of review. |
| Grok `spawn_subagent` | Child of one binary session. Eric does not sit with it in a TUI tab the same way. |

## Scratch does not need memory_service

No `memory_query`, no packs, no workers. The naive seat is a short V1. Squiggy reviews **the transcript** (speech.jsonl / a `v1_transcript` dump). That is the handoff.

## Why do it in this TUI

Once the scratch agent is a real asdaaas seat:

- **Review** — simple: Squiggy is given the tape (path, or a doorbell with the transcript). Not embodiment memory.
- **Clarify** — scratch and Squiggy already have **localmail**. A point of confusion is an email, not a new product.
- **Three-way** — Eric, scratch, and Squiggy join a **channel** (TUI room / IRC). Rooms already exist (`irc_rooms.json`, `[#]` tabs). The missing piece is treating a scratch agent as a first-class nick in that room.

Doing the naive analysis in a throwaway grok.com chat loses those two for free.

## What is not baked in

- Spawn scratch from TUI: name, model, empty epoch, tab. No memory MCP.
- **Transcript review**: one action that puts that V1 in front of Squiggy (doorbell, open-as-doc, or “review this tab”).
- Invite scratch + Squiggy + Eric into one room when the review wants a three-way.

## Why not “just another Squiggy tab”

If the analysis agent *is* Squiggy, you get Squiggy’s priors. The whole move is: **uninformed analysis, then informed review.** Two seats, one overlay, tape moves.
