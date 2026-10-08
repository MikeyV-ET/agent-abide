"""delay_control_from_tool_blob: grok cat vs Claude python json.dump."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tui"))

from delay_control import delay_control_from_tool_blob  # noqa: E402


def test_grok_cat_heredoc():
    blob = """cat > ~/agents/Trip-G/asdaaas/commands/cmd_$(date +%s%3N)_01.json << 'EOF'
{"action": "delay", "seconds": 600}
EOF"""
    assert delay_control_from_tool_blob(blob) == (
        "[aa.control] delay: 600s before next continue"
    )


def test_astro_python_json_dump_split_path():
    blob = """python3 - <<'PYEOF'
import json, os, time, secrets
d = os.path.expanduser('~/agents/Sixel/Astro/asdaaas/commands')
ts = int(time.time()*1000); r = secrets.token_hex(4)
json.dump({'action':'delay','seconds':600,'ack':['bell_518aa292']}, open(f'{d}/cmd_{ts}_{r}.json','w'))
PYEOF
echo ok"""
    assert delay_control_from_tool_blob(blob) == (
        "[aa.control] delay: 600s before next continue"
    )


def test_grep_of_delay_is_not_control():
    blob = "rg -n 'action.: .delay' asdaaas/commands/cmd_foo.json"
    assert delay_control_from_tool_blob(blob) is None
