#!/usr/bin/env python3
"""
Task Adapter — General-purpose process-wrapper control adapter.
================================================================
Runs scripts as isolated subprocesses on behalf of agents. The agent
writes a command, the adapter spawns the process, and the result comes
back as a doorbell. Errors, hangs, and crashes in the script never
propagate to the agent.

The agent writes:
    {"command": "run", "script": "grok_interactor.py", "args": ["--send", "hi"], "timeout": 120}

The adapter immediately delivers an ack:
    [task:grok_interactor.py] ack: job_id=abc123

When the script finishes:
    [task:grok_interactor.py (job=abc123)] ok: <stdout>
    [task:grok_interactor.py (job=abc123)] error: exit_code=1 <stderr>
    [task:grok_interactor.py (job=abc123)] timeout: killed after 120s

Other commands:
    {"command": "status", "job_id": "abc123"}
    {"command": "kill", "job_id": "abc123"}
    {"command": "list"}

Usage:
    python3 task_adapter.py
    python3 task_adapter.py --agents Sr,Jr,Trip,Q,Cinco
    python3 task_adapter.py --max-concurrent 5 --default-timeout 120
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import argparse
import secrets
import signal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter_api

def tprint(msg):
    """Timestamped print."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ============================================================================
# CONFIG
# ============================================================================

try:
    from asdaaas_config import config
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'core'))
    from asdaaas_config import config

HUB_DIR = config.hub_dir
AGENTS_HOME_DIR = config.agents_home
ADAPTER_NAME = "task"
POLL_INTERVAL = 0.25
ALL_AGENTS = ["Sr", "Jr", "Trip", "Q", "Cinco"]

DEFAULT_TIMEOUT = 120  # seconds
MAX_CONCURRENT_PER_AGENT = 5
MAX_OUTPUT_BYTES = 4096  # inline in doorbell; larger goes to payload file
PAYLOAD_DIR = AGENTS_HOME_DIR  # payloads stored under agent dirs


# ============================================================================
# DOORBELL DELIVERY
# ============================================================================

def deliver_doorbell(agent, text, priority=1):
    """Write a doorbell file for an agent (atomic write)."""
    bell_dir = AGENTS_HOME_DIR / agent / "asdaaas" / "doorbells"
    bell_dir.mkdir(parents=True, exist_ok=True)

    bell = {
        "adapter": ADAPTER_NAME,
        "priority": priority,
        "text": text,
        "ts": time.time(),
    }

    fd, tmp_path = tempfile.mkstemp(dir=str(bell_dir), suffix=".tmp", prefix="task_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(bell, f)
        final = tmp_path.replace(".tmp", ".json")
        os.rename(tmp_path, final)
    except Exception as e:
        tprint(f"[task] ERROR writing doorbell for {agent}: {e}")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def deliver_result(agent, script_name, job_id, status, output, exit_code=None):
    """Deliver a job result as a doorbell, using payload file for large output."""
    prefix = f"[task:{script_name} (job={job_id})]"

    if status == "ok":
        body = output or "(no output)"
    elif status == "error":
        code_str = f"exit_code={exit_code} " if exit_code is not None else ""
        body = f"{code_str}{output or '(no stderr)'}"
    elif status == "timeout":
        body = f"killed after timeout"
    else:
        body = output or ""

    full_text = f"{prefix} {status}: {body}"

    # If output is large, write to payload file
    if len(full_text) > MAX_OUTPUT_BYTES:
        payload_dir = AGENTS_HOME_DIR / agent / "asdaaas" / "adapters" / ADAPTER_NAME / "payloads"
        payload_dir.mkdir(parents=True, exist_ok=True)
        payload_path = payload_dir / f"{job_id}.txt"
        payload_path.write_text(output or "")
        truncated = body[:200] + "..."
        full_text = (f"{prefix} {status}: {truncated}\n"
                     f"(Full output: {payload_path})")

    deliver_doorbell(agent, full_text)
    tprint(f"[task] RESULT: {agent} <- {full_text[:120]}")


# ============================================================================
# JOB MANAGEMENT
# ============================================================================

class Job:
    """Represents a running or completed subprocess job."""

    def __init__(self, job_id, agent, script, args, cwd, env, timeout, input_data=None):
        self.job_id = job_id
        self.agent = agent
        self.script = script
        self.script_name = os.path.basename(script)
        self.args = args
        self.cwd = cwd
        self.env = env
        self.timeout = timeout
        self.input_data = input_data
        self.process = None
        self.status = "pending"  # pending, running, completed, failed, timeout, killed
        self.stdout = ""
        self.stderr = ""
        self.exit_code = None
        self.start_time = None
        self.end_time = None
        self.thread = None

    def run(self):
        """Spawn the subprocess in a background thread."""
        self.status = "running"
        self.start_time = time.time()
        self.thread = threading.Thread(target=self._execute, daemon=True)
        self.thread.start()

    def _execute(self):
        """Execute the subprocess (runs in background thread)."""
        cmd = [sys.executable, self.script] + self.args
        tprint(f"[task] SPAWN: {self.agent} job={self.job_id} cmd={' '.join(cmd)}")

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE if self.input_data else subprocess.DEVNULL,
                cwd=self.cwd,
                env=self.env,
                start_new_session=True,  # isolate from our signal group
            )

            try:
                stdout_bytes, stderr_bytes = self.process.communicate(
                    input=self.input_data.encode() if self.input_data else None,
                    timeout=self.timeout,
                )
                self.stdout = stdout_bytes.decode(errors="replace")
                self.stderr = stderr_bytes.decode(errors="replace")
                self.exit_code = self.process.returncode

                if self.exit_code == 0:
                    self.status = "completed"
                    deliver_result(self.agent, self.script_name, self.job_id,
                                   "ok", self.stdout, self.exit_code)
                else:
                    self.status = "failed"
                    deliver_result(self.agent, self.script_name, self.job_id,
                                   "error", self.stderr, self.exit_code)

            except subprocess.TimeoutExpired:
                self._kill_process()
                self.status = "timeout"
                # Capture any partial output
                try:
                    stdout_bytes, stderr_bytes = self.process.communicate(timeout=5)
                    self.stdout = stdout_bytes.decode(errors="replace")
                    self.stderr = stderr_bytes.decode(errors="replace")
                except Exception:
                    pass
                deliver_result(self.agent, self.script_name, self.job_id,
                               "timeout", f"killed after {self.timeout}s", self.exit_code)

        except FileNotFoundError:
            self.status = "failed"
            self.exit_code = -1
            deliver_result(self.agent, self.script_name, self.job_id,
                           "error", f"script not found: {self.script}", -1)
        except PermissionError:
            self.status = "failed"
            self.exit_code = -1
            deliver_result(self.agent, self.script_name, self.job_id,
                           "error", f"permission denied: {self.script}", -1)
        except Exception as e:
            self.status = "failed"
            self.exit_code = -1
            deliver_result(self.agent, self.script_name, self.job_id,
                           "error", f"spawn error: {e}", -1)

        self.end_time = time.time()

    def _kill_process(self):
        """Kill the subprocess and its process group."""
        if self.process and self.process.poll() is None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                    self.process.wait(timeout=3)
            except (ProcessLookupError, PermissionError):
                pass

    def kill(self):
        """Kill the job externally."""
        self._kill_process()
        self.status = "killed"
        self.end_time = time.time()
        deliver_result(self.agent, self.script_name, self.job_id,
                       "error", "killed by agent request", -9)

    def to_dict(self):
        """Return job status as dict."""
        d = {
            "job_id": self.job_id,
            "script": self.script_name,
            "status": self.status,
            "exit_code": self.exit_code,
        }
        if self.start_time:
            elapsed = (self.end_time or time.time()) - self.start_time
            d["elapsed_seconds"] = round(elapsed, 1)
        return d


class JobManager:
    """Track all jobs across all agents."""

    def __init__(self, max_concurrent_per_agent=MAX_CONCURRENT_PER_AGENT):
        self.jobs = {}  # job_id -> Job
        self.max_concurrent = max_concurrent_per_agent
        self._lock = threading.Lock()

    def create_job(self, agent, script, args, cwd, env, timeout, input_data=None):
        """Create and start a new job."""
        with self._lock:
            # Check concurrent limit
            active = sum(1 for j in self.jobs.values()
                         if j.agent == agent and j.status == "running")
            if active >= self.max_concurrent:
                return None, f"concurrent limit ({self.max_concurrent}) reached"

            job_id = secrets.token_hex(6)
            job = Job(job_id, agent, script, args, cwd, env, timeout, input_data)
            self.jobs[job_id] = job

        job.run()
        return job, None

    def get_job(self, job_id):
        """Get a job by ID."""
        return self.jobs.get(job_id)

    def get_agent_jobs(self, agent):
        """Get all jobs for an agent."""
        return [j for j in self.jobs.values() if j.agent == agent]

    def cleanup_old_jobs(self, max_age=3600):
        """Remove completed jobs older than max_age seconds."""
        with self._lock:
            now = time.time()
            expired = [jid for jid, j in self.jobs.items()
                       if j.end_time and (now - j.end_time) > max_age]
            for jid in expired:
                del self.jobs[jid]
            if expired:
                tprint(f"[task] CLEANUP: removed {len(expired)} expired jobs")


# ============================================================================
# SCRIPT RESOLUTION & WHITELIST
# ============================================================================

def resolve_script(script_path, agent, allowed_dirs=None):
    """Resolve a script path and check if it's allowed.

    Resolution order:
    1. Absolute path — use as-is
    2. Relative path — resolve against agent's tools dir, then agent home
    3. Check allowed_dirs whitelist (if configured)

    Returns (resolved_path, error_message). error_message is None on success.
    """
    path = Path(script_path)

    if not path.is_absolute():
        # Try agent's tools dir first
        tools_path = AGENTS_HOME_DIR / agent / "tools" / script_path
        if tools_path.exists():
            path = tools_path
        else:
            # Try agent's home dir
            home_path = AGENTS_HOME_DIR / agent / script_path
            if home_path.exists():
                path = home_path
            else:
                # Try as-is (might be on PATH)
                path = Path(script_path)

    resolved = path.resolve() if path.exists() else path

    # Whitelist check
    if allowed_dirs:
        allowed = False
        for d in allowed_dirs:
            try:
                resolved.relative_to(Path(d).resolve())
                allowed = True
                break
            except ValueError:
                continue
        if not allowed:
            return None, f"script not in allowed directories: {script_path}"

    if not resolved.exists():
        return None, f"script not found: {script_path} (resolved: {resolved})"

    return str(resolved), None


# ============================================================================
# COMMAND PROCESSING
# ============================================================================

def process_command(cmd, agent, job_manager, allowed_dirs=None):
    """Process a task command from an agent."""
    command = cmd.get("command", "")

    if command == "run":
        script = cmd.get("script", "")
        if not script:
            deliver_doorbell(agent, f"[task] error: 'script' field required")
            return

        # Resolve and validate script
        resolved, err = resolve_script(script, agent, allowed_dirs)
        if err:
            deliver_doorbell(agent, f"[task:{os.path.basename(script)}] error: {err}")
            return

        args = cmd.get("args", [])
        if not isinstance(args, list):
            args = [str(args)]

        timeout = cmd.get("timeout", DEFAULT_TIMEOUT)
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT

        cwd = cmd.get("cwd", str(AGENTS_HOME_DIR / agent))
        input_data = cmd.get("input", None)

        # Build environment
        env = dict(os.environ)
        extra_env = cmd.get("env", {})
        if isinstance(extra_env, dict):
            env.update(extra_env)

        # Create and start job
        job, err = job_manager.create_job(agent, resolved, args, cwd, env, timeout, input_data)
        if err:
            deliver_doorbell(agent, f"[task:{os.path.basename(script)}] error: {err}")
            return

        # Immediate ack with job_id
        deliver_doorbell(agent,
            f"[task:{job.script_name}] ack: job_id={job.job_id}")
        tprint(f"[task] RUN: {agent} job={job.job_id} script={job.script_name}")

    elif command == "status":
        job_id = cmd.get("job_id", "")
        if job_id:
            job = job_manager.get_job(job_id)
            if job:
                deliver_doorbell(agent,
                    f"[task:status] {json.dumps(job.to_dict())}")
            else:
                deliver_doorbell(agent,
                    f"[task:status] error: unknown job_id={job_id}")
        else:
            # List all jobs for this agent
            jobs = job_manager.get_agent_jobs(agent)
            summary = [j.to_dict() for j in jobs]
            deliver_doorbell(agent,
                f"[task:status] {json.dumps(summary)}")

    elif command == "kill":
        job_id = cmd.get("job_id", "")
        job = job_manager.get_job(job_id)
        if job and job.agent == agent:
            if job.status == "running":
                job.kill()
                tprint(f"[task] KILL: {agent} job={job_id}")
            else:
                deliver_doorbell(agent,
                    f"[task:kill] job {job_id} not running (status={job.status})")
        else:
            deliver_doorbell(agent,
                f"[task:kill] error: unknown job_id={job_id}")

    elif command == "list":
        jobs = job_manager.get_agent_jobs(agent)
        summary = [j.to_dict() for j in jobs]
        deliver_doorbell(agent,
            f"[task:list] {json.dumps(summary)}")

    else:
        deliver_doorbell(agent,
            f"[task] error: unknown command '{command}'. "
            f"Use: run, status, kill, list")


# ============================================================================
# MAIN LOOP
# ============================================================================

def run_adapter(agents, max_concurrent=MAX_CONCURRENT_PER_AGENT,
                default_timeout=DEFAULT_TIMEOUT, allowed_dirs=None):
    """Main loop: poll command inboxes, manage jobs."""
    tprint(f"[task] Task adapter starting")
    tprint(f"[task] Watching agents: {', '.join(agents)}")
    tprint(f"[task] Max concurrent per agent: {max_concurrent}")
    tprint(f"[task] Default timeout: {default_timeout}s")
    if allowed_dirs:
        tprint(f"[task] Allowed dirs: {allowed_dirs}")

    adapter_api.register_adapter(
        name=ADAPTER_NAME,
        capabilities=["run", "status", "kill", "list"],
        config={
            "type": "control",
            "agents": agents,
            "commands": ["run", "status", "kill", "list"],
            "max_concurrent_per_agent": max_concurrent,
            "default_timeout": default_timeout,
        },
    )

    global DEFAULT_TIMEOUT
    DEFAULT_TIMEOUT = default_timeout

    job_manager = JobManager(max_concurrent_per_agent=max_concurrent)

    # Ensure per-agent inbox directories exist
    for agent in agents:
        inbox = AGENTS_HOME_DIR / agent / "asdaaas" / "adapters" / ADAPTER_NAME / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)

    last_heartbeat = time.time()
    last_cleanup = time.time()

    while True:
        try:
            for agent in agents:
                messages = adapter_api.poll_adapter_inbox(ADAPTER_NAME, agent)
                for msg in messages:
                    cmd = None
                    text = msg.get("text", "")
                    if text:
                        try:
                            cmd = json.loads(text)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    if cmd is None:
                        cmd = msg.get("meta", {})
                    if not cmd.get("command") and msg.get("command"):
                        cmd = msg

                    if cmd and cmd.get("command"):
                        process_command(cmd, agent, job_manager, allowed_dirs)
                    else:
                        tprint(f"[task] MALFORMED from {agent}: {str(msg)[:100]}")
                        deliver_doorbell(agent,
                            "[task] error: malformed command. Expected: "
                            '{"command": "run", "script": "...", "args": [...]}')

            # Heartbeat
            now = time.time()
            if now - last_heartbeat >= 30:
                adapter_api.update_heartbeat(ADAPTER_NAME)
                last_heartbeat = now

            # Periodic cleanup of old completed jobs
            if now - last_cleanup >= 300:
                job_manager.cleanup_old_jobs()
                last_cleanup = now

        except Exception as e:
            tprint(f"[task] ERROR: {e}")
            import traceback
            traceback.print_exc()

        time.sleep(POLL_INTERVAL)


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="MikeyV Task Adapter")
    parser.add_argument("--agents", default=None,
                        help="Comma-separated agent list (default: all)")
    parser.add_argument("--max-concurrent", type=int, default=MAX_CONCURRENT_PER_AGENT,
                        help=f"Max concurrent jobs per agent (default: {MAX_CONCURRENT_PER_AGENT})")
    parser.add_argument("--default-timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"Default timeout in seconds (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--allowed-dirs", default=None,
                        help="Comma-separated allowed script directories (default: no restriction)")
    args = parser.parse_args()

    agents = [a.strip() for a in args.agents.split(",")] if args.agents else list(ALL_AGENTS)
    allowed_dirs = [d.strip() for d in args.allowed_dirs.split(",")] if args.allowed_dirs else None

    def handle_sigterm(signum, frame):
        tprint("[task] SIGTERM received, shutting down.")
        adapter_api.deregister_adapter(ADAPTER_NAME)
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)

    try:
        run_adapter(agents, args.max_concurrent, args.default_timeout, allowed_dirs)
    except KeyboardInterrupt:
        print("\n[task] Shutting down.")
        adapter_api.deregister_adapter(ADAPTER_NAME)


if __name__ == "__main__":
    main()
