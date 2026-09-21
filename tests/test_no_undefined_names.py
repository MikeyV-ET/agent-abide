"""No undefined names in the files agents and the TUI actually run.

Every one of these was only found by someone hitting it live:
  - _record_stdin_interjection: called, never defined        (claude_backend)
  - tempfile/os/json in a module-level helper                 (turn_engine)
  - env used 120 lines before main() assigned it              (asdaaas park clear)
  - is_chrome_speech used, never imported                     (TUI scroll-up)

Each passed review and its own tests, because the tests exercised a different
path from the one that failed. A static undefined-name check catches the whole
class without anyone needing to find the right path first.
"""
import os
import subprocess
import sys

import pytest

pytest.importorskip("pyflakes")

ROOT = os.path.join(os.path.dirname(__file__), "..")

FILES = [
    "tui/asdaaas_tui.py",
    "core/tui_history.py",
    "core/claude_backend.py",
    "core/turn_engine.py",
    "core/interjection.py",
    "core/asdaaas.py",
    "core/stream_adapters/claude.py",
    "core/binary_state/claude.py",
    "core/binary_state/service.py",
    "core/binary_state/machine.py",
]


@pytest.mark.parametrize("rel", FILES)
def test_no_undefined_names(rel):
    out = subprocess.run(
        [sys.executable, "-m", "pyflakes", os.path.join(ROOT, rel)],
        capture_output=True, text=True,
    ).stdout
    undefined = [ln for ln in out.splitlines() if "undefined name" in ln]
    assert not undefined, "\n".join(undefined)
