#!/usr/bin/env python3
"""
tmux_control.py — Tmux session management for agent interaction with terminal apps.
==================================================================================

Gives asdaaas agents the ability to launch, interact with, and observe
terminal applications (like the grok TUI) running in tmux sessions.

Supports both local and remote (SSH) execution for isolated experiment
environments.

Usage (local):
    from tmux_control import TmuxSession

    s = TmuxSession("experiment-1")
    s.launch("grok")
    s.wait_for("❯", timeout=10)       # wait for prompt
    s.send("hello world")
    output = s.capture()               # get screen contents
    s.send_keys("C-c")                 # send control keys
    s.kill()

Usage (remote via SSH):
    s = TmuxSession("experiment-1", ssh_host="experiment-vm")
    s.launch("grok --yolo")
    s.wait_for("❯", timeout=15)
    s.send("help me build a TUI")
    output = s.capture()
    s.kill()

Author: MikeyV-Sr
Date: 2026-03-31
"""

import subprocess
import time
import re
import shlex


class TmuxError(Exception):
    """Raised when a tmux operation fails."""
    pass


class TmuxSession:
    """Manage a tmux session for agent interaction with terminal apps.
    
    Provides a clean interface for:
    - Launching a command in a new tmux session
    - Sending text input (as if typing)
    - Sending control keys (C-c, Enter, etc.)
    - Capturing the current screen contents
    - Waiting for specific output to appear
    - Killing the session
    
    All operations work both locally and over SSH.
    """

    def __init__(self, name, ssh_host=None, ssh_user=None, ssh_opts=None):
        """
        Args:
            name: tmux session name (must be unique)
            ssh_host: remote host for SSH execution (None = local)
            ssh_user: SSH username (None = current user)
            ssh_opts: additional SSH options as list (e.g., ["-i", "/path/to/key"])
        """
        self.name = name
        self.ssh_host = ssh_host
        self.ssh_user = ssh_user
        self.ssh_opts = ssh_opts or []
        self._alive = False

    def _run(self, cmd, check=True, capture=True, timeout=30):
        """Run a command, optionally over SSH.
        
        Args:
            cmd: command as list of strings
            check: raise on non-zero exit
            capture: capture stdout/stderr
            timeout: seconds before timeout
            
        Returns:
            subprocess.CompletedProcess
        """
        if self.ssh_host:
            # Wrap command for SSH execution
            remote_cmd = " ".join(shlex.quote(c) for c in cmd)
            ssh_cmd = ["ssh"]
            ssh_cmd.extend(self.ssh_opts)
            if self.ssh_user:
                ssh_cmd.append(f"{self.ssh_user}@{self.ssh_host}")
            else:
                ssh_cmd.append(self.ssh_host)
            ssh_cmd.append(remote_cmd)
            cmd = ssh_cmd

        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=timeout,
            check=check,
        )

    def exists(self):
        """Check if this tmux session already exists."""
        try:
            result = self._run(
                ["tmux", "has-session", "-t", self.name],
                check=False,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def launch(self, command, width=200, height=50):
        """Launch a command in a new tmux session.
        
        Args:
            command: shell command to run (string)
            width: terminal width in columns
            height: terminal height in rows
            
        Raises:
            TmuxError: if session already exists or launch fails
        """
        if self.exists():
            raise TmuxError(f"Session '{self.name}' already exists. Kill it first or use a different name.")

        try:
            self._run([
                "tmux", "new-session",
                "-d",                    # detached
                "-s", self.name,         # session name
                "-x", str(width),        # width
                "-y", str(height),       # height
                command,
            ])
            self._alive = True
        except subprocess.CalledProcessError as e:
            raise TmuxError(f"Failed to launch session '{self.name}': {e.stderr}") from e

    def send(self, text, enter=True):
        """Send text input to the session (as if typing).
        
        Args:
            text: text to type
            enter: press Enter after text (default True)
        """
        self._check_alive()
        try:
            # Use send-keys with literal flag to avoid key interpretation
            self._run(["tmux", "send-keys", "-t", self.name, "-l", text])
            if enter:
                self._run(["tmux", "send-keys", "-t", self.name, "Enter"])
        except subprocess.CalledProcessError as e:
            raise TmuxError(f"Failed to send text to '{self.name}': {e.stderr}") from e

    def send_keys(self, *keys):
        """Send special keys to the session.
        
        Args:
            keys: tmux key names (e.g., "C-c", "Enter", "Escape", "Up", "C-l")
        """
        self._check_alive()
        try:
            for key in keys:
                self._run(["tmux", "send-keys", "-t", self.name, key])
        except subprocess.CalledProcessError as e:
            raise TmuxError(f"Failed to send keys to '{self.name}': {e.stderr}") from e

    def capture(self, start_line=None, end_line=None, strip_trailing=True):
        """Capture the current screen contents.
        
        Args:
            start_line: first line to capture (negative = from bottom, e.g., -50)
            end_line: last line to capture
            strip_trailing: remove trailing blank lines
            
        Returns:
            str: screen contents
        """
        self._check_alive()
        cmd = ["tmux", "capture-pane", "-t", self.name, "-p"]
        if start_line is not None:
            cmd.extend(["-S", str(start_line)])
        if end_line is not None:
            cmd.extend(["-E", str(end_line)])

        try:
            result = self._run(cmd)
            output = result.stdout
            if strip_trailing:
                # Remove trailing blank lines but preserve internal structure
                lines = output.rstrip("\n").split("\n")
                while lines and lines[-1].strip() == "":
                    lines.pop()
                output = "\n".join(lines) + "\n" if lines else ""
            return output
        except subprocess.CalledProcessError as e:
            raise TmuxError(f"Failed to capture pane from '{self.name}': {e.stderr}") from e

    def capture_scrollback(self, lines=500):
        """Capture scrollback buffer (history beyond current screen).
        
        Args:
            lines: number of scrollback lines to capture
            
        Returns:
            str: scrollback contents
        """
        return self.capture(start_line=-lines)

    def wait_for(self, pattern, timeout=30, poll_interval=0.5):
        """Wait for a pattern to appear in the screen output.
        
        Args:
            pattern: string or regex pattern to wait for
            timeout: seconds before giving up
            poll_interval: seconds between screen captures
            
        Returns:
            str: the captured screen contents when pattern was found
            
        Raises:
            TmuxError: if timeout expires without finding pattern
        """
        self._check_alive()
        deadline = time.time() + timeout
        compiled = re.compile(pattern) if not isinstance(pattern, str) else None

        while time.time() < deadline:
            output = self.capture()
            if compiled:
                if compiled.search(output):
                    return output
            else:
                if pattern in output:
                    return output
            time.sleep(poll_interval)

        raise TmuxError(
            f"Timeout ({timeout}s) waiting for '{pattern}' in session '{self.name}'. "
            f"Last capture:\n{self.capture()}"
        )

    def wait_for_stable(self, timeout=10, stable_duration=1.0, poll_interval=0.25):
        """Wait for screen output to stop changing (app finished rendering).
        
        Args:
            timeout: total seconds before giving up
            stable_duration: seconds of no change required
            poll_interval: seconds between captures
            
        Returns:
            str: the stable screen contents
        """
        self._check_alive()
        deadline = time.time() + timeout
        last_output = None
        stable_since = None

        while time.time() < deadline:
            output = self.capture()
            if output == last_output:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= stable_duration:
                    return output
            else:
                last_output = output
                stable_since = None
            time.sleep(poll_interval)

        # Return whatever we have even if not fully stable
        return self.capture()

    def kill(self):
        """Kill the tmux session."""
        try:
            self._run(["tmux", "kill-session", "-t", self.name], check=False)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        self._alive = False

    def is_alive(self):
        """Check if the session is still running."""
        if not self._alive:
            return False
        alive = self.exists()
        self._alive = alive
        return alive

    def resize(self, width, height):
        """Resize the session's window.
        
        Args:
            width: new width in columns
            height: new height in rows
        """
        self._check_alive()
        try:
            self._run([
                "tmux", "resize-window",
                "-t", self.name,
                "-x", str(width),
                "-y", str(height),
            ])
        except subprocess.CalledProcessError as e:
            raise TmuxError(f"Failed to resize '{self.name}': {e.stderr}") from e

    def _check_alive(self):
        """Raise if session is not alive."""
        if not self._alive:
            raise TmuxError(f"Session '{self.name}' is not running. Call launch() first.")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.kill()

    def __repr__(self):
        host = f"@{self.ssh_host}" if self.ssh_host else "(local)"
        status = "alive" if self._alive else "dead"
        return f"TmuxSession('{self.name}', {host}, {status})"


def list_sessions(ssh_host=None, ssh_user=None, ssh_opts=None):
    """List all tmux sessions.
    
    Args:
        ssh_host: remote host (None = local)
        ssh_user: SSH username
        ssh_opts: additional SSH options
        
    Returns:
        list of dicts with 'name', 'windows', 'created', 'attached' keys
    """
    dummy = TmuxSession("_list", ssh_host=ssh_host, ssh_user=ssh_user, ssh_opts=ssh_opts)
    try:
        result = dummy._run(
            ["tmux", "list-sessions", "-F",
             "#{session_name}\t#{session_windows}\t#{session_created}\t#{session_attached}"],
            check=False,
        )
        if result.returncode != 0:
            return []
        sessions = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 4:
                sessions.append({
                    "name": parts[0],
                    "windows": int(parts[1]),
                    "created": int(parts[2]),
                    "attached": int(parts[3]) > 0,
                })
        return sessions
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
