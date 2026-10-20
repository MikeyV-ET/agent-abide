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


def test_until_event_not_stolen_by_lab_seconds_zero():
    """Lab notes saying seconds:0 must not paint until_event as 0s."""
    blob = r"""
python3 - <<'PY'
open('/home/eric/agents/Trip-G/lab_notebook_trip-g.md','a').write(
    "I did not write seconds:0 last night.\n")
print('lab')
PY
ts=$(date +%s%3N)
cat > ~/agents/Trip-G/asdaaas/commands/cmd_${ts}_01.json << 'EOF'
{"action": "delay", "seconds": "until_event", "text": "Parked after explaining overnight until_event (not delay 0). Wait for Eric. Color hold."}
EOF
"""
    out = delay_control_from_tool_blob(blob)
    assert out is not None
    assert "until_event" in out
    assert "0s before next continue" not in out
    assert "Parked after explaining" in out


def test_real_delay_zero_still_shows_zero():
    blob = """cat > ~/agents/Trip-G/asdaaas/commands/cmd_$(date +%s%3N)_01.json << 'EOF'
{"action": "delay", "seconds": 0, "text": "keep working"}
EOF"""
    out = delay_control_from_tool_blob(blob)
    assert out == "[aa.control] delay: 0s before next continue — keep working"


def test_until_event_plain():
    blob = """cat > ~/agents/Trip-G/asdaaas/commands/cmd_$(date +%s%3N)_01.json << 'EOF'
{"action": "delay", "seconds": "until_event", "text": "Parked for Eric."}
EOF"""
    out = delay_control_from_tool_blob(blob)
    assert out == "[aa.control] delay: until_event (standing by) — Parked for Eric."
