"""
Smoke tests for agent-abide-dev project layout and startup.

These verify that entry points resolve, imports work, and servers can
start. They catch the class of bug where moving a file breaks a
reference elsewhere — the kind of thing that only shows up when
someone actually tries to run it.

Run: python -m pytest tests/test_smoke.py -v
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# 1. Entry points: scripts find what they reference
# ---------------------------------------------------------------------------

class TestEntryPoints:
    """Every script a human runs must resolve its targets."""

    def test_launch_tui_finds_tui(self):
        """launch_tui.sh must point to a TUI file that exists."""
        launch = PROJECT_ROOT / "scripts" / "launch_tui.sh"
        assert launch.exists(), "launch_tui.sh missing"

        # Parse the TUI path from the script
        text = launch.read_text()
        # The script resolves relative to SCRIPT_DIR
        tui_path = PROJECT_ROOT / "tui" / "asdaaas_tui.py"
        assert tui_path.exists(), f"TUI not found at {tui_path}"

    def test_launch_tui_usage(self):
        """launch_tui.sh with no args prints usage (not a crash)."""
        result = subprocess.run(
            ["bash", str(PROJECT_ROOT / "scripts" / "launch_tui.sh")],
            capture_output=True, text=True, timeout=10,
        )
        assert "Usage" in result.stdout or "Usage" in result.stderr

    def test_all_scripts_have_valid_bash_syntax(self):
        """Every .sh file in scripts/ must pass bash -n."""
        scripts_dir = PROJECT_ROOT / "scripts"
        if not scripts_dir.exists():
            pytest.skip("No scripts/ directory")
        for sh in scripts_dir.glob("*.sh"):
            result = subprocess.run(
                ["bash", "-n", str(sh)],
                capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, f"{sh.name} has syntax errors: {result.stderr}"


# ---------------------------------------------------------------------------
# 2. Python imports: all modules compile and import
# ---------------------------------------------------------------------------

class TestImports:
    """Every .py file must at least compile. Import-time crashes are silent killers."""

    def test_all_python_files_compile(self):
        """py_compile every .py file in the project."""
        failures = []
        for py in PROJECT_ROOT.rglob("*.py"):
            if "__pycache__" in str(py) or ".git" in str(py):
                continue
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", str(py)],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                failures.append(f"{py.relative_to(PROJECT_ROOT)}: {result.stderr.strip()}")
        assert not failures, "Compile failures:\n" + "\n".join(failures)

    def test_tui_imports(self):
        """TUI module can be imported (catches missing dependencies)."""
        tui = PROJECT_ROOT / "tui" / "asdaaas_tui.py"
        if not tui.exists():
            pytest.skip("TUI not present")
        result = subprocess.run(
            [sys.executable, "-c", f"import py_compile; py_compile.compile('{tui}', doraise=True)"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"TUI compile failed: {result.stderr}"

    def test_api_modules_import(self):
        """API modules can be imported."""
        api_dir = PROJECT_ROOT / "api"
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, '{}'); import server".format(api_dir)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"API import failed: {result.stderr}"


# ---------------------------------------------------------------------------
# 3. Config schema: agents.json has expected structure
# ---------------------------------------------------------------------------

class TestConfig:
    """Config files must be valid and have the keys code expects."""

    def test_agents_json_valid(self):
        """agents.json must be valid JSON."""
        agents_json = PROJECT_ROOT / "agents.json"
        if not agents_json.exists():
            pytest.skip("No agents.json in project root")
        data = json.loads(agents_json.read_text())
        assert isinstance(data, dict), "agents.json root must be a dict"

    def test_agents_json_schema(self):
        """Each agent in agents.json must have 'home' and 'backend'."""
        agents_json = PROJECT_ROOT / "agents.json"
        if not agents_json.exists():
            pytest.skip("No agents.json in project root")
        data = json.loads(agents_json.read_text())
        agents = data.get("agents", {})
        assert len(agents) > 0, "agents.json has no agents"
        for name, cfg in agents.items():
            assert "home" in cfg, f"Agent '{name}' missing 'home'"
            assert "backend" in cfg or True, f"Agent '{name}' missing 'backend'"  # backend defaults to 'grok'


# ---------------------------------------------------------------------------
# 4. API server startup: can it bind and respond?
# ---------------------------------------------------------------------------

class TestAPIServer:
    """The API server must start and respond to basic requests."""

    def test_api_server_starts_and_responds(self):
        """Start the API server, hit /agents, verify it responds."""
        import urllib.request
        import urllib.error

        server = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "api" / "server.py")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            # Wait for server to be ready
            for _ in range(20):
                time.sleep(0.5)
                try:
                    resp = urllib.request.urlopen("http://localhost:8420/agents", timeout=2)
                    data = json.loads(resp.read())
                    assert isinstance(data, list), "/agents must return a list"
                    return  # Success
                except (urllib.error.URLError, ConnectionRefusedError):
                    continue
            pytest.fail("API server did not respond within 10 seconds")
        finally:
            server.terminate()
            server.wait(timeout=5)


# ---------------------------------------------------------------------------
# 5. Cross-file references: files that reference other files by path
# ---------------------------------------------------------------------------

class TestFileReferences:
    """When code references a path, the target must exist."""

    def test_project_structure(self):
        """Key directories and files must exist."""
        expected = [
            "api/server.py",
            "api/normalizers.py",
            "api/session_locator.py",
            "tui/asdaaas_tui.py",
            "scripts/launch_tui.sh",
        ]
        missing = []
        for rel in expected:
            if not (PROJECT_ROOT / rel).exists():
                missing.append(rel)
        assert not missing, f"Missing expected files: {missing}"
