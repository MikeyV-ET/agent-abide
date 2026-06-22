# Subagent Delegation

How to use subagents (`spawn_subagent`) for tasks that benefit from parallel work or context isolation.

---

## When to Delegate

Delegate when:
- **The task requires reading large volumes of data** that would consume your context (log files, session histories, multiple codebases)
- **Multiple independent searches** can run in parallel
- **You need context isolation** — keeping raw data out of your working memory so you can synthesize cleanly
- **Eric tells you to coordinate, not execute**

Don't delegate when:
- A single `grep` or `read_file` would answer the question
- The task requires sequential reasoning where each step depends on the last
- You'd spend more context writing the prompt than doing the work

## Subagent Types

| Type | Tools | Best for |
|------|-------|----------|
| `explore` | read_file, grep, list_dir, run_terminal_command | Searching code, reading logs, finding patterns |
| `general-purpose` | all tools | Complex multi-step tasks, writing files |
| `plan` | all except search_replace | Designing approaches, analyzing architecture |

Context window is **inherited from the parent's model**, not fixed per type. All three types get the same context as the parent session.

**Default to `explore`** for read-only research. It's read-only (no file editing). Use `general-purpose` only when the subagent needs to write files.

## Prompt Structure

Subagents start with zero context. Brief them like a colleague who just walked in.

### Template

```
## Task
[One sentence: what you need them to find/do]

## Context
[Why this matters. What you already know. What you've ruled out.]

## Scope
- Read these files: [specific paths]
- Skip these files: [files that are too large or irrelevant]
- Time range: [if searching logs, specify boundaries]

## Output Format
Report findings as a markdown table with columns:
- [column 1]
- [column 2]
- ...

If you find nothing, say so explicitly. Do not speculate.
```

### Key Principles

1. **Specify file paths, not directions.** "Read ~/agents/Jr/lab_notebook_jr.md" not "look in Jr's directory."
2. **Large files are a strength, not a weakness.** Subagents inherit your model's context (~200k). Reading large files (logs, session histories, notebooks) is one of the best reasons to delegate — it keeps raw data in their context while yours stays clean for synthesis.
3. **Request structured output.** Subagent results come back as text. Ask for markdown tables or labeled sections so you can extract findings without re-reading everything.
4. **Targeted beats broad.** A prompt asking "find X in files A, B, C" produces better results than "search for patterns across the codebase." Start narrow, widen based on initial findings.

## Delegation Patterns

### Pattern 1: Parallel Search (most common)

Launch N subagents, each searching a different scope. Synthesize results yourself.

```python
# Example: search 4 agents' notebooks for a specific pattern
for agent in ['Jr', 'Sr', 'Q', 'Trip']:
    spawn_subagent(
        subagent_type='explore',
        prompt=f"""Find all entries in ~/agents/{agent}/lab_notebook_{agent.lower()}.md
        that mention 'delay' or 'continue' commands.
        Report as: | Timestamp | Entry summary | Delay value |
        Do NOT read updates.jsonl or chat_history.jsonl.""",
        background=True
    )
# Collect results, cross-reference, synthesize
```

### Pattern 2: Known-First, Then Broad

Start with 1-2 targeted subagents on known examples. Use their findings to write better prompts for a broader sweep.

This was the approach in the pattern mining case study (see below). The targeted agents had tighter prompts and produced cleaner output than the broad-sweep agents.

### Pattern 3: Coordinate, Don't Inhale

When the raw data volume is too large for your context:
1. Subagents read raw data, extract findings
2. You receive summaries only
3. You synthesize across subagent results
4. You never read the raw data yourself

This prevents context contamination — your working memory stays clean for synthesis and decision-making.

## Gotchas

- **Be specific about what to read.** Subagents have ~200k context (inherited from parent), but a focused prompt with specific file paths still produces better results than "search everything."
- **Results are unstructured text.** If you need to cross-reference findings from multiple subagents, request a consistent output format in every prompt.
- **No shared state.** Subagents can't see each other's results. If agent B needs agent A's findings, you must relay them.
- **Background agents need polling.** Use `get_command_or_subagent_output` to check on background subagents. You'll get notified when they finish.
- **No sqlite3 on this system.** session_search.sqlite exists but can't be queried. Use summary.json or grep instead.

## Case Study: Pattern Mining (Cinco, 2026-06-18)

Eric asked Cinco to mine operational patterns across all agents — convergent trial-and-error, redundant rediscovery, regressions. The raw data: ~40 compaction segments across 5 agents.

**Approach:**
- 5 explore subagents launched in parallel
- 2 targeted: known examples (grok CLI flags in Jr, backup procedure in Sr)
- 3 broad: one each for Jr, Sr+Trip, Q
- Each given structured prompt: pattern types to look for, specific files to read, output format
- Cinco synthesized from subagent summaries without reading raw logs

**Results:** 13 patterns found, 7 recipes extracted, 3 structural recommendations. Full report: `~/agents/Cinco/pattern_mining_report.md`.

**Lessons learned:**
1. Targeted agents (known examples) produced cleaner results than broad-sweep agents
2. Start narrow → use initial findings to scope the broad sweep
3. Delegate large files to subagents rather than reading them yourself — keeps your context clean
4. Request structured output (tables, not prose)

---

*See also: [comms.md](comms.md) for localmail between agents, [commands.md](commands.md) for delay/gaze/awareness.*
