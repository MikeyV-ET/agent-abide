"""Tests for the task adapter (process-wrapper control adapter).

Ported from mikeyv-infra/tests/test_task_adapter.py to agent-abide.
Unit tests: Job, JobManager, resolve_script, deliver_result
Integration tests: full command flow through process_command with filesystem
"""

import json
import os
import sys
import time
import threading
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add core and adapters directories to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapters"))

import task_adapter
from task_adapter import (
    Job, JobManager, resolve_script, deliver_doorbell, deliver_result,
    process_command, ADAPTER_NAME,
)


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def agents_home(tmp_path):
    """Create temp agents home directory."""
    home = tmp_path / "agents"
    for agent in ["Q", "Sr", "Jr", "Trip", "Cinco"]:
        (home / agent / "asdaaas" / "doorbells").mkdir(parents=True)
        (home / agent / "asdaaas" / "adapters" / "task" / "inbox").mkdir(parents=True)
        (home / agent / "tools").mkdir(parents=True)
    return home


@pytest.fixture
def patch_paths(agents_home, monkeypatch):
    """Patch task_adapter paths to use temp dirs."""
    monkeypatch.setattr(task_adapter, "AGENTS_HOME_DIR", agents_home)
    monkeypatch.setattr(task_adapter, "PAYLOAD_DIR", agents_home)
    return agents_home


@pytest.fixture
def simple_script(agents_home):
    """Create a simple test script that prints its args."""
    script = agents_home / "Q" / "tools" / "echo_test.py"
    script.write_text('import sys; print(" ".join(sys.argv[1:]))\n')
    return script


@pytest.fixture
def failing_script(agents_home):
    """Create a script that exits with error."""
    script = agents_home / "Q" / "tools" / "fail_test.py"
    script.write_text('import sys; print("error msg", file=sys.stderr); sys.exit(1)\n')
    return script


@pytest.fixture
def slow_script(agents_home):
    """Create a script that sleeps (for timeout testing)."""
    script = agents_home / "Q" / "tools" / "slow_test.py"
    script.write_text('import time; time.sleep(60); print("done")\n')
    return script


@pytest.fixture
def large_output_script(agents_home):
    """Create a script that produces large output."""
    script = agents_home / "Q" / "tools" / "large_test.py"
    script.write_text('print("x" * 10000)\n')
    return script


@pytest.fixture
def stdin_script(agents_home):
    """Create a script that reads stdin."""
    script = agents_home / "Q" / "tools" / "stdin_test.py"
    script.write_text('import sys; data = sys.stdin.read(); print(f"got: {data}")\n')
    return script


# ============================================================================
# JOB UNIT TESTS
# ============================================================================

class TestJob:
    def test_successful_run(self, patch_paths, simple_script):
        job = Job("test1", "Q", str(simple_script), ["hello", "world"],
                  str(patch_paths / "Q"), dict(os.environ), timeout=30)
        job.run()
        job.thread.join(timeout=10)
        assert job.status == "completed"
        assert job.exit_code == 0
        assert "hello world" in job.stdout

    def test_failed_run(self, patch_paths, failing_script):
        job = Job("test2", "Q", str(failing_script), [],
                  str(patch_paths / "Q"), dict(os.environ), timeout=30)
        job.run()
        job.thread.join(timeout=10)
        assert job.status == "failed"
        assert job.exit_code == 1
        assert "error msg" in job.stderr

    def test_timeout(self, patch_paths, slow_script):
        job = Job("test3", "Q", str(slow_script), [],
                  str(patch_paths / "Q"), dict(os.environ), timeout=2)
        job.run()
        job.thread.join(timeout=15)
        assert job.status == "timeout"

    def test_script_not_found(self, patch_paths):
        job = Job("test4", "Q", "/nonexistent/script.py", [],
                  str(patch_paths / "Q"), dict(os.environ), timeout=10)
        job.run()
        job.thread.join(timeout=10)
        assert job.status == "failed"
        assert job.exit_code != 0

    def test_stdin_input(self, patch_paths, stdin_script):
        job = Job("test5", "Q", str(stdin_script), [],
                  str(patch_paths / "Q"), dict(os.environ), timeout=10,
                  input_data="hello from stdin")
        job.run()
        job.thread.join(timeout=10)
        assert job.status == "completed"
        assert "got: hello from stdin" in job.stdout

    def test_to_dict(self, patch_paths, simple_script):
        job = Job("test6", "Q", str(simple_script), ["x"],
                  str(patch_paths / "Q"), dict(os.environ), timeout=10)
        job.run()
        job.thread.join(timeout=10)
        d = job.to_dict()
        assert d["job_id"] == "test6"
        assert d["script"] == "echo_test.py"
        assert d["status"] == "completed"
        assert "elapsed_seconds" in d

    def test_kill(self, patch_paths, slow_script):
        job = Job("test7", "Q", str(slow_script), [],
                  str(patch_paths / "Q"), dict(os.environ), timeout=60)
        job.run()
        time.sleep(1)
        assert job.status == "running"
        job.kill()
        assert job.status == "killed"


# ============================================================================
# JOB MANAGER TESTS
# ============================================================================

class TestJobManager:
    def test_create_and_track(self, patch_paths, simple_script):
        jm = JobManager(max_concurrent_per_agent=5)
        job, err = jm.create_job("Q", str(simple_script), ["hi"],
                                 str(patch_paths / "Q"), dict(os.environ), 30)
        assert err is None
        assert job is not None
        assert jm.get_job(job.job_id) is job
        job.thread.join(timeout=10)

    def test_concurrent_limit(self, patch_paths, slow_script):
        jm = JobManager(max_concurrent_per_agent=2)
        jobs = []
        for i in range(2):
            job, err = jm.create_job("Q", str(slow_script), [],
                                     str(patch_paths / "Q"), dict(os.environ), 60)
            assert err is None
            jobs.append(job)

        # Third should be rejected
        job3, err3 = jm.create_job("Q", str(slow_script), [],
                                   str(patch_paths / "Q"), dict(os.environ), 60)
        assert job3 is None
        assert "concurrent limit" in err3

        # Clean up
        for j in jobs:
            j.kill()

    def test_get_agent_jobs(self, patch_paths, simple_script):
        jm = JobManager()
        j1, _ = jm.create_job("Q", str(simple_script), ["a"],
                               str(patch_paths / "Q"), dict(os.environ), 10)
        j2, _ = jm.create_job("Sr", str(simple_script), ["b"],
                               str(patch_paths / "Sr"), dict(os.environ), 10)
        j1.thread.join(timeout=10)
        j2.thread.join(timeout=10)

        q_jobs = jm.get_agent_jobs("Q")
        assert len(q_jobs) == 1
        assert q_jobs[0].job_id == j1.job_id

    def test_cleanup(self, patch_paths, simple_script):
        jm = JobManager()
        job, _ = jm.create_job("Q", str(simple_script), [],
                               str(patch_paths / "Q"), dict(os.environ), 10)
        job.thread.join(timeout=10)
        # Fake old end time
        job.end_time = time.time() - 7200
        jm.cleanup_old_jobs(max_age=3600)
        assert jm.get_job(job.job_id) is None


# ============================================================================
# RESOLVE SCRIPT TESTS
# ============================================================================

class TestResolveScript:
    def test_absolute_path(self, patch_paths, simple_script):
        resolved, err = resolve_script(str(simple_script), "Q")
        assert err is None
        assert resolved == str(simple_script)

    def test_relative_in_tools(self, patch_paths, simple_script):
        resolved, err = resolve_script("echo_test.py", "Q")
        assert err is None
        assert "tools" in resolved

    def test_not_found(self, patch_paths):
        resolved, err = resolve_script("nonexistent.py", "Q")
        assert resolved is None
        assert "not found" in err

    def test_whitelist_allowed(self, patch_paths, simple_script):
        resolved, err = resolve_script(str(simple_script), "Q",
                                       allowed_dirs=[str(patch_paths / "Q" / "tools")])
        assert err is None

    def test_whitelist_denied(self, patch_paths, simple_script):
        resolved, err = resolve_script(str(simple_script), "Q",
                                       allowed_dirs=["/some/other/dir"])
        assert resolved is None
        assert "not in allowed" in err


# ============================================================================
# DOORBELL DELIVERY TESTS
# ============================================================================

class TestDeliverResult:
    def test_short_output(self, patch_paths):
        deliver_result("Q", "test.py", "job123", "ok", "hello", 0)
        bells = list((patch_paths / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 1
        data = json.loads(bells[0].read_text())
        assert "ok: hello" in data["text"]
        assert "job123" in data["text"]

    def test_large_output_creates_payload(self, patch_paths):
        big_output = "x" * 10000
        deliver_result("Q", "test.py", "bigjob", "ok", big_output, 0)
        payload_dir = patch_paths / "Q" / "asdaaas" / "adapters" / "task" / "payloads"
        payloads = list(payload_dir.glob("*.txt"))
        assert len(payloads) == 1
        assert payloads[0].read_text() == big_output

    def test_error_result(self, patch_paths):
        deliver_result("Q", "test.py", "errjob", "error", "something broke", 1)
        bells = list((patch_paths / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 1
        data = json.loads(bells[0].read_text())
        assert "error:" in data["text"]
        assert "exit_code=1" in data["text"]


# ============================================================================
# PROCESS_COMMAND INTEGRATION TESTS
# ============================================================================

class TestProcessCommand:
    def test_run_command(self, patch_paths, simple_script):
        jm = JobManager()
        cmd = {"command": "run", "script": str(simple_script), "args": ["hello"], "timeout": 10}
        process_command(cmd, "Q", jm)

        # Should have ack doorbell
        bells = list((patch_paths / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) >= 1
        ack = json.loads(bells[0].read_text())
        assert "ack:" in ack["text"]

        # Wait for job to complete
        jobs = jm.get_agent_jobs("Q")
        assert len(jobs) == 1
        jobs[0].thread.join(timeout=10)

        # Should have result doorbell too
        time.sleep(0.5)
        bells = list((patch_paths / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) >= 2

    def test_run_relative_script(self, patch_paths, simple_script):
        jm = JobManager()
        cmd = {"command": "run", "script": "echo_test.py", "args": ["relative"], "timeout": 10}
        process_command(cmd, "Q", jm)
        jobs = jm.get_agent_jobs("Q")
        assert len(jobs) == 1
        jobs[0].thread.join(timeout=10)
        assert jobs[0].status == "completed"

    def test_status_command(self, patch_paths, simple_script):
        jm = JobManager()
        cmd_run = {"command": "run", "script": str(simple_script), "args": [], "timeout": 10}
        process_command(cmd_run, "Q", jm)
        jobs = jm.get_agent_jobs("Q")
        jobs[0].thread.join(timeout=10)

        # Clear doorbells
        for f in (patch_paths / "Q" / "asdaaas" / "doorbells").glob("*.json"):
            f.unlink()

        cmd_status = {"command": "status", "job_id": jobs[0].job_id}
        process_command(cmd_status, "Q", jm)
        bells = list((patch_paths / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 1
        data = json.loads(bells[0].read_text())
        assert "completed" in data["text"]

    def test_list_command(self, patch_paths, simple_script):
        jm = JobManager()
        cmd = {"command": "run", "script": str(simple_script), "args": [], "timeout": 10}
        process_command(cmd, "Q", jm)
        jm.get_agent_jobs("Q")[0].thread.join(timeout=10)

        for f in (patch_paths / "Q" / "asdaaas" / "doorbells").glob("*.json"):
            f.unlink()

        process_command({"command": "list"}, "Q", jm)
        bells = list((patch_paths / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 1

    def test_kill_command(self, patch_paths, slow_script):
        jm = JobManager()
        cmd = {"command": "run", "script": str(slow_script), "args": [], "timeout": 60}
        process_command(cmd, "Q", jm)
        time.sleep(1)

        jobs = jm.get_agent_jobs("Q")
        assert jobs[0].status == "running"

        for f in (patch_paths / "Q" / "asdaaas" / "doorbells").glob("*.json"):
            f.unlink()

        process_command({"command": "kill", "job_id": jobs[0].job_id}, "Q", jm)
        assert jobs[0].status == "killed"

    def test_unknown_command(self, patch_paths):
        jm = JobManager()
        process_command({"command": "bogus"}, "Q", jm)
        bells = list((patch_paths / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 1
        data = json.loads(bells[0].read_text())
        assert "unknown command" in data["text"]

    def test_missing_script_field(self, patch_paths):
        jm = JobManager()
        process_command({"command": "run"}, "Q", jm)
        bells = list((patch_paths / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 1
        data = json.loads(bells[0].read_text())
        assert "script" in data["text"]

    def test_whitelist_rejection(self, patch_paths, simple_script):
        jm = JobManager()
        cmd = {"command": "run", "script": str(simple_script), "timeout": 10}
        process_command(cmd, "Q", jm, allowed_dirs=["/restricted/only"])
        bells = list((patch_paths / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 1
        data = json.loads(bells[0].read_text())
        assert "not in allowed" in data["text"]

    def test_env_vars(self, patch_paths, agents_home):
        script = agents_home / "Q" / "tools" / "env_test.py"
        script.write_text('import os; print(os.environ.get("MY_VAR", "unset"))\n')
        jm = JobManager()
        cmd = {"command": "run", "script": str(script), "env": {"MY_VAR": "hello"}, "timeout": 10}
        process_command(cmd, "Q", jm)
        jm.get_agent_jobs("Q")[0].thread.join(timeout=10)
        assert jm.get_agent_jobs("Q")[0].stdout.strip() == "hello"

    def test_timeout_enforcement(self, patch_paths, slow_script):
        jm = JobManager()
        cmd = {"command": "run", "script": str(slow_script), "timeout": 2}
        process_command(cmd, "Q", jm)
        jobs = jm.get_agent_jobs("Q")
        jobs[0].thread.join(timeout=15)
        assert jobs[0].status == "timeout"
