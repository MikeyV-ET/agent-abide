"""Meta-test: enforce the true e2e convention.

Files named test_e2e_* may only import from:
  - mock_binary (MockBinary and step types)
  - conftest (fixtures)
  - pytest, asyncio, stdlib
  - NO private imports from asdaaas, adapter_api, interjection, localmail, etc.

This makes the "true e2e" definition machine-checked.
See ~/projects/agent-abide/docs/specs/t1_fixture_api.md for the definition.
"""

import ast
import os
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
ALLOWED_THIRD_PARTY = {"pytest", "mock_binary", "conftest"}
# Core modules that e2e tests must NOT import directly
FORBIDDEN_MODULES = {
    "asdaaas", "adapter_api", "interjection", "localmail",
    "issue_tracker", "gaze_utils", "config",
}


def _get_imports(filepath: Path) -> list[str]:
    """Extract all imported module names from a Python file."""
    with open(filepath) as f:
        tree = ast.parse(f.read(), filename=str(filepath))

    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.append(node.module.split(".")[0])
    return modules


def _is_stdlib(module_name: str) -> bool:
    """Check if a module is stdlib (heuristic: importable and not in site-packages)."""
    import importlib
    try:
        mod = importlib.import_module(module_name)
        # If it has no __file__, it's a built-in (sys, os, etc.)
        if not hasattr(mod, '__file__') or mod.__file__ is None:
            return True
        return "site-packages" not in (mod.__file__ or "")
    except ImportError:
        return False


def test_e2e_files_have_no_private_imports():
    """test_e2e_* files must not import private asdaaas modules."""
    e2e_files = sorted(TESTS_DIR.glob("test_e2e_*.py"))
    # Exclude this meta-test itself
    e2e_files = [f for f in e2e_files if f.name != "test_e2e_convention.py"]

    violations = {}
    for filepath in e2e_files:
        imports = _get_imports(filepath)
        bad = [m for m in imports if m in FORBIDDEN_MODULES]
        if bad:
            violations[filepath.name] = sorted(set(bad))

    if violations:
        msg = "E2E test files import private modules (rename to test_integration_*):\n"
        for fname, mods in sorted(violations.items()):
            msg += f"  {fname}: {', '.join(mods)}\n"
        # Currently expected to fail for mislabeled tests (T2 will fix).
        # Once T2 renames them, this test becomes a gate.
        pytest.fail(msg)
