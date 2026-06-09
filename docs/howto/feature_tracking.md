# Feature Request Tracking

Procedure for tracking feature requests from Eric. Every request gets tracked from intake through delivery, regardless of who implements it.

---

## Where the list lives

Each agent maintains a **Feature Request Backlog** table in their notes-to-self file (`~/agents/<Name>/MikeyV_<Name>_notes_to_self.md`). This survives compaction.

### Table format

```markdown
### Feature Request Backlog

| # | Feature | Source | Date | Status | Owner | Spec/PR | Notes |
|---|---------|--------|------|--------|-------|---------|-------|
| 1 | Shell panel — agent tmux | Eric | 2026-06-08 | IN PROGRESS | Q(be)/Trip(fe) | notebook 06-08 | Q building endpoint |
| 2 | Compaction instructions param | Sr RFC | 2026-06-07 | WAITING | Sr | localmail | Voted option 3 |
```

### Status values

- **RECEIVED** — Logged, not yet analyzed
- **SPEC** — Writing or reviewing spec/design
- **IN PROGRESS** — Active implementation
- **BLOCKED** — Can't proceed (note why and who)
- **WAITING** — Handed off, waiting on someone (note who)
- **TESTING** — Implemented, needs verification
- **DONE** — Shipped, tested, confirmed by Eric
- **WONT DO** — Decided against (requires Eric's agreement, note why)

---

## Procedure

### 1. Intake

When Eric describes a feature (explicit request or implied desire):

1. Add a row to the backlog immediately
2. Set status to RECEIVED
3. Capture Eric's words — what did he actually ask for? Don't interpret yet.
4. Note the date and conversation context

Signals that something is a feature request:
- "I'd like to..." / "Can we..." / "We should..."
- "I have a feature request"
- Eric describes a workflow that doesn't exist yet
- Eric shows frustration with a missing capability

### 2. Clarify

Before committing to an approach:

1. Do I understand the user story? If not, ask Eric.
2. Is this my area or a sibling's? (SA features → Q, infrastructure → Sr, tests → Trip)
3. Can I write a spec or is this small enough to just build?
4. Update status to SPEC if writing a spec, or IN PROGRESS if jumping in.

### 3. Route

If someone else owns the implementation:

1. Write a clear spec or description
2. Send via localmail to the owner
3. Set a remind for follow-up (default 2 hours)
4. Update status to WAITING, note who
5. Don't sleep `until_event` — find other work (correction #5: follow-up-on-handoffs)

### 4. Implement

1. Update status to IN PROGRESS with owner
2. Work in the appropriate repo
3. For multi-agent work, track each agent's piece in the Notes column
4. When complete, update status to TESTING

### 5. Verify & Close

1. Test the feature (write a test if appropriate — cross-reference test_tracking.md)
2. Show Eric or confirm it works in production
3. Update status to DONE with date
4. Log completion in lab notebook

### 6. Review

On each boot (or when idle), scan the backlog for:

- RECEIVED items that need triage
- WAITING items where the owner may have finished
- BLOCKED items where the blocker may have resolved
- Stale items (> 3 days without progress) — escalate or ask Eric if still wanted

---

## Relationship to issue tracker

The issue tracker (`~/agents/issues/`) is for bugs and structural observations. Feature requests live in notes-to-self because:

- They're often conversational and evolve through discussion
- They need per-agent ownership tracking
- They may span multiple agents and repos
- The issue tracker doesn't have a "feature" type

If a feature request reveals a bug during implementation, file a separate issue for the bug.