#!/usr/bin/env python3
"""Run a command in a PTY with forced terminal geometry.

Usage:
  python3 scripts/run_with_geom.py 120 40 -- echo hello
  python3 scripts/run_with_geom.py 80 24 -- python3 scripts/verify_tui_tip.py

This is the agent-side stand-in for "Eric's glass": arbitrary cols×rows without
depending on tmux client size or a human session.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    packed = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cols", type=int, help="terminal columns")
    ap.add_argument("rows", type=int, help="terminal rows")
    ap.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="command after -- ",
    )
    args = ap.parse_args()
    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        ap.error("need a command after --")

    cols = max(20, args.cols)
    rows = max(5, args.rows)
    master, slave = pty.openpty()
    _set_winsize(slave, rows, cols)
    try:
        _set_winsize(master, rows, cols)
    except OSError:
        pass

    env = os.environ.copy()
    env["TERM"] = env.get("TERM") or "xterm-256color"
    env["COLUMNS"] = str(cols)
    env["LINES"] = str(rows)
    # strip host geometry lies
    env.pop("GPG_TTY", None)

    proc = subprocess.Popen(
        cmd,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        close_fds=True,
        preexec_fn=os.setsid,
    )
    os.close(slave)

    out = b""
    try:
        while True:
            if proc.poll() is not None:
                # drain
                while True:
                    r, _, _ = select.select([master], [], [], 0.05)
                    if master not in r:
                        break
                    try:
                        chunk = os.read(master, 8192)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        break
                    out += chunk
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                break
            r, _, _ = select.select([master], [], [], 0.2)
            if master in r:
                try:
                    chunk = os.read(master, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        os.close(master)

    return int(proc.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
