"""Binary State Observer — facade (back-compat).

Implementation lives in ``core/binary_state/``:

  types.py / machine.py     common ActivityEvent + state machine
  grok.py                   GrokBinaryStateObserver (native updates.jsonl)
  claude.py                 ClaudeBinaryStateObserver (session jsonl)
  service.py                InProcessObserver / ObserverService

Historical name ``BinaryStateObserver`` == ``GrokBinaryStateObserver``.
"""
from __future__ import annotations

import argparse
import os
import sys

from binary_state import (  # noqa: F401
    BinaryStateObserver,
    ClaudeBinaryStateObserver,
    ClaudeInProcessObserver,
    GrokBinaryStateObserver,
    InProcessObserver,
    ObserverService,
    ObserverState,
    UpdatesJSONLTailer,
    load_known_types,
    load_silence_windows,
)
from binary_state.types import (  # noqa: F401
    DEFAULT_SILENCE_WINDOW,
    GATE_TOOL_KINDS,
    STATE_TTL,
    TIMEOUT_BUFFER,
)


def main():
    parser = argparse.ArgumentParser(
        description="Binary State Observer — monitors grok binary state (legacy CLI)"
    )
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()

    if not os.path.isdir(args.session_dir):
        print(f"Error: session directory does not exist: {args.session_dir}", file=sys.stderr)
        sys.exit(1)
    state_dir = os.path.dirname(args.state_file)
    if state_dir:
        os.makedirs(state_dir, exist_ok=True)

    service = ObserverService(
        pid=args.pid,
        session_dir=args.session_dir,
        state_file=args.state_file,
        data_dir=args.data_dir,
    )
    service.orient()
    service.run()


if __name__ == "__main__":
    main()
