# V1 token-efficient transcript

**Code:** `core/v1_transcript.py`, CLI `scripts/v1_transcript.py`  
**Input:** `{agent}/asdaaas/history/speech.jsonl (fallback: conversation.jsonl)`  
**Output:** plain text for memory agents / humans (JSON remains SoR)

## Profiles

| Profile | Flag | Use |
|---------|------|-----|
| Default | (none) | Light filter: drop ops kinds/continues; keep attribution |
| **Memory pack** | `--memory-pack` | Day headers, strip tui wrappers, chrome cuts, `##` marker strip |

Memory pack implements `memoryagent_1_recommendations.md` deterministic cuts.

## Keep / drop (kinds)

| KEEP | DROP |
|------|------|
| `message`, `speech`, `interjection`, `thinking` (opt-in) | `doorbell`, `prompt`, `speech_repair` |
| pre-schema user/assistant | pre-schema ops user (`[continue`, clocks, …) |

## Memory-pack chrome cuts

| Cut | Behavior |
|-----|----------|
| Context-left | Remove `[Context left …]`; emit `--- Day Mon DD YYYY ---` once per calendar day |
| Attribution | Strip `<eric (via tui)>` / long bell wrappers; keep `U (arena):` if via ≠ tui |
| Ack / interjection envelope | Drop `To ack:…`, `reply_via=`, `[interjection (…)]` prefix |
| Bare `.` | Delete |
| Doing-preamble | Drop first line if Checking/Investigating/… and body remains long |
| Ephact | Keep inner markdown only |
| Loop liturgy | Drop delay-0 / notes flushed / polling lines |
| Exact command dedupe | Keep first occurrence of a command line |
| **Markdown headings** | See below |

## Markdown `##` headers (`--md-headings`)

| Mode | Effect |
|------|--------|
| `keep` | Leave `## Title` as-is (default non-memory) |
| `single_hash` | `##`/`###` Title → `# Title` (memory-pack default) — **one `#` marks header; extra hashes dropped** |
| `strip_markers` | `## Title` → `Title` (no hash) |
| `strip_and_drop_closers` | strip all markers + remove closer heading lines |

`drop_closer_headings` (on in memory-pack) removes only the **closer** heading lines (`Bottom line`, `One line`, …), not all headers. Body under them stays unless it was only the heading.

**Why strip `#`:** tokens for AT/ATX markers add up across long technical speech; the words still skim. Tables/code fences are **not** stripped.

## Usage

```bash
# Memory ingest (recommended)
python3 scripts/v1_transcript.py --agent Trip-G --memory-pack --stats -o /tmp/tripg_mem.txt

# Default lean view
python3 scripts/v1_transcript.py --agent Trip-G --stats

# Keep ## markers but drop Bottom line headings only
python3 scripts/v1_transcript.py --agent Trip-G --memory-pack --md-headings keep
```

Approx tokens ≈ `chars/4` in `--stats`.


## Two-stage memory read (users → expand)

```bash
# 1) Eric-only index (handles u0, u1, … + ts)
python3 scripts/v1_transcript.py users --agent Trip-G --stats
python3 scripts/v1_transcript.py users --agent Trip-G --json -o /tmp/users.json
python3 scripts/v1_transcript.py users --agent Trip-G --index-out /tmp/users_index.json

# 2) Search user turns
python3 scripts/v1_transcript.py search --agent Trip-G "thiasai"

# 3) Expand full interaction around a hit
python3 scripts/v1_transcript.py expand --agent Trip-G --handle u12 --before 1 --after 2
python3 scripts/v1_transcript.py expand --agent Trip-G --at-ts 1789123456.0 --before 0 --after 1
```

Handles: `u{N}` (user index), `e{N}` (kept-entry index), `ts={unix}`, or bare `N` as `uN`.

Agent protocol sketch:
1. `users` / `search` on Eric turns only  
2. `expand` for windows that need A: context  
3. V2 by `ts` only if implementation detail is required  
