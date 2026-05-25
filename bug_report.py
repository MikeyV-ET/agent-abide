"""Backward compatibility shim. Use issue_tracker instead."""
from issue_tracker import (
    file_issue as file_bug,
    list_issues as list_bugs,
    update_issue as update_bug,
    get_issue as get_bug,
    summary,
    patterns,
    pattern_report,
    file_issue,
    list_issues,
    update_issue,
    get_issue,
)
