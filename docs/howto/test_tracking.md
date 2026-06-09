# Test Request Tracking

Procedure for tracking test requests from Eric or from your own observations. Every request gets tracked from intake through completion.

---

## Where the list lives

Each agent maintains a **Test Request Backlog** table in their notes-to-self file (`~/agents/<Name>/MikeyV_<Name>_notes_to_self.md`). This survives compaction.

### Table format

```markdown
### Test Request Backlog

| # | Request | Source | Date | Status | Test File | Notes |
|---|---------|--------|------|--------|-----------|-------|
| 1 | E2E: post-compaction turn fires | Eric | 2026-06-09 | DONE | test_e2e_compaction.py:test4 | Committed 0931540 |
| 2 | Unit: validate config schema | Self | 2026-06-10 | IN PROGRESS | | |
| 3 | SA: lazy load on scroll-up | Eric | 2026-06-10 | PENDING | | Waiting on Q's endpoint |
```

### Status values

- **PENDING** — Received, not started
- **IN PROGRESS** — Actively writing
- **BLOCKED** — Can't proceed (note why)
- **DONE** — Written, passing, committed (include commit hash or file)
- **WONT TEST** — Decided not to test (note why, requires Eric's agreement)

---

## Procedure

### 1. Intake

When a test request arrives (from Eric, a sibling, or your own observation):

1. Add a row to the backlog table immediately, before doing anything else
2. Set status to PENDING
3. Source: who asked (Eric, self, Q, Sr, etc.)
4. If the request is vague, note what you need to clarify before writing

### 2. Triage

Before starting a test, check:

- Do I understand what to assert? If not, ask.
- Which test suite does this belong in? (Playwright for SA, pytest for agent-abide, E2E harness for lifecycle)
- Is there a dependency? (e.g., waiting on a fix to land, endpoint to exist)
- Mark BLOCKED with a note if you can't proceed yet.

### 3. Write

1. Set status to IN PROGRESS
2. Study the relevant code — understand what to assert against
3. Write the test
4. Run it (`|| true` for test runners to avoid doom loop)
5. If it fails as expected (testing a known bug), note that. If it should pass, investigate.

### 4. Complete

1. Commit the test with a descriptive message
2. Update the backlog row: status = DONE, add test file path and commit hash
3. Log to lab notebook (what you tested, result, commit)
4. If the test is for someone else's code (Q's SA, Sr's agent-abide), send them a localmail with the test location

### 5. Review

On each boot (or when idle), scan the backlog for:

- PENDING items that can be started
- BLOCKED items where the blocker may have resolved
- Patterns: are there areas with many requests but few tests?

---

## What counts as a test request

- Eric says "write a test for X" — explicit request
- Eric describes a bug — implicit request for a regression test
- Eric says "does this work?" about a feature — implicit request for a verification test
- You find a bug — file issue AND add a test request
- A sibling reports a bug to you — add a test request

The bar is: if it could break again, it should have a test.