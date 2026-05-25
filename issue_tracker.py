"""MikeyV Issue Tracker -- bugs, design gaps, and structural observations.

Any agent can file an issue. Sr triages.
Tags group related issues for structural pattern recognition.

Issue types:
  bug          -- something worked and stopped working
  gap          -- something was never built but should exist
  observation  -- structural pattern or systemic concern

Usage:
    from issue_tracker import file_issue, list_issues, update_issue, patterns

    file_issue(
        filed_by="Q",
        title="session_registry.json never updated",
        description="asdaaas gets new session ID but never writes registry",
        type="gap",
        severity="P2",
        tags=["stale-data", "restart-safety"],
    )

    # Backward compat: file_bug still works
    from issue_tracker import file_bug
"""

import json
import os
import time
import secrets
from pathlib import Path

try:
    from asdaaas_config import config
except ModuleNotFoundError:
    import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'core'))
    from asdaaas_config import config

ISSUES_DIR = config.issues_dir
VALID_TYPES = {"bug", "gap", "observation"}


def _next_id():
    """Generate next issue ID from existing files."""
    ISSUES_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(ISSUES_DIR.glob("issue_*.json"))
    if not existing:
        return "issue_0001"
    last = existing[-1].stem
    num = int(last.split("_")[1]) + 1
    return f"issue_{num:04d}"


def file_issue(filed_by, title, description="", type="bug", severity="P2",
               tags=None, steps_to_reproduce=None, context=None):
    """File a new issue. Returns the issue ID.

    type: "bug" (regression), "gap" (missing feature/design), "observation" (structural pattern)
    Tags are short labels for grouping. When 2+ issues share a tag, that's a pattern.
    """
    if type not in VALID_TYPES:
        type = "bug"
    issue_id = _next_id()
    issue = {
        "id": issue_id,
        "type": type,
        "filed_by": filed_by,
        "filed_at": time.time(),
        "severity": severity,
        "title": title,
        "description": description,
        "steps_to_reproduce": steps_to_reproduce or [],
        "context": context or "",
        "tags": tags or [],
        "status": "open",
        "assigned_to": None,
        "diagnosis": None,
        "fix_commit": None,
        "verified_by": None,
    }
    path = ISSUES_DIR / f"{issue_id}.json"
    with open(path, "w") as f:
        json.dump(issue, f, indent=2)

    # Check if any tags just formed a structural pattern (2+ issues on same tag)
    new_patterns = []
    if tags:
        from collections import defaultdict
        tag_counts = defaultdict(int)
        for existing in list_issues():
            if existing["id"] == issue_id:
                continue
            for t in existing.get("tags", []):
                tag_counts[t] += 1
        for t in tags:
            if tag_counts.get(t, 0) >= 1:
                new_patterns.append(t)

    # Notify Sr via localmail (only if filed by someone else)
    if filed_by != "Sr":
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).parent))
            from localmail import send_mail
            pattern_note = ""
            if new_patterns:
                pattern_note = f"\nPATTERN DETECTED: tags {new_patterns} now have 2+ issues"
            send_mail(
                from_agent=filed_by,
                to_agent="Sr",
                text=f"[ISSUE FILED] {issue_id} ({type}): {title}\nSeverity: {severity}\nFiled by: {filed_by}{pattern_note}",
            )
        except Exception:
            pass

    if new_patterns:
        print(f"[issue_tracker] PATTERN: tags {new_patterns} now have 2+ issues -- structural condition?")

    return issue_id


def list_issues(status=None, severity=None, assigned_to=None, type=None):
    """List issues, optionally filtered. Returns list of issue dicts."""
    ISSUES_DIR.mkdir(parents=True, exist_ok=True)
    issues = []
    for f in sorted(ISSUES_DIR.glob("issue_*.json")):
        try:
            with open(f) as fh:
                issue = json.load(fh)
            if status and issue.get("status") != status:
                continue
            if severity and issue.get("severity") != severity:
                continue
            if assigned_to and issue.get("assigned_to") != assigned_to:
                continue
            if type and issue.get("type") != type:
                continue
            issues.append(issue)
        except (json.JSONDecodeError, OSError):
            pass
    return issues


def update_issue(issue_id, **fields):
    """Update fields on an existing issue. Returns updated issue or None."""
    path = ISSUES_DIR / f"{issue_id}.json"
    if not path.exists():
        return None
    with open(path) as f:
        issue = json.load(f)

    allowed = {"status", "assigned_to", "diagnosis", "fix_commit",
               "verified_by", "severity", "title", "description",
               "steps_to_reproduce", "context", "tags", "type"}
    for k, v in fields.items():
        if k in allowed:
            issue[k] = v

    issue["updated_at"] = time.time()

    with open(path, "w") as f:
        json.dump(issue, f, indent=2)
    return issue


def get_issue(issue_id):
    """Read a single issue by ID. Returns dict or None."""
    path = ISSUES_DIR / f"{issue_id}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def summary():
    """One-line-per-issue summary."""
    issues = list_issues()
    if not issues:
        return "No issues filed."
    lines = []
    for i in issues:
        status = i.get("status", "?")
        sev = i.get("severity", "?")
        itype = i.get("type", "bug")
        assigned = i.get("assigned_to") or "unassigned"
        lines.append(f"  {i['id']} [{sev}] {itype:12s} {status:10s} {assigned:10s} {i['title']}")
    return "\n".join(lines)


def patterns(status=None):
    """Group issues by tag. Returns {tag: [issue, ...]} for tags with 2+ issues."""
    from collections import defaultdict
    tag_map = defaultdict(list)
    for issue in list_issues(status=status):
        for tag in issue.get("tags", []):
            tag_map[tag].append(issue)
    return {tag: issues for tag, issues in sorted(tag_map.items()) if len(issues) >= 2}


def pattern_report(status=None):
    """Human-readable pattern report."""
    p = patterns(status=status)
    if not p:
        return "No structural patterns detected (need 2+ issues sharing a tag)."
    lines = ["Structural patterns:"]
    for tag, issues in p.items():
        lines.append(f"\n  [{tag}] ({len(issues)} issues)")
        for i in issues:
            lines.append(f"    {i['id']} [{i.get('severity','?')}] {i.get('status','?'):10s} {i['title']}")
    return "\n".join(lines)


# ---- Backward compatibility aliases ----
file_bug = file_issue
list_bugs = list_issues
update_bug = update_issue
get_bug = get_issue


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "list":
        print(summary())
    elif cmd == "patterns":
        print(pattern_report())
    elif cmd == "open":
        for i in list_issues(status="open"):
            sev = i.get("severity", "?")
            itype = i.get("type", "bug")
            print(f"  {i['id']} [{sev}] {itype:12s} {i['title']}")
    else:
        print("Usage: python3 issue_tracker.py list       # all issues")
        print("       python3 issue_tracker.py patterns   # structural pattern review")
        print("       python3 issue_tracker.py open       # open issues only")
