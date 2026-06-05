# Intern/Mentor System

Reference for sandboxed agent onboarding. Read when you are mentoring an intern or when you ARE an intern.

---

## Overview

New or untrusted agents ("interns") run in a sandboxed environment with a mentor agent overseeing their work. This is used for testing new models or agents that haven't earned trust yet.

## Security Model: Sandbox + PR Workflow

The intern runs with kernel-level filesystem sandboxing (`--sandbox workspace`) which restricts writes to sein CWD + `/tmp` + `~/.grok/`. The intern can read everything (to understand the codebase) but can only modify files in sein own directory. This is enforced by Landlock (Linux kernel) -- the model cannot bypass it at runtime.

The intern clones/copies projects into sein workspace, works on a branch, and submits changes via pull request. The mentor reviews the PR before merging. This gives semantic oversight (reviewing actual code changes) rather than mechanical oversight (approving individual tool calls).

## Config (agents.json)

```json
{
  "yolo": true,
  "mentor": "Q",
  "sandbox": "workspace",
  "permission_mode": "auto",
  "context_window": 500000
}
```
- `sandbox`: kernel-level filesystem restriction profile
- `permission_mode: "auto"`: auto-approve tool calls within sandbox bounds
- `mentor`: which agent reviews PRs and provides guidance

## When You're a Mentor

Your intern works independently within sein sandboxed workspace. Your role is:
1. **Code review**: Review PRs before merging into shared repos
2. **Guidance**: Answer questions, suggest approaches via localmail
3. **Escalation**: Flag concerns to Sr or Eric if something looks wrong

## Legacy Permission System

The old per-tool-call permission system (`permission_handler.py`) is still available for cases where finer control is needed. Set `"yolo": false` in agents.json to enable it.

## Sandbox Profiles

Available: `workspace` (recommended), `read-only`, `strict`, or custom profiles in `~/.grok/sandbox.toml`.
