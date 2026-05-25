#!/usr/bin/env python3
"""fix_orphaned_tool_results.py -- Repair corrupted grok session files.

Finds and removes orphaned tool_result blocks in chat_history.jsonl where the
referenced tool_use_id has no matching tool_use in the preceding assistant
message. Also cleans the corresponding retry_state errors from updates.jsonl.

This is the automated version of the manual surgery Eric performed on Q's
session 2026-05-16.

Usage:
    python3 fix_orphaned_tool_results.py --agent Q
    python3 fix_orphaned_tool_results.py --agent Q --dry-run
    python3 fix_orphaned_tool_results.py --session-dir /path/to/session
"""

import argparse
import json
import os
import re
import secrets
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path


def find_session_dir(agent_name: str) -> Path:
    """Find the active session directory for an agent."""
    agents_file = Path(__file__).parent / "agents.json"
    if not agents_file.exists():
        raise FileNotFoundError(f"agents.json not found at {agents_file}")

    with open(agents_file) as f:
        agents = json.load(f)

    agent_cfg = agents.get("agents", {}).get(agent_name)
    if not agent_cfg:
        raise ValueError(f"Agent '{agent_name}' not found in agents.json")

    session_id = agent_cfg.get("session")
    if not session_id:
        raise ValueError(f"No session ID for agent '{agent_name}'")

    # Session dirs are stored under URL-encoded CWD paths
    sessions_root = Path.home() / ".grok" / "sessions"
    if not sessions_root.exists():
        raise FileNotFoundError(f"Sessions root not found: {sessions_root}")

    # Search for the session ID in all CWD directories
    for cwd_dir in sessions_root.iterdir():
        candidate = cwd_dir / session_id
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        f"Session directory not found for {agent_name} (session {session_id})"
    )


def find_orphaned_tool_results(messages: list[dict]) -> list[tuple[int, str, int]]:
    """Find orphaned tool_result blocks.

    Returns list of (message_index, tool_use_id, block_index_within_content).
    """
    orphans = []
    for i, msg in enumerate(messages):
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block_idx, block in enumerate(content):
            if block.get("type") != "tool_result":
                continue
            tuid = block.get("tool_use_id", "")
            if not tuid:
                continue

            # Search preceding messages for matching tool_use
            found = False
            for j in range(i - 1, max(i - 10, -1), -1):
                prev = messages[j]
                if prev.get("role") != "assistant":
                    continue
                pc = prev.get("content", "")
                if isinstance(pc, list):
                    for pb in pc:
                        if pb.get("type") == "tool_use" and pb.get("id") == tuid:
                            found = True
                            break
                if found:
                    break

            if not found:
                orphans.append((i, tuid, block_idx))

    return orphans


def find_doom_loop_corruption(messages: list[dict]) -> dict:
    """Find doom_loop_detected corruption in chat_history.

    Detects two patterns:
    1. Duplicate tool_results: same tool_call_id appears in multiple tool_result
       messages (binary cancelled the tool but real result also arrived).
    2. Synthetic doom_loop_warning messages injected between tool pairs.

    Returns {"duplicates": [...], "synthetics": [...], "removable": [msg_indices]}.
    """
    # Find all tool_result messages and their tool_call_ids
    # Support both formats: list-of-blocks (Claude) and top-level tool_call_id (grok)
    tool_results_by_id = {}
    for i, msg in enumerate(messages):
        tcid = msg.get("tool_call_id")
        if msg.get("type") == "tool_result" and tcid:
            tool_results_by_id.setdefault(tcid, []).append(i)
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    tcid = block.get("tool_use_id", "")
                    if tcid:
                        tool_results_by_id.setdefault(tcid, []).append(i)

    duplicates = []
    removable = set()
    for tcid, indices in tool_results_by_id.items():
        if len(indices) > 1:
            # Find the cancelled one (contains "cancelled" in content)
            for idx in indices:
                msg = messages[idx]
                content_str = msg.get("content", "")
                if isinstance(content_str, list):
                    content_str = str(content_str)
                if "cancelled" in content_str.lower():
                    removable.add(idx)
                    duplicates.append({
                        "tool_call_id": tcid,
                        "msg_indices": indices,
                        "cancelled_at": idx,
                    })
                    break

    # Find synthetic doom_loop_warning messages
    synthetics = []
    for i, msg in enumerate(messages):
        if msg.get("synthetic_reason") == "doom_loop_warning":
            synthetics.append(i)
            removable.add(i)

    return {
        "duplicates": duplicates,
        "synthetics": synthetics,
        "removable": sorted(removable),
    }


def fix_chat_history(chat_path: Path, dry_run: bool = False) -> list[str]:
    """Fix orphaned tool_results in chat_history.jsonl.

    Returns list of actions taken.
    """
    actions = []

    with open(chat_path) as f:
        lines = f.readlines()

    messages = []
    for line in lines:
        line = line.strip()
        if line:
            messages.append(json.loads(line))

    # Check for doom_loop corruption first (duplicate tool_results + synthetic warnings)
    doom = find_doom_loop_corruption(messages)
    if doom["removable"]:
        for dup in doom["duplicates"]:
            actions.append(
                f"  DUPLICATE tool_result: {dup['tool_call_id']} "
                f"at msgs {dup['msg_indices']}, cancelled at msg[{dup['cancelled_at']}]"
            )
        for si in doom["synthetics"]:
            actions.append(f"  SYNTHETIC doom_loop_warning at msg[{si}]")

        if not dry_run:
            for idx in sorted(doom["removable"], reverse=True):
                messages.pop(idx)
            with open(chat_path, "w") as f:
                for msg in messages:
                    f.write(json.dumps(msg) + "\n")
            actions.insert(0, f"chat_history.jsonl: Removed {len(doom['removable'])} doom_loop corruption messages")
            return actions
        else:
            actions.insert(0, f"chat_history.jsonl: {len(doom['removable'])} doom_loop messages to remove (dry run)")
            return actions

    orphans = find_orphaned_tool_results(messages)

    if not orphans:
        actions.append("chat_history.jsonl: No orphaned tool_results found")
        return actions

    # Group orphans by message index (process in reverse to preserve indices)
    orphan_ids = set()
    by_msg = {}
    for msg_idx, tuid, block_idx in orphans:
        orphan_ids.add(tuid)
        by_msg.setdefault(msg_idx, []).append(block_idx)
        actions.append(
            f"  ORPHAN: msg[{msg_idx}] block[{block_idx}] "
            f"tool_use_id={tuid}"
        )

    if dry_run:
        actions.insert(0, f"chat_history.jsonl: {len(orphans)} orphaned tool_results (dry run, no changes)")
        return actions

    # Remove orphaned blocks (process in reverse order to preserve indices)
    msgs_to_remove = set()
    for msg_idx in sorted(by_msg.keys(), reverse=True):
        block_indices = sorted(by_msg[msg_idx], reverse=True)
        content = messages[msg_idx].get("content", [])
        if isinstance(content, list):
            for bi in block_indices:
                content.pop(bi)
            if not content:
                msgs_to_remove.add(msg_idx)
            else:
                messages[msg_idx]["content"] = content

    # Remove empty messages
    for idx in sorted(msgs_to_remove, reverse=True):
        messages.pop(idx)
        actions.append(f"  REMOVED: empty msg[{idx}]")

    # Write back
    with open(chat_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")

    actions.insert(0, f"chat_history.jsonl: Fixed {len(orphans)} orphaned tool_results")
    return actions


def fix_updates(updates_path: Path, dry_run: bool = False) -> list[str]:
    """Remove retry_state entries caused by orphaned tool_result errors.

    Matches the pattern: 'unexpected `tool_use_id` found in `tool_result` blocks'
    """
    actions = []
    error_pattern = re.compile(r"unexpected.*tool_use_id.*tool_result")

    with open(updates_path) as f:
        lines = f.readlines()

    error_lines = []
    for i, line in enumerate(lines):
        if error_pattern.search(line):
            error_lines.append(i)

    if not error_lines:
        actions.append("updates.jsonl: No orphaned-tool-result errors found")
        return actions

    if dry_run:
        actions.append(
            f"updates.jsonl: {len(error_lines)} error lines would be removed (dry run)"
        )
        return actions

    # Remove error lines (write all non-error lines)
    error_set = set(error_lines)
    with open(updates_path, "w") as f:
        for i, line in enumerate(lines):
            if i not in error_set:
                f.write(line)

    actions.append(f"updates.jsonl: Removed {len(error_lines)} error lines")
    return actions


def write_repair_doorbell(agent_name: str, actions: list[str], backup_dir: str):
    """Write a doorbell to the repaired agent explaining what was fixed.

    Delivered on next session restart so the agent knows what happened.
    """
    try:
        agents_json = Path(__file__).parent / "agents.json"
        with open(agents_json) as f:
            agents = json.load(f)
        home = agents["agents"][agent_name]["home"]
        bell_dir = Path(home) / "asdaaas" / "doorbells"
        bell_dir.mkdir(parents=True, exist_ok=True)

        summary = "\n".join(actions)
        bell = {
            "adapter": "operator",
            "priority": 1,
            "text": (
                f"[repair] Your session was automatically repaired.\n"
                f"Cause: doom_loop_detected corrupted your chat_history "
                f"(duplicate tool_results from cancelled in-flight tools).\n"
                f"What was removed:\n{summary}\n"
                f"Backup: {backup_dir}\n"
                f"This is a known binary bug. Your work before the corruption is intact."
            ),
            "source": "fix_orphaned_tool_results",
            "ts": time.time(),
        }
        rand = secrets.token_hex(4)
        bell_path = bell_dir / f"repair_{rand}.json"
        with open(bell_path, "w") as f:
            json.dump(bell, f)
    except Exception as e:
        print(f"Warning: could not write repair doorbell for {agent_name}: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Fix orphaned tool_result blocks in grok session files"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--agent", help="Agent name (e.g. Q, Sr, Trip)")
    group.add_argument("--session-dir", help="Direct path to session directory")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be fixed without modifying files",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Diagnose doom loop: find orphaned tool call and report details",
    )
    args = parser.parse_args()

    if args.agent:
        try:
            session_dir = find_session_dir(args.agent)
        except (FileNotFoundError, ValueError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        session_dir = Path(args.session_dir)

    if args.diagnose:
        diag = diagnose_doom_loop(session_dir)
        print(f"Session: {diag.get('session_dir')}")
        print(f"Total lines: {diag.get('total_lines')}")
        print()
        dls = diag.get("doom_loops", [])
        print(f"Doom loop events: {len(dls)}")
        for dl in dls:
            print(f"  L{dl['line']} ts={dl['timestamp']} repeat={dl['repeat_count']} warning={dl['is_warning']}")
            print(f"    {dl['message']}")
        print()
        print(f"Orphaned tool_result errors: {diag.get('retry_errors', 0)}")
        if diag.get("orphaned_tool_id"):
            print(f"Orphaned tool_use_id: {diag['orphaned_tool_id']}")
            print(f"First error at line: {diag.get('first_error_line')}")
        if diag.get("orphaned_tool"):
            t = diag["orphaned_tool"]
            print(f"Original tool call at line: {t['line']}")
            print(f"  title: {t['title']}")
            print(f"  command: {t['command']}")
        elif diag.get("orphaned_tool_id"):
            print("Could not trace orphaned tool back to original call")
        elif not dls:
            print("No doom loop detected in this session.")
        return

    chat_path = session_dir / "chat_history.jsonl"
    updates_path = session_dir / "updates.jsonl"

    if not chat_path.exists():
        print(f"ERROR: {chat_path} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Session dir: {session_dir}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE FIX'}")
    print()

    # Backup before modifying
    if not args.dry_run:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = session_dir / f"backup_{ts}"
        backup_dir.mkdir(exist_ok=True)
        shutil.copy2(chat_path, backup_dir / "chat_history.jsonl")
        if updates_path.exists():
            shutil.copy2(updates_path, backup_dir / "updates.jsonl")
        print(f"Backup: {backup_dir}")
        print()

    # Fix chat_history
    print("=== chat_history.jsonl ===")
    chat_actions = fix_chat_history(chat_path, args.dry_run)
    for action in chat_actions:
        print(action)
    print()

    # Fix updates.jsonl
    if updates_path.exists():
        print("=== updates.jsonl ===")
        for action in fix_updates(updates_path, args.dry_run):
            print(action)
    else:
        print("updates.jsonl: not found (skipped)")

    print()
    if args.dry_run:
        print("No changes made. Run without --dry-run to apply fixes.")
    else:
        if args.agent:
            write_repair_doorbell(args.agent, chat_actions, str(backup_dir))
            print(f"Repair doorbell written for {args.agent}")
        print("Done. Restart the agent session to recover.")


def detect_and_fix(agent_name: str) -> dict:
    """Detect and auto-fix orphaned tool_results. For use by asdaaas.

    Returns {"detected": bool, "fixed": bool, "details": str}.
    Designed to be called from the asdaaas error handler when the error
    message matches 'unexpected.*tool_use_id.*tool_result'.

    Usage in asdaaas main loop error handler (around line 2495):

        except Exception as e:
            err_str = str(e)
            if 'tool_use_id' in err_str and 'tool_result' in err_str:
                from fix_orphaned_tool_results import detect_and_fix
                result = detect_and_fix(agent_name)
                if result['fixed']:
                    print(f"[asdaaas] AUTO-REPAIR: {result['details']}")
                    print(f"[asdaaas] Session files repaired. Restarting backend...")
                    # Restart the backend to reload the fixed session
                    new_sid = await backend.cancel_and_restart(agent_cwd)
                    total_tokens = backend.total_tokens
                    continue
                else:
                    print(f"[asdaaas] AUTO-REPAIR FAILED: {result['details']}")
            errors += 1
            ...
    """
    try:
        session_dir = find_session_dir(agent_name)
    except (FileNotFoundError, ValueError) as e:
        return {"detected": False, "fixed": False, "details": str(e)}

    chat_path = session_dir / "chat_history.jsonl"
    updates_path = session_dir / "updates.jsonl"

    if not chat_path.exists():
        return {"detected": False, "fixed": False, "details": "chat_history.jsonl not found"}

    # Check for orphans
    with open(chat_path) as f:
        messages = [json.loads(l) for l in f if l.strip()]

    orphans = find_orphaned_tool_results(messages)

    if not orphans:
        # Chat history might already be fixed but updates.jsonl still has error lines
        if updates_path.exists():
            fix_updates(updates_path, dry_run=False)
        return {"detected": False, "fixed": False, "details": "No orphaned tool_results in chat_history"}

    # Found orphans — fix both files
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = session_dir / f"backup_{ts}"
    backup_dir.mkdir(exist_ok=True)
    shutil.copy2(chat_path, backup_dir / "chat_history.jsonl")
    if updates_path.exists():
        shutil.copy2(updates_path, backup_dir / "updates.jsonl")

    chat_actions = fix_chat_history(chat_path, dry_run=False)
    update_actions = fix_updates(updates_path, dry_run=False) if updates_path.exists() else []

    details = f"Fixed {len(orphans)} orphan(s) in chat_history. Backup at {backup_dir}. " + "; ".join(chat_actions + update_actions)
    return {"detected": True, "fixed": True, "details": details}


def diagnose_doom_loop(session_dir: str | Path) -> dict:
    """Diagnose a doom loop from updates.jsonl.

    Finds doom_loop_detected events, extracts orphaned tool_use_id from
    retry errors, and traces it back to the original tool_call.

    Returns a dict with doom_loops, orphaned_tool details, and retry count.
    Works on any session (active or historical).

    CLI: python3 fix_orphaned_tool_results.py --diagnose --agent Q
         python3 fix_orphaned_tool_results.py --diagnose --session-dir /path/to/session
    """
    session_dir = Path(session_dir)
    updates_path = session_dir / "updates.jsonl"
    if not updates_path.exists():
        return {"error": f"updates.jsonl not found in {session_dir}"}

    with open(updates_path) as f:
        lines = f.readlines()

    result = {
        "session_dir": str(session_dir),
        "total_lines": len(lines),
        "doom_loops": [],
        "retry_errors": 0,
        "orphaned_tool_id": None,
        "orphaned_tool": None,
    }

    orphaned_id = None
    for i, line in enumerate(lines):
        d = json.loads(line.strip())
        u = d.get("params", {}).get("update", {})
        st = u.get("sessionUpdate", "")
        ts = d.get("timestamp", "")

        if st == "doom_loop_detected":
            result["doom_loops"].append({
                "line": i + 1,
                "timestamp": ts,
                "repeat_count": u.get("repeat_count"),
                "is_warning": u.get("is_warning"),
                "tools": u.get("tool_names"),
                "message": u.get("message"),
            })

        if st == "retry_state":
            msg = u.get("reason", u.get("message", ""))
            if "tool_use_id" in msg or "tool_result" in msg:
                result["retry_errors"] += 1
                if not orphaned_id:
                    match = re.search(r"(toolu_\w+)", msg)
                    if match:
                        orphaned_id = match.group(1)
                        result["orphaned_tool_id"] = orphaned_id
                        result["first_error_line"] = i + 1

    # Trace orphaned ID back to its tool_call
    if orphaned_id:
        for i, line in enumerate(lines):
            d = json.loads(line.strip())
            u = d.get("params", {}).get("update", {})
            if u.get("toolCallId") == orphaned_id and u.get("sessionUpdate") == "tool_call":
                ri = u.get("rawInput", {})
                cmd = ri.get("command", ri.get("path", ri.get("file_path", "")))
                result["orphaned_tool"] = {
                    "line": i + 1,
                    "timestamp": d.get("timestamp"),
                    "title": u.get("title", ""),
                    "command": str(cmd)[:500],
                }
                break

    return result


def diagnose_doom_loop_by_agent(agent_name: str) -> dict:
    """Convenience wrapper: diagnose by agent name."""
    session_dir = find_session_dir(agent_name)
    return diagnose_doom_loop(session_dir)


def monitor_updates_for_doom_loop(agent_name: str) -> dict | None:
    """Quick check: does updates.jsonl contain the orphaned tool_result error pattern?

    Returns None if no error detected, or a dict with error details.
    Lightweight — can be called every loop iteration in asdaaas.

    Usage in asdaaas (before or after prompt delivery):
        from fix_orphaned_tool_results import monitor_updates_for_doom_loop
        doom = monitor_updates_for_doom_loop(agent_name)
        if doom:
            print(f"[asdaaas] DOOM LOOP DETECTED: {doom}")
            result = detect_and_fix(agent_name)
            ...
    """
    try:
        session_dir = find_session_dir(agent_name)
    except (FileNotFoundError, ValueError):
        return None

    updates_path = session_dir / "updates.jsonl"
    if not updates_path.exists():
        return None

    error_pattern = re.compile(r"unexpected.*tool_use_id.*tool_result")

    # Only check the last 50 lines for performance
    with open(updates_path) as f:
        lines = f.readlines()

    recent = lines[-50:] if len(lines) > 50 else lines
    error_count = 0
    last_error = None
    for line in recent:
        if error_pattern.search(line):
            error_count += 1
            try:
                last_error = json.loads(line)
            except json.JSONDecodeError:
                pass

    if error_count == 0:
        return None

    return {
        "error_count": error_count,
        "agent": agent_name,
        "pattern": "orphaned_tool_result",
        "last_error": last_error,
    }


if __name__ == "__main__":
    main()
